from __future__ import annotations

import asyncio
import json
import logging

import httpx
import pytest
from conftest import EXT, TRACK, FakeWorker

from mornye_bridge.config import Config
from mornye_bridge.errors import BridgeError
from mornye_bridge.ids import (
    ITEM,
    RECORDING_PREFIX,
    decode_id,
    encode_id,
    external_id,
    internal_id,
    parse_recording,
    recording_id,
)
from mornye_bridge.server import create_app
from mornye_bridge.service import Service, transfer_slots

WANTED = {
    "title": "Invented Tune",
    "artist": "Invented Band",
    "durationMs": "1000",
    "isrc": "zz-abc-26-00009",
}


class Finder(FakeWorker):
    """checkAvailability answers with opaque context; downloads report ``reported``
    (the host's answer fields) next to the 1-second synthetic file."""

    def __init__(self, available: bool = True) -> None:
        super().__init__()
        self.available = available
        self.answer: dict[str, object] = {}
        self.reported: dict[str, object] = {"isrc": "ZZABC2600009"}
        self.checks: list[list[object]] = []
        self.downloads: list[dict[str, object]] = []

    async def call(self, method, args=None):
        if method == "invokeExtensionAction" and args["action"] == "checkAvailability":
            self.calls.append((method, args))
            self.checks.append(json.loads(args["arguments_json"]))
            return {
                "available": self.available,
                "track_id": "opaque:1",
                "prepared_context": {"opaque": True},
            } | self.answer
        if method == "downloadByStrategy":
            self.downloads.append(json.loads(args))
            result = await super().call(method, args)
            return result | self.reported
        return await super().call(method, args)


@pytest.mark.parametrize(
    "fields",
    [
        ("ZZABC2600009", "Invented Tune", "Invented Band", 1000, "Invented Album"),
        ("", "héllo / wörld", "A & B", 0, ""),
        ("", "x" * 120, "y", 1, ""),
    ],
)
def test_recording_id_round_trip_and_disjoint_from_native_ids(fields):
    ident = recording_id(fields)
    assert ident and ident.startswith(RECORDING_PREFIX) and ITEM.fullmatch(ident)
    assert parse_recording(ident) == fields
    with pytest.raises(ValueError):
        decode_id(ident)
    assert external_id(internal_id(ident)) == ident
    native = encode_id(RECORDING_PREFIX + "x")
    assert internal_id(native) == RECORDING_PREFIX + "x"


def test_recording_id_drops_album_then_needs_a_reference():
    long_album = ("", "Tune", "Band", 1000, "a" * 200)
    assert parse_recording(recording_id(long_album) or "") == ("", "Tune", "Band", 1000, "")
    assert recording_id(("", "t" * 200, "Band", 1000, "")) is None
    for bad in (RECORDING_PREFIX + "0", RECORDING_PREFIX + "abc.d", RECORDING_PREFIX):
        with pytest.raises(ValueError):
            parse_recording(bad)


@pytest.mark.parametrize(
    "capabilities,slots",
    [
        (None, 3),
        ({}, 3),
        ({"downloadTransfer": {}}, 3),
        ({"downloadTransfer": {"maxConcurrentDownloads": 1}}, 1),
        ({"downloadTransfer": {"maxConcurrentDownloads": 1.5}}, 2),
        ({"downloadTransfer": {"maxConcurrentDownloads": 0}}, 1),
        ({"downloadTransfer": {"maxConcurrentDownloads": 99}}, 3),
        ({"downloadTransfer": {"maxConcurrentDownloads": True}}, 3),
        ({"downloadTransfer": {"maxConcurrentDownloads": "2"}}, 3),
    ],
)
def test_transfer_slots_follow_the_host_rules(capabilities, slots):
    assert transfer_slots({"capabilities": capabilities}) == slots


def test_default_overall_preparations(tmp_path):
    config = Config(library=tmp_path / "unused")
    assert config.preparations == 6 < config.admission


