from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
from test_sign_in import (
    RECORD,
    SESSION,
    AccountWorker,
    expires_in,
    install_manifest,
    native_manifest,
    settle,
    started,
    write_record,
)
from test_web import REGISTRY, csrf, setup

from mornye_bridge import service as service_module
from mornye_bridge.config import Config
from mornye_bridge.server import create_app
from mornye_bridge.service import Service
from mornye_bridge.sessions import parse_time, record_name, saved_session


class RenewingWorker(AccountWorker):
    """customSearch is a signed request; the fake host renews when the record is due."""

    def __init__(self, state: Path) -> None:
        super().__init__(state)
        self.renews = True
        self.searches: list[Any] = []

    async def call(self, method: str, args: Any = None) -> Any:
        if method == "invokeExtensionAction" and args["action"] == "customSearch":
            self.searches.append(json.loads(args["arguments_json"]))
            if self.renews:
                write_record(self.state, expires_in(7200))
            return []
        return await super().call(method, args)


async def keep_alive_service(config) -> tuple[Service, RenewingWorker]:
    install_manifest(config.state_dir, native_manifest())
    worker = RenewingWorker(config.state_dir)
    service = Service(config, worker)
    await service.start()
    service.start_maintenance()
    return service, worker


def test_session_record_name_and_expiry_follow_the_host(tmp_path):
    # Same scope normalization, defaults and digest as the host's record file names.
    assert record_name(SESSION) == RECORD
    assert (
        record_name(
            {
                "namespace": " Invented-V1 ",
                "baseUrl": "https://Gateway.example.test/v1",
                "appVersion": "invented@1.0",
            }
        )
        == RECORD
    )
    assert record_name({"namespace": "x", "baseUrl": "https://g.example.test"}) == (
        "x-20e52bb3b5935205.json"
    )
    assert record_name({"namespace": "..."}) == ""
    assert parse_time("2026-11-11T10:00:00.123456789Z") == datetime(
        2026, 11, 11, 10, 0, 0, 123456, tzinfo=UTC
    )
    assert parse_time("2026-11-11T12:00:00+02:00") == datetime(2026, 11, 11, 10, tzinfo=UTC)
    assert parse_time("2026-11-11T10:00:00") is None and parse_time("") is None
    later = expires_in(600)
    write_record(tmp_path, later)
    assert saved_session(tmp_path / "data", native_manifest()) == (True, later)
    write_record(tmp_path, None, held=False)
    assert saved_session(tmp_path / "data", native_manifest()) == (False, None)
    assert saved_session(tmp_path / "data", native_manifest(signedSession=None)) is None


async def test_keep_alive_searches_once_inside_the_renewal_window(config):
    service, worker = await keep_alive_service(config)
    try:
        write_record(config.state_dir, expires_in(3 * 3600))
        # Outside the window nothing is sent; sessions are still checked every 30 minutes.
        assert await service.check_sessions() == service_module.SESSION_CHECK_SECONDS
        assert not worker.searches
        # The next look is timed from the expiry: fifty minutes before it.
        write_record(config.state_dir, expires_in(3600 + 600))
        delay = await service.check_sessions()
        assert not worker.searches and 1200 - 5 < delay <= 1200
        write_record(config.state_dir, expires_in(1800))
        await service.check_sessions()
        assert worker.searches == [["music", {"limit": 1, "offset": 0, "filter": "artists"}]]
        info = service.sessions["invented"]
        assert info["renewed"] and parse_time(info["expires"]) > expires_in(7000)
        saved = json.loads((config.state_dir / "sessions.json").read_text())
        assert saved["invented"]["renewed"] == info["renewed"]
        assert "private" not in json.dumps(saved)
        assert not any(name == "downloadByStrategy" for name, _ in worker.calls)
        view = service.session_view("invented")
        assert view and view["keep_alive"] == "on" and view["renewed"]
    finally:
        await service.close()


async def test_extension_whose_searches_do_not_renew_is_left_alone(config, monkeypatch):
    service, worker = await keep_alive_service(config)
    worker.renews = False
    now = [1000.0]
    monkeypatch.setattr(service_module.time, "monotonic", lambda: now[0])
    try:
        write_record(config.state_dir, expires_in(1800))
        await service.check_sessions()
        await service.check_sessions()
        assert len(worker.searches) == 1  # at most one search per twenty minutes
        now[0] += service_module.KEEP_ALIVE_SPACING
        await service.check_sessions()
        assert len(worker.searches) == 2
        assert service.sessions["invented"]["no_keep_alive"] == "1.0"
        now[0] += service_module.KEEP_ALIVE_SPACING
        await service.check_sessions()
        assert len(worker.searches) == 2
        # Renewal by a brief download takes over once a song has been delivered.
        assert service.session_view("invented")["keep_alive"] == "download_later"
        service.config.renew_by_download = False
        assert service.session_view("invented")["keep_alive"] == "none"
        service.config.renew_by_download = True
        # Another version may sign its searches again.
        worker.extension["version"] = "1.1"
        service.management.invalidate()
        worker.renews = True
        await service.check_sessions()
        assert len(worker.searches) == 3 and "no_keep_alive" not in service.sessions["invented"]
    finally:
        await service.close()


