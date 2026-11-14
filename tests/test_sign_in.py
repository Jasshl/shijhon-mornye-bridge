from __future__ import annotations

import copy
import json
import re
import zipfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from conftest import EXT, FakeWorker
from test_web import REGISTRY, csrf, setup

from mornye_bridge import service as service_module
from mornye_bridge.errors import BridgeError
from mornye_bridge.service import Service, login_action
from mornye_bridge.updates import installed_manifest, keeps_sign_in, package_manifest

SESSION = {
    "namespace": "invented-v1",
    "baseUrl": "https://gateway.example.test/v1",
    "appVersion": "invented@1.0",
    "platform": "extension",
}


def native_manifest(version: str = "1.0", **changes: Any) -> dict[str, Any]:
    manifest: dict[str, Any] = {
        "name": "invented",
        "displayName": "Invented Music",
        "version": version,
        "permissions": {"storage": True},
        "settings": [
            {"key": "region", "type": "string"},
            {"key": "accountToken", "type": "string", "secret": True},
        ],
        "signedSession": dict(SESSION),
    }
    manifest.update(changes)
    return manifest


def install_manifest(state: Path, manifest: dict[str, Any]) -> None:
    directory = state / "extensions" / manifest["name"]
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "manifest.json").write_text(json.dumps(manifest))


def package(path: Path, manifest: dict[str, Any]) -> Path:
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("manifest.json", json.dumps(manifest))
        archive.writestr("index.js", "// invented")
    return path


RECORD = "invented-v1-4d6ce8570e3fd736.json"


def write_record(state: Path, expires: datetime | None, *, held: bool = True) -> Path:
    path = state / "data" / "signed_sessions" / RECORD
    path.parent.mkdir(parents=True, exist_ok=True)
    record: dict[str, Any] = {"install_id": "0" * 32, "namespace": "invented-v1"}
    if held:
        record |= {"session_id": "private-session", "session_secret": "private-secret"}
    if expires:
        record["expires_at"] = expires.isoformat().replace("+00:00", "Z")
    path.write_text(json.dumps(record))
    return path


def expires_in(seconds: float) -> datetime:
    return (datetime.now(UTC) + timedelta(seconds=seconds)).replace(microsecond=0)


class AccountWorker(FakeWorker):
    """A fake native host with one extension, its registry offer and its sign-in."""

    def __init__(self, state: Path) -> None:
        super().__init__()
        self.state = state
        self.extension = copy.deepcopy(EXT)
        self.pending = False
        self.offer: dict[str, Any] | None = None
        self.offered: dict[str, Any] = native_manifest("2.0")
        self.download_answer: dict[str, Any] = {
            "success": False,
            "error_type": "verification_required",
        }
        self.action_answer: Any = {"success": True}

    async def call(self, method: str, args: Any = None) -> Any:
        if method == "getInstalledExtensions":
            self.calls.append((method, args))
            return [self.extension | {"enabled": self.enabled}]
        if method == "getExtensionPendingAuth":
            self.calls.append((method, args))
            return (
                {"auth_url": "https://gateway.example.test/verify?state=private"}
                if self.pending
                else ""
            )
        if method == "checkExtensionHealth":
            self.calls.append((method, args))
            return {"status": "online"}
        if method == "downloadByStrategy":
            self.calls.append((method, args))
            return self.download_answer
        if method == "getRepoRegistryUrl":
            return REGISTRY
        if method in {"setRepoRegistryUrl", "clearRepoRegistryUrl"}:
            return None
        if method == "getRepoExtensions":
            self.calls.append((method, args))
            return [self.offer] if self.offer else []
        if method == "downloadRepoExtension":
            self.calls.append((method, args))
            return str(package(self.state / "repo" / "invented.package", self.offered))
        if method == "upgradeExtension":
            self.calls.append((method, args))
            assert Path(args["file_path"]).exists()
            install_manifest(self.state, self.offered)
            self.extension["version"] = self.offered["version"]
            return self.extension
        if method == "invokeExtensionAction" and args["action"] not in {
            "customSearch",
            "getTrack",
            "getAlbum",
            "getArtist",
        }:
            self.calls.append((method, args))
            if isinstance(self.action_answer, Exception):
                raise self.action_answer
            return self.action_answer
        return await super().call(method, args)

    def called(self, method: str, action: str = "") -> int:
        return sum(
            1
            for name, args in self.calls
            if name == method and (not action or (args or {}).get("action") == action)
        )


