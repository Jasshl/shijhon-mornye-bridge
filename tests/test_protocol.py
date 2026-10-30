from __future__ import annotations

import asyncio
import base64
import json

import httpx
import pytest
from conftest import EXT, TRACK, FakeWorker

from mornye_bridge.errors import BridgeError, native_error
from mornye_bridge.ids import decode_id, encode_id
from mornye_bridge.server import create_app
from mornye_bridge.service import Service, manifest


@pytest.mark.parametrize("value", ["simpleID", "dot.2f", "slash/id", "urn:one", "héllo", ".", ".."])
def test_ids(value: str) -> None:
    assert decode_id(encode_id(value)) == value
    assert encode_id("/") != encode_id(".2f")


@pytest.mark.parametrize("value", ["", "bad/", ".", "a.ff", "a.2F", "x" * 191])
def test_bad_ids(value: str) -> None:
    with pytest.raises((ValueError, UnicodeError)):
        decode_id(value)


async def test_http_catalog_and_manifest(config) -> None:
    worker = FakeWorker()
    service = Service(config, worker)
    app = create_app(service)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://bridge"
        ) as client:
            base = f"/addons/{service.access_token}/invented"
            assert (await client.get("/addons/wrong/invented/manifest.json")).status_code == 404
            result = (await client.get(base + "/manifest.json")).json()
            assert result["fullCatalog"] == {"key": "invented", "region": "", "clientArtwork": True}
            assert set(result["resources"]) == {
                "search",
                "catalog",
                "fullcatalog",
                "stream",
                "availability",
                "cached",
                "resolve",
            }
            assert result["settings"][0]["key"] == "format"
            assert result["settings"][0]["default"] == "native-aac"
            searched = (
                await client.get(base + "/search", params={"q": "Invented", "limit": 70})
            ).json()
            track = searched["tracks"][0]
            assert "explicit" not in track and "trackNumber" not in track
            assert track["album"]["id"] == encode_id("album:one")
            assert all(
                c[1]["arguments_json"].find('"limit": 70') >= 0
                for c in worker.calls
                if c[0] == "invokeExtensionAction"
            )
            album = (await client.get(base + "/album/" + encode_id("album:one"))).json()
            assert album["label"] == "Invented Label"
            assert album["tracks"][0]["album"] == {
                "id": encode_id("album:one"),
                "title": "Invented Album",
            }
            artist = (await client.get(base + "/artist/" + encode_id("artist:one"))).json()
            assert artist["releasesNext"] == "next:page"
            page = (
                await client.get(
                    base + "/catalog/artist/" + encode_id("artist:one") + "/releases",
                    params={"cursor": artist["releasesNext"]},
                )
            ).json()
            assert page["next"] is None
            assert page["albums"][0]["id"] == encode_id("album:two")
            assert (await client.get(base + "/catalog/isrc/ZZABC2600001")).json()["tracks"][0][
                "isrc"
            ] == "ZZABC2600001"
            song = (await client.get(base + "/catalog/track/" + encode_id(TRACK["id"]))).json()
            assert song["isrc"] == TRACK["isrc"]
            assert (await client.get(base + "/search?q=Invented&limit=101")).status_code == 400
            assert (await client.get(base + "/stream/bad.2F")).status_code == 400


