"""Independent-review regressions: invented metadata, synthetic files and fake IPC only."""

from __future__ import annotations

import asyncio
import copy
import json
import time
from pathlib import Path

import httpx
import pytest
from conftest import EXT, TRACK, FakeWorker
from test_web import PASSWORD, csrf, setup
from test_worker import worker as pipe_worker

from mornye_bridge.catalog import map_item
from mornye_bridge.cli import parser
from mornye_bridge.config import Config
from mornye_bridge.errors import BridgeError, native_error
from mornye_bridge.ids import encode_id
from mornye_bridge.server import create_app
from mornye_bridge.service import Service, manifest, selected_format
from mornye_bridge.web_forms import fields_view, parse_values


async def test_progress_extends_download_without_generic_timeout(config):
    config.call_timeout = 0.2
    config.download_stall_timeout = 0.15
    host = pipe_worker(config)
    try:
        task = asyncio.create_task(
            host.call("downloadByStrategy", json.dumps({"item_id": "progress"}))
        )
        await asyncio.sleep(0.25)
        assert not task.done()
        assert await host.call("echo", 42) == 42
        assert (await task)["success"]
        assert host.restarts == 0
    finally:
        await host.close()


@pytest.mark.parametrize(
    "item,cap,restarts", [("stalled", 10, 0), ("cap", 0.25, 0), ("hung", 10, 1)]
)
async def test_watchdog_cancels_one_job_and_only_restarts_hang(config, item, cap, restarts):
    config.call_timeout = 0.2
    config.download_stall_timeout = 0.15
    config.download_timeout = cap
    config.cancel_timeout = 0.12
    host = pipe_worker(config)
    try:
        other = asyncio.create_task(
            host.call("downloadByStrategy", json.dumps({"item_id": "progress"}))
        )
        task = asyncio.create_task(host.call("downloadByStrategy", json.dumps({"item_id": item})))
        with pytest.raises(BridgeError, match="download_timeout"):
            await task
        assert (config.state_dir / "cancel-observed").exists()
        assert (config.state_dir / ("item-cancel-" + item)).exists()
        assert host.restarts == restarts
        assert await host.call("echo", 19) == 19
        if restarts:
            with pytest.raises(BridgeError, match="worker_restarted"):
                await other
        elif item == "cap":
            # Absolute cap also applies to the other transfer.
            with pytest.raises(BridgeError, match="download_timeout"):
                await other
        else:
            assert (await other)["success"]
    finally:
        await host.close()


async def test_timeout_backoff_is_not_a_miss_and_busy_is_quick(config):
    class TimeoutWorker(FakeWorker):
        async def call(self, method, args=None):
            if method == "downloadByStrategy":
                self.started.set()
                await self.release.wait()
                raise BridgeError("download_timeout")
            return await super().call(method, args)

    config.stream_wait = 0.001
    config.preparations = 1
    fake = TimeoutWorker()
    fake.release.clear()
    service = Service(config, fake)
    app = create_app(service)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://bridge"
        ) as client:
            base = f"/addons/{service.access_token}/invented"
            ext = await service.extension("invented")
            task = service.begin(ext, "one", "native-aac")
            await fake.started.wait()
            busy = await client.get(base + "/availability", params={"id": "two", "prepare": "true"})
            assert busy.status_code == 200 and busy.json() == {
                "available": False,
                "preparing": False,
            }
            wanted = {
                "title": "Another",
                "artist": "Artist",
                "durationMs": "1000",
                "prepare": "true",
            }
            assert (await client.get(base + "/availability", params=wanted)).json()[
                "preparing"
            ] is False
            pending = await client.get(base + "/availability", params={"id": "one"})
            assert pending.json()["preparing"] is True
            fake.release.set()
            with pytest.raises(BridgeError, match="download_timeout"):
                await task
            assert service.backoffs and not service.misses
            for _ in range(3):
                reply = await client.get(
                    base + "/availability", params={"id": "one", "prepare": "true"}
                )
                assert reply.json() == {"available": False, "preparing": False}
                assert (await client.get(base + "/stream/one")).status_code == 503
            assert not service.jobs
            service.backoffs.clear()
            with pytest.raises(BridgeError, match="download_timeout"):
                await service.begin(ext, "one", "native-aac")