async def started(config) -> tuple[Service, AccountWorker]:
    worker = AccountWorker(config.state_dir)
    service = Service(config, worker)
    await service.start()
    service.start_maintenance()
    return service, worker


async def settle(service: Service) -> None:
    for task in list(service.sign_in_tasks.values()):
        await task


def test_update_keeps_sign_in_only_when_the_host_keeps_it():
    old = native_manifest()
    assert keeps_sign_in(old, native_manifest("2.0"))
    # The host files a signed session by these values: any change starts a new one.
    for key, value in [("appVersion", "invented@2.0"), ("namespace", "other"), ("platform", "x")]:
        changed = native_manifest("2.0", signedSession=dict(SESSION, **{key: value}))
        assert not keeps_sign_in(old, changed)
    assert not keeps_sign_in(old, native_manifest("2.0", signedSession=None))
    assert not keeps_sign_in(native_manifest(signedSession=None), native_manifest("2.0"))
    assert not keeps_sign_in(old, native_manifest("2.0", permissions={"storage": False}))
    assert not keeps_sign_in(old, native_manifest("2.0", settings=[{"key": "region"}]))
    plain = native_manifest(signedSession=None)
    assert keeps_sign_in(plain, native_manifest("2.0", signedSession=None))
    assert not keeps_sign_in({}, native_manifest("2.0"))
    assert not keeps_sign_in(old, native_manifest("2.0", name="another"))


def test_manifests_are_read_as_bounded_plain_files(tmp_path):
    install_manifest(tmp_path, native_manifest())
    assert installed_manifest(tmp_path / "extensions", "invented")["version"] == "1.0"
    assert installed_manifest(tmp_path / "extensions", "../invented") == {}
    assert installed_manifest(tmp_path / "extensions", "missing") == {}
    assert (
        package_manifest(package(tmp_path / "one.package", native_manifest("2.0")))["version"]
        == "2.0"
    )
    (tmp_path / "broken.package").write_bytes(b"not a zip archive")
    assert package_manifest(tmp_path / "broken.package") == {}
    with zipfile.ZipFile(tmp_path / "nested.package", "w") as archive:
        archive.writestr("inner/manifest.json", json.dumps(native_manifest()))
    assert package_manifest(tmp_path / "nested.package") == {}


def test_only_sign_in_actions_run_automatically():
    def extension(*fields):
        return {"settings": list(fields)}

    assert login_action(extension({"key": "login", "action": "login"})) == "login"
    assert login_action(extension({"key": "x", "label": "Sign in again", "action": "go"})) == "go"
    assert login_action(extension({"key": "logout", "label": "Log out", "action": "logout"})) == ""
    assert login_action(extension({"key": "clear_auth", "action": "clearAuth"})) == ""
    assert login_action(extension({"key": "disconnect", "action": "disconnect"})) == ""
    assert login_action(extension({"key": "cache", "label": "Clear cache", "action": "x"})) == ""
    assert login_action(extension({"key": "login", "action": "bad action"})) == ""
    assert (
        login_action(
            extension({"key": "refresh", "label": "Refresh catalog", "action": "refreshCatalog"})
        )
        == ""
    )
    assert login_action(extension({"key": "x", "label": "Refresh session", "action": "x"})) == "x"