async def test_preparation_timeout_ranges_cached_and_cancellation(config) -> None:
    worker = FakeWorker()
    worker.release.clear()
    service = Service(config, worker)
    app = create_app(service)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://bridge"
        ) as client:
            base = f"/addons/{service.access_token}/invented"
            ident = encode_id(TRACK["id"])
            wanted = {"title": "Invented Song", "artist": "Invented Artist", "durationMs": "1000"}
            assert (await client.get(base + "/cached", params=wanted)).json() == {
                "available": False
            }
            pending = await client.get(base + "/stream/" + ident)
            assert pending.status_code == 503 and pending.headers["retry-after"] == "2"
            assert pending.json() == {"error": "preparing"}
            assert len(service.jobs) == 1
            assert (await client.get(base + "/stream/" + ident)).status_code == 503
            assert sum(c[0] == "downloadByStrategy" for c in worker.calls) == 1
            worker.release.set()
            await asyncio.gather(*service.jobs.values())
            ready = (await client.get(base + "/stream/" + ident)).json()
            assert ready["codec"] == "pcm_s16le" and ready["format"] == "wav"
            audio_path = httpx.URL(ready["url"]).path
            part = await client.get(audio_path, headers={"Range": "bytes=0-3"})
            assert part.status_code == 206 and part.content == b"RIFF"
            assert (await client.head(audio_path)).headers["accept-ranges"] == "bytes"
            assert (
                await client.get(audio_path, headers={"Range": "bytes=999999-"})
            ).status_code == 416
            cached = (
                await client.get(base + "/cached", params=wanted | {"format": "native-flac"})
            ).json()
            assert cached["available"]
            descriptor = (
                await client.get(
                    base + "/cached-stream/" + cached["selector"],
                    params={"format": "unknown-preference"},
                )
            ).json()
            assert descriptor["url"] == ready["url"]
            assert sum(c[0] == "downloadByStrategy" for c in worker.calls) == 1
            worker.release.clear()
            assert (await client.get(base + "/stream/" + encode_id("track:two"))).status_code == 503
            await service.cancel("invented", encode_id("track:two"), "native-aac")
            assert worker.cancelled and not service.jobs
            assert list((config.state_dir / "jobs").iterdir()) == []


async def test_management_redaction_and_state(config) -> None:
    worker = FakeWorker()
    service = Service(config, worker)
    app = create_app(service)
    async with app.router.lifespan_context(app):
        assert len(base64.b64decode((config.state_dir / "master.key").read_text())) == 32
        for name in ("master.key", "access.token", "admin.token"):
            assert (config.state_dir / name).stat().st_mode & 0o777 == 0o600
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://bridge"
        ) as client:
            assert (await client.post("/management", json={})).status_code == 404
            headers = {"Authorization": "Bearer " + service.admin_token}

            async def manage(operation, **params):
                response = await client.post(
                    "/management",
                    headers=headers,
                    json={"operation": operation, "params": {"extension": "invented", **params}},
                )
                assert response.status_code == 200
                return response.json()

            shown = await manage("settings.show")
            assert shown == {"region": "xx", "mediaUserToken": "[redacted]", "other": "[redacted]"}
            await manage("settings.set", values={"region": "zz"})
            assert worker.settings["mediaUserToken"] == "private-token"
            await manage("action", action="login")
            assert worker.settings["region"] == "yy"
            pending = await manage("pending-auth.show")
            assert pending["pending"] and "private.example" not in json.dumps(pending)
            worker.fail = BridgeError("needs_login_or_verification")
            base = f"/addons/{service.access_token}/invented"
            assert (await client.get(base + "/search?q=Invented")).status_code == 503
            assert service.management.states["invented"] == "needs_login_or_verification"
            worker.fail = BridgeError("rate_limited", 429, 60)
            response = await client.get(base + "/search?q=Invented")
            assert response.status_code == 429 and response.headers["retry-after"] == "60"
            worker.fail = None
            await manage("disable")
            assert (await client.get(base + "/manifest.json")).status_code == 404


async def test_exclusive_state_and_stable_secrets(config) -> None:
    first = Service(config, FakeWorker())
    await first.start()
    key = (config.state_dir / "master.key").read_text()
    second = Service(config, FakeWorker())
    with pytest.raises(BridgeError, match="state_already_in_use"):
        await second.start()
    await first.close()
    await second.start()
    assert (config.state_dir / "master.key").read_text() == key
    await second.close()


def test_native_defaults(config) -> None:
    assert (
        manifest(
            EXT | {"quality_options": [{"id": "first"}, {"id": "second", "default": True}]}, config
        )["settings"][0]["default"]
        == "second"
    )


async def test_opaque_long_cursor_and_nested_action_updates(config) -> None:
    class Extended(FakeWorker):
        async def call(self, method, args=None):
            if method == "invokeExtensionAction" and args["action"] == "getArtist":
                cursor = json.loads(args["arguments_json"])[0]
                assert cursor == "opaque:" + "x" * 480
                return {"albums": [], "albums_next": None}
            if method == "invokeExtensionAction" and args["action"] == "nested":
                return {
                    "success": True,
                    "result": {"success": True, "setting_updates": {"region": "nested"}},
                }
            return await super().call(method, args)

    service = Service(config, Extended())
    await service.start()
    try:
        assert await service.catalog.releases("invented", "opaque:" + "x" * 480) == {
            "albums": [],
            "next": None,
        }
        await service.execute("action", {"extension": "invented", "action": "nested"})
        assert (await service.execute("settings.show", {"extension": "invented"}))[
            "region"
        ] == "nested"
        with pytest.raises(BridgeError, match="grant_not_completed"):
            await service.execute(
                "pending-auth.grant", {"extension": "invented", "grant": "invented"}
            )
    finally:
        await service.close()