def test_default_non_spatial_and_explicit_choice(config):
    options = [
        {"id": "surround", "kind": "spatial"},
        {"id": "other", "spatial": True},
        {"id": "stereo", "kind": "lossy"},
    ]
    ext = EXT | {"quality_options": options}
    assert selected_format(ext) == "stereo"
    assert selected_format(ext, "surround") == "surround"
    options[0]["default"] = True
    assert selected_format(ext) == "surround"
    assert manifest(ext, config)["settings"][0]["default"] == "surround"


async def test_begin_cached_publish_duplicates_and_rollback(config, tmp_path):
    service = Service(config, FakeWorker())
    await service.start()
    try:
        assert service.cache
        cache = service.cache
        path = tmp_path / "first.wav"
        path.write_bytes(b"synthetic")
        entry = cache.publish(
            "invented", "one", "native-aac", path, map_item(TRACK | {"id": "one"}, "track"), {}
        )
        for _ in range(2):
            assert (await service.begin(EXT, "one", "native-aac")).selector == entry.selector
        assert not service.jobs
        duplicate = tmp_path / "duplicate.wav"
        duplicate.write_bytes(b"duplicate")
        assert (
            cache.publish("invented", "one", "native-aac", duplicate, {}, {}).selector
            == entry.selector
        )
        assert not duplicate.exists() and len(list(cache.root.iterdir())) == 1
        cache.db.execute(
            "CREATE TRIGGER reject_insert BEFORE INSERT ON files "
            "BEGIN SELECT RAISE(FAIL, 'invented failure'); END"
        )
        rejected = tmp_path / "rejected.wav"
        rejected.write_bytes(b"rejected")
        with pytest.raises(Exception, match="invented failure"):
            cache.publish("invented", "two", "native-aac", rejected, {}, {})
        assert len(list(cache.root.iterdir())) == 1
    finally:
        await service.close()


async def test_stale_lifecycle_snapshot_never_starts_work(config):
    fake = FakeWorker()
    service = Service(config, fake)
    await service.start()
    try:
        ext = await service.extension("invented")
        service.changing.add("invented")
        with pytest.raises(BridgeError, match="extension_changing"):
            service.begin(ext, "one", "native-aac")
        with pytest.raises(BridgeError, match="extension_changing"):
            await service.catalog.lookup("invented", "track", "one")
        service.changing.clear()
        await service.execute("disable", {"extension": "invented"})
        with pytest.raises(BridgeError):
            service.begin(ext, "one", "native-aac")
        await service.execute("enable", {"extension": "invented"})
        with pytest.raises(BridgeError, match="extension_changed_retry"):
            service.begin(ext, "one", "native-aac")
        assert not service.jobs
    finally:
        await service.close()


async def test_metadata_cache_invalidates_on_lifecycle(config):
    service = Service(config, FakeWorker())
    await service.management.installed()
    await service.management.installed()
    assert len(service.worker.calls) == 1
    await service.execute("disable", {"extension": "invented"})
    assert not (await service.management.extension("invented"))["enabled"]
    assert sum(method == "getInstalledExtensions" for method, _ in service.worker.calls) == 2