async def test_missing_sign_in_survives_unrelated_success_and_restart(config):
    service, worker = await started(config)
    management = service.management
    management.manual.add("invented")
    try:
        with pytest.raises(BridgeError) as failed:
            await management.call("downloadByStrategy", "{}", "invented")
        assert failed.value.code == "needs_login_or_verification"
        # A catalog search can work without the account a download needs.
        await management.raw("invented", "customSearch", ["x", {"filter": "tracks"}])
        assert management.states["invented"] == "needs_login_or_verification"
        await service.execute("health", {"extension": "invented"})
        assert management.states["invented"] == "needs_login_or_verification"
    finally:
        await service.close()
    saved = json.loads((config.state_dir / "sign-in.json").read_text())
    assert saved == {
        "invented": {
            "code": "needs_login_or_verification",
            "source": "downloadByStrategy",
            "expires": None,
        }
    }
    service, worker = await started(config)
    management = service.management
    try:
        assert management.states["invented"] == "needs_login_or_verification"
        worker.download_answer = {"success": True}
        await management.call("downloadByStrategy", "{}", "invented")
        assert "invented" not in management.states
        assert not (config.state_dir / "sign-in.json").exists()
    finally:
        await service.close()


async def test_signed_session_signs_in_again_without_a_browser_when_possible(config):
    install_manifest(config.state_dir, native_manifest())
    write_record(config.state_dir, expires_in(600))
    service, worker = await started(config)
    try:
        service.management.needs_sign_in("invented", "downloadByStrategy")
        # The gateway issued a new session at the host's check: it expires later.
        write_record(config.state_dir, expires_in(7200))
        await settle(service)
        assert worker.called("getExtensionPendingAuth") == 1
        assert "invented" not in service.management.states
        assert worker.called("checkExtensionHealth") == 1

        # Not more than one automatic attempt per ten minutes, even after a success.
        service.management.needs_sign_in("invented", "downloadByStrategy")
        assert not service.sign_in_tasks
        service.sign_in_next.clear()
        worker.pending = True
        service.management.needs_sign_in("invented", "downloadByStrategy")
        await settle(service)
        assert service.management.states["invented"] == "needs_login_or_verification"
        assert "invented" in service.management.manual
        # A browser step is needed: nothing is retried automatically until it is done.
        before = len(worker.calls)
        service.management.needs_sign_in("invented", "downloadByStrategy")
        assert not service.sign_in_tasks and len(worker.calls) == before
        # The challenge was answered: a new session expires later.
        worker.pending = False
        write_record(config.state_dir, expires_in(9000))
        await service.execute("pending-auth.show", {"extension": "invented"})
        assert "invented" not in service.management.manual
        assert "invented" not in service.management.states
    finally:
        await service.close()


async def test_declared_sign_in_action_runs_once_with_backoff(config, monkeypatch):
    service, worker = await started(config)
    worker.extension["settings"] = [
        {"key": "logout", "type": "button", "label": "Log out", "action": "logout"},
        {"key": "renew", "type": "button", "label": "Sign in again", "action": "renewLogin"},
    ]
    now = [1000.0]
    monkeypatch.setattr(service_module.time, "monotonic", lambda: now[0])
    try:
        worker.action_answer = BridgeError("extension_failed", 502)
        service.management.needs_sign_in("invented", "downloadByStrategy")
        await settle(service)
        assert worker.called("invokeExtensionAction", "renewLogin") == 1
        assert service.sign_in_next["invented"] == 1600.0
        now[0] = 1599.0
        service.management.needs_sign_in("invented", "downloadByStrategy")
        assert not service.sign_in_tasks
        now[0] = 1600.0
        service.management.needs_sign_in("invented", "downloadByStrategy")
        await settle(service)
        assert worker.called("invokeExtensionAction", "renewLogin") == 2
        # Failures in a row double the wait.
        assert service.sign_in_next["invented"] == 1600.0 + 1200.0

        now[0] = 5000.0
        worker.action_answer = {"success": True, "setting_updates": {"region": "zz"}}
        service.management.needs_sign_in("invented", "downloadByStrategy")
        await settle(service)
        assert "invented" not in service.management.states
        assert worker.settings["region"] == "zz"

        now[0] = 9000.0
        worker.action_answer = {
            "success": True,
            "action_form": {
                "version": 1,
                "submit_action": "submitCode",
                "fields": [{"key": "code", "type": "otp"}],
            },
        }
        service.management.needs_sign_in("invented", "downloadByStrategy")
        await settle(service)
        assert service.management.states["invented"] == "needs_login_or_verification"
        assert "invented" in service.management.manual
        assert worker.called("invokeExtensionAction", "logout") == 0
        assert worker.called("invokeExtensionAction", "submitCode") == 0
    finally:
        await service.close()