async def test_resolve_finds_by_description_and_streams_through_the_planner(config, caplog):
    config.stream_wait = 10
    caplog.set_level(logging.INFO, logger="mornye_bridge")
    worker = Finder()
    service = Service(config, worker)
    app = create_app(service)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://bridge"
        ) as client:
            base = f"/addons/{service.access_token}/invented"
            found = await client.get(base + "/resolve", params=WANTED | {"format": "native-aac"})
            item = found.json()["item"]
            assert item == {
                "id": item["id"],
                "title": "Invented Tune",
                "artist": "Invented Band",
                "durationMs": 1000,
                "isrc": "ZZABC2600009",
            }
            assert item["id"].startswith(RECORDING_PREFIX) and len(item["id"]) <= 190
            isrc, title, artist, options = worker.checks[0]
            assert (isrc, title, artist) == ("ZZABC2600009", "Invented Tune", "Invented Band")
            assert set(options) == {"duration_ms", "track"} and options["duration_ms"] == 1000
            assert options["track"]["name"] == "Invented Tune"
            assert options["track"]["artists"] == "Invented Band"
            assert options["track"]["isrc"] == "ZZABC2600009" and options["track"]["id"] == ""
            # A repeat is answered from memory.
            assert (await client.get(base + "/resolve", params=WANTED)).json()["item"] == item
            assert len(worker.checks) == 1

            reply = await client.get(base + "/stream/" + item["id"])
            assert reply.status_code == 200
            payload = worker.downloads[0]
            assert payload["source"] == ""
            assert payload["service"] == payload["download_provider"] == "invented"
            assert not any(k.endswith("_id") and k != "item_id" for k in payload)
            assert payload["use_fallback"] is False and payload["use_extensions"] is True
            assert (payload["isrc"], payload["track_name"], payload["artist_name"]) == (
                "ZZABC2600009",
                "Invented Tune",
                "Invented Band",
            )
            assert payload["duration_ms"] == 1000 and payload["album_name"] == ""
            assert not any(
                isinstance(c[1], dict) and c[1].get("action") == "getTrack" for c in worker.calls
            )

            # Cache, availability and cached matching use the wanted recording.
            assert (await client.get(base + "/stream/" + item["id"])).status_code == 200
            assert len(worker.downloads) == 1
            ready = await client.get(base + "/availability", params={"id": item["id"]})
            assert ready.json() == {"available": True, "preparing": False, "id": item["id"]}
            described = await client.get(base + "/availability", params=WANTED)
            assert described.json()["available"] is True
            cached = await client.get(base + "/cached", params={"isrc": "ZZABC2600009"})
            assert cached.json()["available"] is True
            # Another description of the same ISRC is the same file.
            other = WANTED | {"title": "Invented Tune (Remaster)"}
            other_id = (await client.get(base + "/resolve", params=other)).json()["item"]["id"]
            assert len(worker.checks) == 1  # found in the cache by ISRC
            assert (await client.get(base + "/stream/" + other_id)).status_code == 200
            assert len(worker.downloads) == 1
    text = caplog.text
    assert "resolve extension=invented source=native" in text and "outcome=found" in text
    assert "preparation extension=invented kind=recording format=native-aac" in text
    assert "outcome=ready" in text
    for private in ("Invented Tune", "Invented Band", "ZZABC2600009", item["id"], "opaque"):
        assert private not in text


async def test_resolve_miss_is_404_remembered_and_cleared(config):
    worker = Finder(available=False)
    service = Service(config, worker)
    app = create_app(service)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://bridge"
        ) as client:
            base = f"/addons/{service.access_token}/invented"
            for _ in range(2):
                reply = await client.get(base + "/resolve", params=WANTED)
                assert reply.status_code == 404
            assert len(worker.checks) == 1
            await service.execute(
                "settings.set", {"extension": "invented", "values": {"region": "yy"}}
            )
            assert (await client.get(base + "/resolve", params=WANTED)).status_code == 404
            assert len(worker.checks) == 2
            for bad in ({"title": "x"}, WANTED | {"durationMs": "soon"}, WANTED | {"artist": " "}):
                assert (await client.get(base + "/resolve", params=bad)).status_code == 400
            assert (
                await client.get(base + "/resolve", params=WANTED | {"title": "t" * 501})
            ).status_code == 400