async def test_exact_isrc_and_negative_capability_decay(config):
    class Search(FakeWorker):
        rows = [TRACK, TRACK | {"id": "other", "isrc": "ZZABC2600002"}]

        async def call(self, method, args=None):
            if method == "invokeExtensionAction" and args["action"] == "customSearch":
                self.calls.append((method, args))
                return copy.deepcopy(self.rows)
            return await super().call(method, args)

    fake = Search()
    service = Service(config, fake)
    app = create_app(service)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://bridge"
        ) as client:
            base = f"/addons/{service.access_token}/invented"
            reply = await client.get(base + "/catalog/isrc/" + TRACK["isrc"])
            assert [row["id"] for row in reply.json()["tracks"]] == [encode_id(TRACK["id"])]
            assert (await client.get(base + "/catalog/isrc/ZZABC2699999")).json() == {"tracks": []}
            # Audio add-ons find songs by resolve; there is no ISRC text search for them.
            assert (
                await client.get(base + "/resolve-isrc", params={"isrc": TRACK["isrc"]})
            ).status_code == 404
            fake.rows = [TRACK | {"isrc": ""}]
            for _ in range(3):
                assert (await client.get(base + "/catalog/isrc/" + TRACK["isrc"])).json() == {
                    "tracks": []
                }
            before = len(fake.calls)
            assert (await client.get(base + "/catalog/isrc/" + TRACK["isrc"])).status_code == 404
            assert len(fake.calls) == before
            service.catalog.isrc_failures["invented"] = (3, time.monotonic() - 1)
            fake.rows = [TRACK]
            assert (await client.get(base + "/catalog/isrc/" + TRACK["isrc"])).status_code == 200


async def test_management_surface_sessions_and_readonly_overlay(tmp_path, monkeypatch):
    async with setup(tmp_path, login=False) as (client, service, fake, app):
        for path in ("/", "/links", "/extensions/invented"):
            assert (await client.get(path)).status_code == 303
        assert not app.state.web.auth.sessions
        page = await client.get("/login")
        assert len(app.state.web.auth.sessions) == 1
        assert (
            await client.post("/login", data={"csrf": csrf(page), "password": PASSWORD})
        ).status_code == 303
        page = await client.get("/links")
        assert "Effective default format" in page.text and "Native" in page.text
        original = (tmp_path / "config.toml").read_bytes()
        (tmp_path / "config.toml").chmod(0o400)
        service.config.persist(registries=[], catalogs={"invented": {"key": "newcatalog"}})
        assert (tmp_path / "config.toml").read_bytes() == original
        loaded = Config.load(tmp_path / "config.toml")
        assert loaded.registries == [] and loaded.catalogs["invented"].key == "newcatalog"
        assert (service.config.state_dir / "settings.toml").stat().st_mode & 0o777 == 0o600

        def fail(*args, **kwargs):
            raise OSError("invented private detail")

        monkeypatch.setattr("mornye_bridge.config.tempfile.mkstemp", fail)
        with pytest.raises(BridgeError, match="settings_save_failed"):
            service.config.persist(registries=["https://example.test"])
        api = create_app(service, surface="api", manage_lifespan=False)
        management = create_app(service, surface="management", manage_lifespan=False)
        for surface, page_status, addon_status in ((api, 404, 200), (management, 200, 404)):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=surface), base_url="http://bridge"
            ) as separate:
                assert (await separate.get("/login")).status_code == page_status
                assert (
                    await separate.get(f"/addons/{service.access_token}/invented/manifest.json")
                ).status_code == addon_status
        assert (await client.get("/addons/caf%C3%A9/invented/manifest.json")).status_code == 404
        assert (
            await client.post(
                "/management", headers={"authorization": "Bearer caf\xe9".encode("latin1")}
            )
        ).status_code == 404


async def test_token_rotation_and_startup_temp_cleanup(config):
    config.initialize()
    (config.state_dir / "temp" / "old-private-file").write_text("invented")
    service = Service(config, FakeWorker())
    await service.start()
    try:
        old = (service.access_token, service.admin_token)
        assert list((config.state_dir / "temp").iterdir()) == []
        assert (await service.execute("rotate-tokens", {}))["rotated"]
        assert old[0] != service.access_token and old[1] != service.admin_token
        for name in ("access", "admin"):
            assert (config.state_dir / (name + ".token")).stat().st_mode & 0o777 == 0o600
        assert parser().parse_args(["rotate-tokens"]).command == "rotate-tokens"
    finally:
        await service.close()


