from __future__ import annotations

import asyncio
import json
import time

import httpx
import pytest
from conftest import ALBUM, TRACK, FakeWorker

from mornye_bridge.catalog import map_item
from mornye_bridge.errors import native_error
from mornye_bridge.ids import decode_id, encode_id, single_id, single_track_id
from mornye_bridge.server import create_app
from mornye_bridge.service import Service


@pytest.mark.parametrize(
    "kind,message,status",
    [
        ("not_found", "Absent recording", 404),
        ("quality_unavailable", "Selected audio format absent", 404),
        ("extension_error", "Fallback stopped by Invented: direct track is not playable", 404),
        ("api_error", "This track does not offer a full playable stream", 404),
        ("download_error", "No supported full Invented stream is available", 404),
        ("provider_unavailable", "Provider unavailable", 502),
        ("extension_error", "Direct lookup failed: upstream request failed", 502),
        ("network", "Connection timed out", 502),
        ("permission", "Cannot create output", 502),
        ("extension_error", "Unknown extension failure", 502),
        ("verification_required", "Login needed", 503),
        ("rate_limit", "Rate limit", 429),
    ],
)
def test_native_recording_misses_are_distinct_from_failures(kind, message, status):
    error = native_error({"error_type": kind, "error": message})
    assert error.status == status
    assert message not in str(error)


class MissingWorker(FakeWorker):
    def __init__(self):
        super().__init__()
        self.downloads = 0
        self.error_type = "not_found"

    async def call(self, method, args=None):
        if method == "downloadByStrategy":
            self.downloads += 1
            return {"success": False, "error_type": self.error_type, "error": "Invented miss"}
        return await super().call(method, args)


async def test_miss_memory_covers_stream_and_both_availability_workflows(config):
    worker = MissingWorker()
    service = Service(config, worker)
    app = create_app(service)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://bridge"
        ) as client:
            base = f"/addons/{service.access_token}/invented"
            ident = encode_id(TRACK["id"])
            wanted = {
                "title": TRACK["name"],
                "artist": TRACK["artists"],
                "durationMs": "1000",
                "prepare": "true",
            }
            assert (await client.get(base + "/stream/" + ident)).status_code == 404
            assert (await client.get(base + "/stream/" + ident)).status_code == 404
            for query in (wanted, {"id": ident, "prepare": "true"}):
                result = await client.get(base + "/availability", params=query)
                assert result.json() == {"available": False, "preparing": False}
            assert not service.jobs and worker.downloads == 1
            # A miss at one selected format cannot hide another native format.
            assert (
                await client.get(base + "/stream/" + ident, params={"format": "native-flac"})
            ).status_code == 404
            assert worker.downloads == 2
            # Expiry permits a new preparation; a later success can recover.
            service.misses[("invented", TRACK["id"], "native-aac")] = (time.monotonic() - 1, None)
            assert (await client.get(base + "/stream/" + ident)).status_code == 404
            assert worker.downloads == 3
            assert list((config.state_dir / "jobs").iterdir()) == []
            await service.execute(
                "settings.set", {"extension": "invented", "values": {"region": "yy"}}
            )
            assert not service.misses


async def test_background_preparation_remembers_misses_and_failures(config):
    worker = MissingWorker()
    service = Service(config, worker)
    app = create_app(service)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://bridge"
        ) as client:
            base = f"/addons/{service.access_token}/invented"
            wanted = {
                "title": TRACK["name"],
                "artist": TRACK["artists"],
                "durationMs": "1000",
                "prepare": "true",
            }
            assert (await client.get(base + "/availability", params=wanted)).json()["preparing"]
            await asyncio.gather(*list(service.jobs.values()), return_exceptions=True)
            assert not (await client.get(base + "/availability", params=wanted)).json()["preparing"]
            assert worker.downloads == 1
            # Other failures are remembered too and answered at once with their code.
            worker.error_type = "network"
            for _ in range(2):
                reply = await client.get(base + "/stream/another")
                assert reply.status_code == 502 and reply.json() == {"error": "extension_failed"}
                assert "retry-after" not in reply.headers
            assert worker.downloads == 2
            assert service.unavailable("invented", {"id": "another"}, "native-aac")
            # Another format is another preparation; settings changes forget failures.
            assert (
                await client.get(base + "/stream/another", params={"format": "native-flac"})
            ).status_code == 502
            assert worker.downloads == 3
            await service.execute(
                "settings.set", {"extension": "invented", "values": {"region": "yy"}}
            )
            assert not service.failures and not service.failure_codes
            assert (await client.get(base + "/stream/another")).status_code == 502
            assert worker.downloads == 4


@pytest.mark.parametrize("native", ["track:one", "dot.2f", "héllo/track", "a" * 182])
def test_single_id_round_trip_and_disjoint_namespace(native):
    ident = single_id(native)
    assert single_track_id(ident) == native
    assert len(ident) <= 190
    assert encode_id(".single.track") != ".single.track"
    with pytest.raises(ValueError):
        decode_id(ident)