@pytest.mark.parametrize("answer,status", [({"available": "yes"}, 502), ([], 502), (None, 404)])
async def test_resolve_rejects_malformed_native_answers(config, answer, status):
    class Broken(Finder):
        async def call(self, method, args=None):
            if method == "invokeExtensionAction" and args["action"] == "checkAvailability":
                return answer
            return await super().call(method, args)

    service = Service(config, Broken())
    await service.start()
    try:
        ext = await service.extension("invented")
        with pytest.raises(BridgeError) as failure:
            await service.resolve(ext, WANTED)
        assert failure.value.status == status
    finally:
        await service.close()


async def test_long_recording_gets_a_reference_until_restart(config):
    config.stream_wait = 10
    long = WANTED | {"title": "Long " * 60}
    first = Service(config, Finder())
    app = create_app(first)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://bridge"
        ) as client:
            base = f"/addons/{first.access_token}/invented"
            item = (await client.get(base + "/resolve", params=long)).json()["item"]
            assert item["title"] == long["title"] and len(item["id"]) <= 190
            assert (await client.get(base + "/stream/" + item["id"])).status_code == 200
    second = Service(config, Finder())
    app = create_app(second)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://bridge"
        ) as client:
            base = f"/addons/{second.access_token}/invented"
            # The description is kept in the bridge state: another format still prepares.
            assert (await client.get(base + "/stream/" + item["id"])).status_code == 200
            flac = {"format": "native-flac"}
            reply = await client.get(base + "/stream/" + item["id"], params=flac)
            assert reply.status_code == 200
            # A pruned description is "not found", so the caller looks the song up again.
            assert second.cache
            second.cache.db.execute("UPDATE recordings SET used = 0")
            second.cache.db.commit()
            other = {"format": "native-aac"}
            entry = second.cache.get("invented", "\x00" + item["id"], "native-aac")
            assert entry
            second.cache.db.execute("DELETE FROM files WHERE selector=?", (entry.selector,))
            second.cache.db.commit()
            reply = await client.get(base + "/stream/" + item["id"], params=other)
            assert reply.status_code == 404
            reply = await client.get(
                base + "/availability", params={"id": item["id"], "prepare": "true"} | other
            )
            assert reply.json() == {"available": False, "preparing": False}


async def test_native_ids_are_prepared_from_their_details(config, tmp_path):
    config.stream_wait = 10

    class Catalog(Finder):
        async def call(self, method, args=None):
            if method == "invokeExtensionAction" and args["action"] == "getTrack":
                ident = json.loads(args["arguments_json"])[0]
                if ident == "nameless":
                    return TRACK | {"id": ident, "artists": ""}
                return TRACK | {"id": ident, "explicit": True}
            return await super().call(method, args)

    worker = Catalog()
    worker.reported = {"isrc": TRACK["isrc"]}
    service = Service(config, worker)
    await service.start()
    try:
        ext = await service.extension("invented")
        entry = await service.stream(ext, encode_id(TRACK["id"]), "native-aac")
        payload = worker.downloads[0]
        # The documented way: details from getTrack, then the planner finds the track.
        assert payload["source"] == "" and payload["service"] == "invented"
        assert "provider_track_id" not in payload
        assert not any("_id" in k and k != "item_id" for k in payload)
        assert (payload["isrc"], payload["track_name"], payload["artist_name"]) == (
            TRACK["isrc"],
            TRACK["name"],
            TRACK["artists"],
        )
        assert payload["album_name"] == TRACK["album_name"]
        # Published under the native ID with checked length and ISRC; the catalog's
        # version stays because the result reported the track's own ISRC.
        assert entry.native_id == TRACK["id"]
        assert entry.metadata["id"] == encode_id(TRACK["id"])
        assert entry.metadata["isrc"] == TRACK["isrc"]
        assert entry.metadata["explicit"] is True
        assert entry.metadata["album"]["id"] == encode_id(TRACK["album_id"])
        # Without the reported ISRC, the length decides and the version is not kept.
        worker.reported = {}
        entry = await service.stream(ext, encode_id("second"), "native-aac")
        assert "isrc" not in entry.metadata and "explicit" not in entry.metadata
        # Another reported ISRC is another recording.
        worker.reported = {"isrc": "ZZABC2600099"}
        with pytest.raises(BridgeError) as failure:
            await service.stream(ext, encode_id("third"), "native-aac")
        assert failure.value.code == "different_recording_isrc"
        # A track without an artist cannot be looked up.
        downloads = len(worker.downloads)
        with pytest.raises(BridgeError) as failure:
            await service.stream(ext, encode_id("nameless"), "native-aac")
        assert failure.value.status == 404 and failure.value.code == "track_lacks_details"
        assert len(worker.downloads) == downloads
    finally:
        await service.close()