@pytest.mark.parametrize("kind", ["boolean", "number", "select"])
def test_secret_flag_does_not_change_nontext_controls(kind):
    field = {"key": "flag", "type": kind, "secret": True, "options": ["one", "two"]}
    view = fields_view([field], {"flag": "one"})[0]
    assert not view["secret"] and view.get("options") == ["one", "two"]
    value = {"boolean": "true", "number": "4", "select": "two"}[kind]
    parsed = parse_values([field], {"value:flag": value, "clear:flag": "yes"})
    assert parsed["flag"] == {"boolean": True, "number": 4, "select": "two"}[kind]


def test_native_answer_errors_and_loose_readiness_matching():
    with pytest.raises(BridgeError) as bad:
        map_item(TRACK | {"id": "x" * 191}, "track")
    assert bad.value.status == 502
    assert (
        native_error(
            {"error_type": "network", "title": "Not playable", "error": "Login required"}
        ).status
        == 502
    )
    assert native_error({"error_type": "not_found", "title": "Authentication"}).status == 404
    assert native_error({"code": "ERROR", "message": "Track not found"}).status == 404
    assert (
        native_error({"code": "ERROR", "message": 'Lookup failed for "Not playable"'}).status == 502
    )
    assert (
        native_error(
            {"code": "ERROR", "title": "Not playable", "message": "Lookup failed for Not playable"}
        ).status
        == 502
    )
    assert (
        native_error({"error_type": "extension_error", "title": "Cancelled, not playable"}).status
        == 502
    )
    track = map_item(TRACK | {"name": "Caf\xe9", "artists": "Invented Artist feat. Guest"}, "track")
    assert Service.matches(
        track, {"title": "CAFE", "artist": "invented artist & guest", "durationMs": "1000"}
    )
    assert not Service.matches(
        track, {"title": "Different", "artist": "Guest", "durationMs": "1000"}
    )


async def test_bounded_shutdown_kills_worker_before_removing_staging(config):
    class SlowCancel(FakeWorker):
        def __init__(self):
            super().__init__()
            self.release.clear()
            self.stopping = asyncio.Event()
            self.closed = False

        async def call(self, method, args=None):
            if method == "downloadByStrategy":
                self.directory = Path(json.loads(args)["output_dir"])
                self.started.set()
                try:
                    await self.release.wait()
                except asyncio.CancelledError:
                    self.stopping.set()
                    await self.release.wait()
                assert self.closed
                raise asyncio.CancelledError
            return await super().call(method, args)

        async def close(self):
            assert self.directory.exists()
            self.closed = True
            self.release.set()

    config.shutdown_timeout = 0.03
    fake = SlowCancel()
    service = Service(config, fake)
    await service.start()
    task = service.begin(EXT, "one", "native-aac")
    await fake.started.wait()
    before = time.monotonic()
    await service.close()
    assert time.monotonic() - before < 0.3
    assert task.cancelled() and fake.closed
    assert list((config.state_dir / "jobs").iterdir()) == []


async def test_non_download_cancel_returns_immediately(config):
    config.cancel_timeout = 0.2
    host = pipe_worker(config)
    try:
        await host.call("echo")
        task = asyncio.create_task(host.call("hang"))
        await asyncio.sleep(0.02)
        before = time.monotonic()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert time.monotonic() - before < 0.08
        assert await host.call("echo", "other") == "other"
        await asyncio.gather(*host.drainers)
        assert host.restarts == 1
    finally:
        await host.close()


def test_progress_deltas_are_item_scoped_and_heartbeats_do_not_extend(config):
    from mornye_bridge.supervisor import Supervisor

    host = Supervisor(config)
    host.downloads["one"] = (1, {"bytes_received": 20, "progress": 0.2})
    host._progress({"kind": 10, "value": {"items": {"other": {"bytes_received": 100}}}})
    assert host.downloads["one"][0] == 1
    host._progress({"kind": 10, "value": {"items": {"one": {"speed_mbps": 5}}}})
    assert host.downloads["one"][0] == 1
    host._progress({"kind": 10, "value": {"items": {"one": {"bytes_received": 30}}}})
    assert host.downloads["one"][0] > 1 and host.downloads["one"][1]["progress"] == 0.2