async def test_keep_alive_backs_off_and_follows_the_setting(config, monkeypatch):
    service, worker = await keep_alive_service(config)
    now = [1000.0]
    monkeypatch.setattr(service_module.time, "monotonic", lambda: now[0])
    original = worker.call

    async def failing(method, args=None):
        if method == "invokeExtensionAction" and args["action"] == "customSearch":
            worker.searches.append("failed")
            return {"success": False, "error_type": "api_error"}
        return await original(method, args)

    try:
        write_record(config.state_dir, expires_in(3000))
        worker.call = failing
        await service.check_sessions()
        assert service.keep_alive_next["invented"] == 1000.0 + service_module.KEEP_ALIVE_RETRY
        now[0] += service_module.KEEP_ALIVE_RETRY
        await service.check_sessions()
        assert service.keep_alive_next["invented"] == now[0] + 2 * service_module.KEEP_ALIVE_RETRY
        assert len(worker.searches) == 2
        worker.call = original
        service.config.persist(keep_alive=False)
        now[0] += 10 * service_module.KEEP_ALIVE_RETRY
        await service.check_sessions()
        assert len(worker.searches) == 2
        assert service.session_view("invented")["keep_alive"] == "off"
    finally:
        await service.close()


async def test_keep_alive_reports_a_lapsed_session_for_sign_in(config):
    service, worker = await keep_alive_service(config)
    original = worker.call

    async def lapsed(method, args=None):
        if method == "invokeExtensionAction" and args["action"] == "customSearch":
            # The host found the session unusable and opened a new challenge.
            worker.pending = True
            return {"success": False, "error_type": "verification_required"}
        return await original(method, args)

    worker.call = lapsed
    service.management.manual.add("invented")
    try:
        write_record(config.state_dir, expires_in(1800))
        await service.check_sessions()
        await settle(service)
        assert service.management.states["invented"] == "needs_login_or_verification"
    finally:
        await service.close()


async def test_update_all_installs_an_update_that_signs_out(config):
    install_manifest(config.state_dir, native_manifest())
    service, worker = await started(config)
    service.config.persist(registries=[REGISTRY], update_all=True)
    worker.offer = {"id": "invented", "version": "2.0", "is_installed": True, "has_update": True}
    worker.offered = native_manifest("2.0", signedSession=dict(SESSION, appVersion="invented@2"))
    try:
        await service.check_updates()
        assert worker.called("upgradeExtension") == 1
        assert service.updates["invented"]["state"] == "installed"
    finally:
        await service.close()


async def test_add_on_routes_ask_for_sign_in_on_the_bridge_page(tmp_path):
    config = Config(
        library=tmp_path / "unused",
        state_dir=tmp_path / "state",
        management_public_url="https://manage.example.test",
    )
    worker = AccountWorker(config.state_dir)
    worker.action_answer = {"available": False, "reason": "verification_required"}
    service = Service(config, worker)
    app = create_app(service, surface="api")
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://bridge"
        ) as client:
            reply = await client.get(
                f"/addons/{service.access_token}/invented/resolve",
                params={"title": "Invented Song", "artist": "Invented Artist"},
            )
            assert reply.status_code == 503
            assert reply.json() == {
                "error": "needs_sign_in",
                "url": "https://manage.example.test/sign-in",
            }
            assert reply.headers["cache-control"] == "no-store"
            # Other failures keep their codes.
            missing = await client.get(f"/addons/{service.access_token}/missing/manifest.json")
            assert missing.json()["error"] != "needs_sign_in"


async def test_dashboard_shows_the_session_and_saves_the_keep_alive_setting(tmp_path):
    async with setup(tmp_path) as (client, service, worker, app):
        state = service.config.state_dir
        install_manifest(state, native_manifest())
        later = expires_in(5 * 3600)
        write_record(state, later)
        service.sessions["invented"] = {"renewed": "2026-11-11T09:30:00+00:00"}
        page = await client.get("/")
        assert f"Session valid until {later:%Y-%m-%d %H:%M} UTC" in page.text
        assert "last renewed 2026-11-11 09:30 UTC" in page.text
        assert "Kept alive automatically" in page.text and "private-session" not in page.text
        await client.post("/", data={"csrf": csrf(page), "operation": "sessions.save"})
        assert service.config.keep_alive is False
        page = await client.get("/")
        assert "Keep-alive is off, and with it renewal by a brief download." in page.text
        await client.post(
            "/",
            data={
                "csrf": csrf(page),
                "operation": "updates.save",
                "auto_update": "yes",
                "update_all": "yes",
            },
        )
        assert service.config.update_all is True
        page = await client.get("/")
        assert "installs automatically at the next check" in page.text