async def test_files_cached_by_native_id_stay_usable(config, tmp_path):
    worker = Finder()
    service = Service(config, worker)
    await service.start()
    try:
        # A file prepared earlier under a native ID (as the direct path published it).
        source = tmp_path / "earlier.flac"
        source.write_bytes(b"invented")
        assert service.cache
        track = {"id": encode_id(TRACK["id"]), "title": TRACK["name"], "durationMs": 1000}
        service.cache.publish("invented", TRACK["id"], "native-aac", source, track, {})
        ext = await service.extension("invented")
        entry = await service.stream(ext, encode_id(TRACK["id"]), "native-aac")
        assert entry.metadata == track and not worker.downloads
    finally:
        await service.close()


async def test_extension_slots_and_overall_cap(config):
    config.stream_wait = 10

    class Limited(Finder):
        async def call(self, method, args=None):
            if method == "getInstalledExtensions":
                return [
                    EXT
                    | {
                        "enabled": self.enabled,
                        "capabilities": {"downloadTransfer": {"maxConcurrentDownloads": 1}},
                    }
                ]
            return await super().call(method, args)

    worker = Limited()
    worker.reported = {"isrc": TRACK["isrc"]}
    worker.release.clear()
    service = Service(config, worker)
    app = create_app(service)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://bridge"
        ) as client:
            base = f"/addons/{service.access_token}/invented"
            config.stream_wait = 0.02
            first = await client.get(base + "/stream/one")
            assert first.status_code == 503 and first.json() == {"error": "preparing"}
            busy = await client.get(base + "/stream/two")
            assert busy.json() == {"error": "preparation_busy"}
            assert busy.headers["retry-after"] == "3"
            worker.release.set()
            await asyncio.gather(*service.jobs.values())
            config.stream_wait = 10
            assert (await client.get(base + "/stream/two")).status_code == 200
    config.preparations = 1
    ext = EXT | {"_generation": 0}
    service = Service(config, FakeWorker())
    await service.start()
    try:
        assert service.slots_free(ext)
        service.jobs[("other", "x", "f")] = asyncio.get_running_loop().create_future()  # type: ignore[assignment]
        assert not service.slots_free(ext)
        service.jobs.clear()
    finally:
        await service.close()


async def test_login_and_rate_limit_failures_are_not_remembered(config):
    class Refusing(FakeWorker):
        downloads = 0
        error_type = "verification_required"

        async def call(self, method, args=None):
            if method == "downloadByStrategy":
                self.downloads += 1
                return {"success": False, "error_type": self.error_type, "error": "x"}
            return await super().call(method, args)

    worker = Refusing()
    service = Service(config, worker)
    app = create_app(service)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://bridge"
        ) as client:
            base = f"/addons/{service.access_token}/invented"
            for error_type, status in (("verification_required", 503), ("rate_limit", 429)):
                worker.error_type = error_type
                before = worker.downloads
                for _ in range(2):
                    reply = await client.get(base + "/stream/one")
                    assert reply.status_code == status
                assert worker.downloads == before + 2
            worker.error_type = "verification_required"
            reply = await client.get(base + "/stream/one")
            # Login is no "ask again soon": no Retry-After.
            assert "retry-after" not in reply.headers
            assert not service.failures