async def test_install_package_cleanup_even_after_failure(config):
    class Package(FakeWorker):
        failed = False

        async def call(self, method, args=None):
            if method == "downloadRepoExtension":
                self.package = config.state_dir / "repo" / "invented.package"
                self.package.write_bytes(b"invented package")
                return str(self.package)
            if method == "loadExtensionFromPath":
                assert self.package.exists()
                if self.failed:
                    raise BridgeError("extension_failed", 502)
                return EXT
            return await super().call(method, args)

    fake = Package()
    service = Service(config, fake)
    await service.start()
    try:
        await service.execute("install", {"extension": "invented"})
        assert not fake.package.exists()
        fake.failed = True
        with pytest.raises(BridgeError):
            await service.execute("install", {"extension": "invented"})
        assert not fake.package.exists()
    finally:
        await service.close()


async def test_login_session_cap_returns_429_without_extra_session(tmp_path):
    async with setup(tmp_path, login=False) as (client, service, fake, app):
        for _ in range(512):
            app.state.web.auth.create()
        assert (await client.get("/links")).status_code == 303
        assert (await client.get("/login")).status_code == 429
        assert len(app.state.web.auth.sessions) == 512


def test_build_wrapper_uses_pinned_path_tools(tmp_path):
    import os
    import subprocess

    tools = tmp_path / "bin"
    tools.mkdir()
    commit = "c90ec6478ad3e808ef22ba9464f8ad05054dde7b"
    scripts = {
        "rustc": '#!/bin/sh\necho "rustc 1.99.0 (invented)"\n',
        "cargo": '#!/bin/sh\nif [ "$1" = --version ]; then echo "cargo 1.99.0 (invented)"; else\n'
        'mkdir -p "$CARGO_TARGET_DIR/release"\n'
        'echo "$*" > "$CARGO_TARGET_DIR/../cargo-args"\n'
        'touch "$CARGO_TARGET_DIR/release/libmornye_desktop.dylib"\n'
        'touch "$CARGO_TARGET_DIR/release/libmornye_desktop.so"\nfi\n',
        "git": f'#!/bin/sh\ncase "$*" in *rev-parse*) echo {commit} ;; esac\n',
        "rustup": '#!/bin/sh\necho "unexpected rustup" >&2\nexit 1\n',
        "curl": '#!/bin/sh\necho "unexpected curl" >&2\nexit 1\n',
    }
    for name, script in scripts.items():
        target = tools / name
        target.write_text(script)
        target.chmod(0o700)
    build = tmp_path / "build"
    subprocess.run(
        ["sh", "tools/build-host.sh", str(build)],
        check=True,
        capture_output=True,
        env=os.environ | {"PATH": str(tools) + os.pathsep + os.defpath},
    )
    assert not (build / "rustup").exists()
    assert (build / "cargo-args").read_text().startswith("build --release --locked")


async def test_repeated_cancel_preserves_bounded_native_cleanup(config):
    from mornye_bridge.supervisor import Supervisor

    class Cleanup(Supervisor):
        async def _cancel_native(self, ident, future, process, item_id=""):
            await asyncio.sleep(0.03)
            future.set_result("drained")

    host = Cleanup(config)
    future = asyncio.get_running_loop().create_future()
    task = asyncio.create_task(host._cancel(1, future, None, "invented"))
    await asyncio.sleep(0.005)
    task.cancel()
    await asyncio.sleep(0.005)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert future.result() == "drained" and host.restarts == 0


async def test_old_installed_reply_cannot_repopulate_invalidated_cache(config):
    class Delayed(FakeWorker):
        async def call(self, method, args=None):
            if method == "getInstalledExtensions" and not self.started.is_set():
                self.started.set()
                await self.release.wait()
                return [EXT]
            return await super().call(method, args)

    fake = Delayed()
    fake.release.clear()
    service = Service(config, fake)
    reading = asyncio.create_task(service.management.installed())
    await fake.started.wait()
    service.management.invalidate()
    fake.enabled = False
    fake.release.set()
    await reading
    assert not (await service.management.installed())[0]["enabled"]