def test_single_id_limit_fails_explicitly():
    with pytest.raises(ValueError):
        single_id("a" * 183)
    with pytest.raises(ValueError):
        single_track_id(".single.bad.2F")


class SingleWorker(FakeWorker):
    async def call(self, method, args=None):
        if method == "invokeExtensionAction":
            action = args["action"]
            if action == "getTrack":
                arguments = json.loads(args["arguments_json"])
                if arguments[0] == "missing":
                    return None
                if arguments[0] != TRACK["id"]:
                    return TRACK | {"id": arguments[0]}
                return self.track()
            if (
                action == "customSearch"
                and json.loads(args["arguments_json"])[1]["filter"] == "tracks"
            ):
                return [self.track()]
        return await super().call(method, args)

    @staticmethod
    def track():
        return TRACK | {
            "album_id": "",
            "album_name": "",
            "artists": [{"id": "artist:one", "name": "Invented Artist"}],
            "cover_url": "https://images.example.test/invented.jpg",
            "release_date": "2026-10-01",
            "disc_number": 4,
            "track_number": 12,
        }


async def test_albumless_search_lookup_and_restart_single_release(config):
    for _ in range(2):
        service = Service(config, SingleWorker())
        app = create_app(service)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://bridge"
            ) as client:
                base = f"/addons/{service.access_token}/invented"
                # Lookup the single first: no search or remembered album association needed.
                ident = single_id(TRACK["id"])
                response = await client.get(base + "/album/" + ident)
                assert response.status_code == 200
                release = response.json()
                assert release["id"] == ident and release["kind"] == "single"
                assert release["title"] == "Invented Song - Single"
                assert release["trackCount"] == len(release["tracks"]) == 1
                track = release["tracks"][0]
                assert track["discNumber"] == track["trackNumber"] == 1
                for key in ("artist", "artists", "artworkURL", "releaseDate"):
                    assert release[key] == track[key]
                assert track["album"] == {"id": ident, "title": release["title"]}
                search = (await client.get(base + "/search?q=Invented")).json()
                lookup = (
                    await client.get(base + "/catalog/track/" + encode_id(TRACK["id"]))
                ).json()
                assert search["tracks"][0] == lookup == track
                # A real album's parent is supplied before track mapping, preserving its numbering.
                album = (await client.get(base + "/album/" + encode_id(ALBUM["id"]))).json()
                assert album["tracks"][0]["album"]["id"] == encode_id(ALBUM["id"])
                assert "trackNumber" not in album["tracks"][0]
                assert (
                    await client.get(base + "/album/" + single_id("has-album"))
                ).status_code == 404
                assert (
                    await client.get(base + "/album/" + single_id("missing"))
                ).status_code == 404
                assert (await client.get(base + "/album/.single.bad.2F")).status_code == 400


def test_native_album_identity_is_preserved():
    track = map_item(TRACK | {"disc_number": 2, "track_number": 7}, "track")
    assert track["album"] == {"id": encode_id(TRACK["album_id"]), "title": TRACK["album_name"]}
    assert track["discNumber"] == 2
    assert track["trackNumber"] == 7


class TitleOnlyWorker(SingleWorker):
    @staticmethod
    def track():
        return SingleWorker.track() | {"album_name": "An unrelated owned album"}


async def test_title_only_album_search_lookup_and_single_refusal(config):
    service = Service(config, TitleOnlyWorker())
    app = create_app(service)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://bridge"
        ) as client:
            base = f"/addons/{service.access_token}/invented"
            search = await client.get(base + "/search?q=Invented")
            lookup = await client.get(base + "/catalog/track/" + encode_id(TRACK["id"]))
            assert search.status_code == lookup.status_code == 200
            track = lookup.json()
            assert search.json()["tracks"][0] == track
            assert track["album"] == {"title": "An unrelated owned album"}
            assert track["discNumber"] == 4
            assert track["trackNumber"] == 12
            assert (await client.get(base + "/album/" + single_id(TRACK["id"]))).status_code == 404


@pytest.mark.parametrize("album_name", [None, ""])
def test_track_without_album_identity_gets_single(album_name):
    raw = SingleWorker.track()
    raw.pop("album_id")
    if album_name is None:
        raw.pop("album_name")
    track = map_item(raw, "track")
    assert track["album"] == {
        "id": single_id(raw["id"]),
        "title": "Invented Song - Single",
    }
    assert track["discNumber"] == track["trackNumber"] == 1


async def test_title_only_album_download_keeps_native_album_hint(config):
    config.stream_wait = 3
    worker = TitleOnlyWorker()
    service = Service(config, worker)
    app = create_app(service)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://bridge"
        ) as client:
            base = f"/addons/{service.access_token}/invented"
            response = await client.get(base + "/stream/" + encode_id(TRACK["id"]))
            assert response.status_code == 200
            payload = next(
                json.loads(args) for method, args in worker.calls if method == "downloadByStrategy"
            )
            assert payload["album_name"] == "An unrelated owned album"
            assert payload["source"] == "" and "provider_track_id" not in payload
            entry = service.cache.get("invented", TRACK["id"], "native-aac")
            assert entry.metadata["album"] == {"title": "An unrelated owned album"}