async def test_extension_without_automatic_sign_in_is_left_for_the_page(config):
    service, worker = await started(config)
    try:
        service.management.needs_sign_in("invented", "downloadByStrategy")
        await settle(service)
        assert "invented" in service.management.manual
        assert worker.called("invokeExtensionAction") == 0
    finally:
        await service.close()


async def test_update_that_keeps_sign_in_is_installed_and_rechecked(config):
    install_manifest(config.state_dir, native_manifest())
    service, worker = await started(config)
    service.config.persist(registries=[REGISTRY])
    worker.offer = {"id": "invented", "version": "2.0", "is_installed": True, "has_update": True}
    try:
        assert await service.check_updates() == service_module.UPDATE_CHECK_SECONDS
        assert worker.called("upgradeExtension") == 1
        assert service.updates["invented"]["state"] == "installed"
        assert not (config.state_dir / "repo" / "invented.package").exists()
        await service.maintenance[-1]
        assert worker.called("checkExtensionHealth") == 1
        assert worker.called("getExtensionPendingAuth") == 1
    finally:
        await service.close()


async def test_update_that_signs_out_waits_for_the_next_sign_in(config):
    install_manifest(config.state_dir, native_manifest())
    service, worker = await started(config)
    service.config.persist(registries=[REGISTRY])
    worker.offer = {"id": "invented", "version": "2.0", "is_installed": True, "has_update": True}
    worker.offered = native_manifest("2.0", signedSession=dict(SESSION, appVersion="invented@2.0"))
    try:
        await service.check_updates()
        assert worker.called("upgradeExtension") == 0
        assert service.updates["invented"]["state"] == "waiting"
        # Known to sign out: not downloaded again on the next check.
        await service.check_updates()
        assert worker.called("downloadRepoExtension") == 1

        worker.pending = True
        service.management.needs_sign_in("invented", "downloadByStrategy")
        await settle(service)
        assert worker.called("upgradeExtension") == 1
        assert service.updates["invented"]["state"] == "installed"
        upgrade = next(i for i, call in enumerate(worker.calls) if call[0] == "upgradeExtension")
        last_check = max(
            i for i, call in enumerate(worker.calls) if call[0] == "getExtensionPendingAuth"
        )
        assert upgrade < last_check
        assert service.management.states["invented"] == "needs_login_or_verification"
    finally:
        await service.close()


async def test_updates_wait_for_preparations_and_follow_the_setting(config):
    install_manifest(config.state_dir, native_manifest())
    service, worker = await started(config)
    service.config.persist(registries=[REGISTRY])
    worker.offer = {"id": "invented", "version": "2.0", "is_installed": True, "has_update": True}
    try:
        service.jobs[("invented", "track", "native-aac")] = None  # type: ignore[assignment]
        assert await service.check_updates() == service_module.UPDATE_BUSY_RETRY
        assert service.updates["invented"]["state"] == "busy"
        # Nothing is downloaded or installed while the extension prepares a file.
        assert worker.called("downloadRepoExtension") == 0
        assert worker.called("upgradeExtension") == 0
        assert not (config.state_dir / "repo" / "invented.package").exists()
        service.jobs.clear()
        downloads = worker.called("downloadRepoExtension")
        service.config.persist(auto_update=False)
        await service.check_updates()
        assert worker.called("downloadRepoExtension") == downloads
        assert "auto_update = false" in (config.state_dir / "settings.toml").read_text()
    finally:
        await service.close()


async def test_session_check_notices_a_lapsed_signed_session(config):
    install_manifest(config.state_dir, native_manifest())
    service, worker = await started(config)
    try:
        await service.check_sessions()
        assert "invented" not in service.management.states
        worker.pending = True
        await service.check_sessions()
        await settle(service)
        assert service.management.states["invented"] == "needs_login_or_verification"
        assert "invented" in service.management.manual
    finally:
        await service.close()