async def test_prepare_availability_without_id_and_disable(config) -> None:
    worker = FakeWorker()
    worker.release.clear()
    service = Service(config, worker)
    await service.start()
    try:
        wanted = {"title": "Invented Song", "artist": "Invented Artist", "durationMs": "1000"}
        assert service.prepare_wanted(EXT, wanted, "native-aac")
        await asyncio.wait_for(worker.started.wait(), 1)
        assert service.preparing("invented", wanted, "native-aac")
        await service.execute("disable", {"extension": "invented"})
        assert worker.cancelled and not service.jobs
    finally:
        await service.close()


async def test_lifecycle_management_contract(config) -> None:
    class Lifecycle(FakeWorker):
        async def call(self, method, args=None):
            if method == "getRepoRegistryUrl":
                return "https://registry.example.test/index.json"
            if method == "getRepoExtensions":
                return [
                    {
                        "id": "invented",
                        "download_url": "https://private.example.test/signed?token=secret",
                    }
                ]
            if method == "downloadRepoExtension":
                return str(config.state_dir / "repo" / "invented.package")
            if method in ("loadExtensionFromPath", "upgradeExtension"):
                return EXT
            if method == "queueExtensionCallbackUrl":
                return True
            if method == "completeExtensionSessionGrant":
                return True
            if method == "checkExtensionHealth":
                return {
                    "status": "online",
                    "checks": [
                        {
                            "id": "invented",
                            "status": "online",
                            "url": "https://private.example.test/",
                            "message": "private-token",
                        }
                    ],
                }
            return await super().call(method, args)

    service = Service(config, Lifecycle())
    await service.start()
    try:
        assert (await service.execute("registry.get", {}))["registry"].startswith("https:")
        assert await service.execute(
            "registry.set", {"url": "https://registry.example.test/index.json"}
        ) == {"saved": True}
        available = await service.execute("available", {})
        assert "download_url" not in available[0]
        for operation in ("install", "update"):
            assert (await service.execute(operation, {"extension": "invented"}))["id"] == "invented"
        schema = await service.execute("describe", {"extension": "invented"})
        assert next(x for x in schema["settings"] if x["key"] == "mediaUserToken")["secret"]
        assert await service.execute(
            "pending-auth.grant", {"extension": "invented", "grant": "invented"}
        ) == {"completed": True}
        assert (
            await service.execute(
                "pending-auth.callback", {"url": "mornye://callback?state=invented"}
            )
        )["submitted"]
        health = await service.execute("health", {"extension": "invented"})
        assert health["status"] == "online" and "private-token" not in json.dumps(health)
        assert await service.execute("remove", {"extension": "invented"}) == {"saved": True}
    finally:
        await service.close()


async def test_manifest_song_alias_and_default_unfiltered_search(config) -> None:
    class Search(FakeWorker):
        async def call(self, method, args=None):
            if method == "invokeExtensionAction" and args["action"] == "customSearch":
                query, options = json.loads(args["arguments_json"])
                assert options["limit"] == 9
                if "filter" in options:
                    assert options["filter"] == "songs"
                return [TRACK]
            return await super().call(method, args)

    service = Service(config, Search())
    await service.start()
    try:
        for behavior in ({"filters": [{"id": "songs"}]}, {}):
            result = await service.catalog.search(
                EXT | {"search_behavior": behavior}, "Invented", 9
            )
            assert result["tracks"][0]["id"] == encode_id(TRACK["id"])
    finally:
        await service.close()


@pytest.mark.parametrize("code", ["auth_required", "auth_failed", "verification required"])
def test_native_auth_error_is_safe_and_unavailable(code) -> None:
    error = native_error(
        {"code": code, "message": "https://private.example.test/?grant=private-token"}
    )
    assert error.status == 503
    assert str(error) == "needs_login_or_verification"