async def test_prepared_recordings_are_verified_before_publication(config, caplog):
    caplog.set_level(logging.INFO, logger="mornye_bridge")
    config.stream_wait = 10
    worker = Finder()
    service = Service(config, worker)
    app = create_app(service)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://bridge"
        ) as client:
            base = f"/addons/{service.access_token}/invented"

            async def stream(wanted):
                item = (await client.get(base + "/resolve", params=wanted)).json()["item"]
                return await client.get(base + "/stream/" + item["id"])

            # The synthetic file lasts 1 s: an 8 s recording is another one.
            reply = await stream(WANTED | {"durationMs": "8000"})
            assert reply.status_code == 404
            assert reply.json() == {"error": "different_recording_length"}
            assert "outcome=different_recording_length" in caplog.text
            assert service.misses and not list(service.cache.entries("invented"))
            assert list((config.state_dir / "jobs").iterdir()) == []
            downloads = len(worker.downloads)
            assert (await stream(WANTED | {"durationMs": "8000"})).status_code == 404
            assert len(worker.downloads) == downloads  # remembered
            # A reported ISRC that differs is another recording.
            worker.reported = {"isrc": "ZZABC2600010"}
            reply = await stream(WANTED | {"title": "Second Tune"})
            assert reply.json() == {"error": "different_recording_isrc"}
            # Without a wanted length only the wanted ISRC, reported, will do.
            worker.reported = {}
            reply = await stream(WANTED | {"title": "Third Tune", "durationMs": "0"})
            assert reply.json() == {"error": "unverified_recording"}
            # A verified file publishes the probed length and what the result reports.
            worker.reported = {"isrc": "ZZABC2600011", "explicit": True}
            wanted = {"title": "Fourth Tune", "artist": "Invented Band", "durationMs": "2500"}
            assert (await stream(wanted)).status_code == 200
            [entry] = service.cache.entries("invented")
            assert entry.metadata["durationMs"] == 1000
            assert entry.metadata["isrc"] == "ZZABC2600011"
            assert entry.metadata["explicit"] is True
            for version, available in (("explicit", True), ("clean", False)):
                query = wanted | {"durationMs": "1000", "version": version}
                reply = await client.get(base + "/cached", params=query)
                assert reply.json()["available"] is available


@pytest.mark.parametrize(
    "answer,status",
    [
        ({"reason": "verification_required"}, 503),
        ({"error_type": "auth_required", "reason": "x"}, 503),
        ({"error_type": "rate_limit", "retry_after_seconds": 7}, 429),
    ],
)
async def test_resolve_keeps_login_and_rate_limits_apart_from_absence(config, answer, status):
    worker = Finder(available=False)
    worker.answer = answer
    service = Service(config, worker)
    app = create_app(service)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://bridge"
        ) as client:
            base = f"/addons/{service.access_token}/invented"
            for _ in range(2):
                reply = await client.get(base + "/resolve", params=WANTED)
                assert reply.status_code == status
            assert len(worker.checks) == 2 and not service.resolved
            if status == 503:
                assert reply.json() == {"error": "needs_login_or_verification"}
                assert "retry-after" not in reply.headers
                assert service.management.states["invented"] == "needs_login_or_verification"
            else:
                assert reply.headers["retry-after"] == "7"


async def test_changing_extensions_ask_again_shortly(config):
    service = Service(config, FakeWorker())
    app = create_app(service)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://bridge"
        ) as client:
            base = f"/addons/{service.access_token}/invented"
            service.changing.add("invented")
            reply = await client.get(base + "/resolve", params=WANTED)
            assert reply.status_code == 503 and reply.headers["retry-after"] == "2"
            service.changing.clear()
            ext = await service.extension("invented")
            service.generations["invented"] = 1
            with pytest.raises(BridgeError) as failure:
                service.check_snapshot(ext)
            assert failure.value.retry_after == 2