async def test_sign_in_page_walks_through_and_returns_to_the_list(tmp_path):
    async with setup(tmp_path) as (client, service, worker, app):
        install_manifest(service.config.state_dir, native_manifest())
        service.management.manual.add("invented")
        service.management.needs_sign_in("invented", "downloadByStrategy")
        page = await client.get("/")
        assert "1 extension needs signing in" in page.text and 'href="/sign-in"' in page.text
        listing = await client.get("/sign-in")
        assert "Invented Music" in listing.text and "verification page" in listing.text
        worker.pending = True
        step = await client.get("/sign-in/invented")
        assert step.status_code == 200
        assert "Open provider login" in step.text and "data-pending" in step.text
        assert 'name="return_to" value="sign-in"' in step.text
        assert 'action="/extensions/invented"' in step.text
        assert "private-code" not in step.text and "private" not in step.text.replace(
            "Private management", ""
        )
        response = await client.post(
            "/extensions/invented",
            data={
                "csrf": csrf(step),
                "operation": "grant",
                "return": "private-grant",
                "return_to": "sign-in",
            },
        )
        assert response.status_code == 303
        assert response.headers["location"].endswith("/sign-in/invented")
        done = await client.get("/sign-in/invented")
        assert done.status_code == 303 and done.headers["location"].endswith("/sign-in")
        listing = await client.get("/sign-in")
        assert "Invented Music: Ready." in listing.text
        assert (
            "Nothing needs signing in" in listing.text and "Signed in on this page" in listing.text
        )
        assert "private-grant" not in listing.text


async def test_sign_in_step_installs_a_waiting_update_first(tmp_path):
    async with setup(tmp_path) as (client, service, worker, app):
        service.management.manual.add("invented")
        service.management.needs_sign_in("invented", "downloadByStrategy")
        service.config.persist(registries=[REGISTRY])
        service.updates["invented"] = {"version": "2.0", "registry": REGISTRY, "state": "waiting"}
        worker.pending = True
        package(worker.package, native_manifest("2.0"))
        step = await client.get("/sign-in/invented")
        # Opening the step changes nothing; it offers the update.
        assert not any(method == "upgradeExtension" for method, _ in worker.calls)
        assert "Install version 2.0 first" in step.text
        response = await client.post(
            "/sign-in", data={"csrf": csrf(step), "operation": "update", "extension": "invented"}
        )
        assert response.headers["location"].endswith("/sign-in/invented")
        step = await client.get("/sign-in/invented")
        assert "Updated Invented Music to version" in step.text
        assert any(method == "upgradeExtension" for method, _ in worker.calls)
        assert service.updates["invented"]["state"] == "installed"
        recheck = await client.post(
            "/sign-in", data={"csrf": csrf(step), "operation": "recheck", "extension": "invented"}
        )
        assert recheck.headers["location"].endswith("/sign-in/invented")


async def test_dashboard_shows_updates_and_saves_the_setting(tmp_path):
    async with setup(tmp_path) as (client, service, worker, app):
        page = await client.get("/")
        assert "Update to 2.0 available. It installs automatically" in page.text
        assert "Check for updates now" in page.text
        service.updates["invented"] = {"version": "2.0", "registry": REGISTRY, "state": "waiting"}
        page = await client.get("/")
        assert "waits for the next sign-in" in page.text
        response = await client.post("/", data={"csrf": csrf(page), "operation": "updates.save"})
        assert response.status_code == 303 and service.config.auto_update is False
        page = await client.get("/")
        assert re.search(r"Update to 2.0 available\.</p>", page.text)
        assert "Check for updates now" not in page.text
        await client.post(
            "/", data={"csrf": csrf(page), "operation": "updates.save", "auto_update": "yes"}
        )
        assert service.config.auto_update is True


async def test_removal_forgets_the_sign_in_and_the_automatic_update(config):
    service, worker = await started(config)
    try:
        service.management.manual.add("invented")
        service.management.needs_sign_in("invented", "downloadByStrategy")
        service.updates["invented"] = {"version": "2.0", "registry": REGISTRY, "state": "waiting"}
        await service.execute("remove", {"extension": "invented"})
        assert "invented" not in service.management.states and not service.updates
        assert not (config.state_dir / "sign-in.json").exists()
    finally:
        await service.close()
