"""Regression tests for the independent review of the sign-in, update and keep-alive work."""

from __future__ import annotations

import asyncio
import json
import stat
import zipfile
from typing import Any

import pytest
from test_keep_alive import keep_alive_service
from test_sign_in import (
    SESSION,
    AccountWorker,
    expires_in,
    install_manifest,
    native_manifest,
    settle,
    started,
    write_record,
)
from test_web import REGISTRY, setup

from mornye_bridge import service as service_module
from mornye_bridge import updates
from mornye_bridge.errors import BridgeError
from mornye_bridge.updates import keeps_sign_in, package_manifest

NEEDS = "needs_login_or_verification"


def sign_in_file(config) -> dict[str, Any]:
    return json.loads((config.state_dir / "sign-in.json").read_text())


# P1.1 — another failure never replaces a missing sign-in, in memory or on disk.
@pytest.mark.parametrize(
    "answer",
    [
        {"success": False, "error_type": "not_found"},
        {"success": False, "error_type": "rate_limit", "retry_after_seconds": 5},
        BridgeError("worker_timeout"),
    ],
)
async def test_failure_in_between_keeps_the_missing_sign_in(config, answer):
    service, worker = await started(config)
    management = service.management
    management.manual.add("invented")
    try:
        with pytest.raises(BridgeError):
            await management.call("downloadByStrategy", "{}", "invented")
        if isinstance(answer, Exception):
            original = worker.call

            async def failing(method, args=None):
                if method == "downloadByStrategy":
                    raise answer
                return await original(method, args)

            worker.call = failing
        else:
            worker.download_answer = answer
        with pytest.raises(BridgeError):
            await management.call("downloadByStrategy", "{}", "invented")
        assert management.states["invented"] == NEEDS
        # Any unrelated success: the notice stays, memory and file in step.
        await management.raw("invented", "customSearch", ["x", {"filter": "tracks"}])
        assert management.states["invented"] == NEEDS
        assert sign_in_file(config)["invented"]["source"] == "downloadByStrategy"
        assert management.state("invented") == NEEDS
    finally:
        await service.close()
    service, worker = await started(config)
    try:
        assert service.management.states["invented"] == NEEDS
    finally:
        await service.close()


async def test_other_failures_are_kept_beside_and_cleared_by_success(config):
    service, worker = await started(config)
    try:
        worker.download_answer = {"success": False, "error_type": "api_error"}
        with pytest.raises(BridgeError):
            await service.management.call("downloadByStrategy", "{}", "invented")
        assert service.management.errors["invented"] == "extension_failed"
        assert "invented" not in service.management.states
        assert not (config.state_dir / "sign-in.json").exists()
        await service.management.raw("invented", "customSearch", ["x", {"filter": "tracks"}])
        assert "invented" not in service.management.errors
    finally:
        await service.close()


# P1.2 — the whole signedSession object and the host's load gates.
@pytest.mark.parametrize(
    "change",
    [
        {"endpoints": {"refresh": "/session/renew"}},
        {"headerPrefix": "X-Other-"},
        {"schemeLabel": "OTHER-HMAC-V1"},
        {"timeWindowSeconds": 120},
        {"callbackUrl": "mornye://other"},
        {"unknownKey": True},
        {"baseUrl": "https://gateway.example.test/v1/"},
    ],
)
def test_any_signed_session_difference_defers_the_update(change):
    old = native_manifest(signedSession=dict(SESSION, endpoints={"refresh": "/session/refresh"}))
    same = native_manifest("2.0", signedSession=dict(old["signedSession"]))
    assert keeps_sign_in(old, same)
    changed = dict(old["signedSession"]) | change
    assert not keeps_sign_in(old, native_manifest("2.0", signedSession=changed))
    assert not keeps_sign_in(old, native_manifest("2.0", signedSession="malformed"))


@pytest.mark.parametrize(
    "features,minimum,accepted",
    [
        (["signedSession@3", "sessionGrant@1", "downloadSegments"], "5.2.6", True),
        ([], "5.2", True),
        ([], "", True),
        (["signedSession@4"], "", False),
        (["unknownFeature@1"], "", False),
        (["signedSession@x"], "", False),
        ("signedSession@3", "", False),
        ([3], "", False),
        ([], "9.0", False),
        ([], "5.3.0", False),
        ([], "abc", False),
    ],
)
def test_updates_need_every_host_requirement_met(features, minimum, accepted):
    manifest = native_manifest("2.0", requiredRuntimeFeatures=features, minAppVersion=minimum)
    assert updates.host_accepts(manifest) is accepted
    # Whether the host can load it is judged apart from keeping the sign-in.
    assert keeps_sign_in(native_manifest(), manifest)


def test_package_reader_accepts_the_root_layout_the_host_reads(tmp_path):
    # Real registry packages carry manifest.json and index.js at the archive root with
    # license files beside them; the host also cleans "./manifest.json" to the root.
    for name in ("manifest.json", "./manifest.json"):
        path = tmp_path / "invented.sflx"
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr(name, json.dumps(native_manifest("2.0")))
            for extra in ("index.js", "LICENSE", "NOTICE", "COPYING"):
                archive.writestr(extra, "invented")
        assert package_manifest(path)["version"] == "2.0"
    with zipfile.ZipFile(tmp_path / "two.sflx", "w") as archive:
        archive.writestr("manifest.json", "{}")
        archive.writestr("./manifest.json", "{}")
    assert package_manifest(tmp_path / "two.sflx") == {}


# P2.3 — only the sign-in action (and the form steps it starts) clears the notice.
async def test_unrelated_action_does_not_clear_the_notice(config):
    service, worker = await started(config)
    worker.extension["settings"] = [
        {"key": "rebuild", "type": "button", "label": "Rebuild index", "action": "rebuildIndex"},
        {"key": "login", "type": "button", "label": "Log in", "action": "login"},
    ]
    service.management.manual.add("invented")
    try:
        service.management.needs_sign_in("invented", "downloadByStrategy")
        await service.execute("action", {"extension": "invented", "action": "rebuildIndex"})
        assert service.management.states["invented"] == NEEDS
        worker.action_answer = {
            "success": True,
            "action_form": {
                "version": 1,
                "submit_action": "submitCode",
                "fields": [{"key": "code", "type": "otp"}],
            },
        }
        await service.execute("action", {"extension": "invented", "action": "login"})
        assert service.management.states["invented"] == NEEDS
        worker.action_answer = {"success": True}
        await service.execute(
            "action", {"extension": "invented", "action": "submitCode", "arguments": [{}]}
        )
        assert "invented" not in service.management.states
    finally:
        await service.close()


# P2.4 — no challenge is not proof for a signed session.
async def test_empty_challenge_answer_does_not_clear_a_signed_session_notice(config):
    install_manifest(config.state_dir, native_manifest())
    write_record(config.state_dir, expires_in(3000))
    service, worker = await started(config)
    try:
        service.management.needs_sign_in("invented", "downloadByStrategy")
        await settle(service)
        assert service.management.states["invented"] == NEEDS
        assert "invented" in service.management.manual
        await service.execute("pending-auth.show", {"extension": "invented"})
        assert service.management.states["invented"] == NEEDS
        # The session expires later than at the report: a renewed or new session.
        write_record(config.state_dir, expires_in(9000))
        await service.execute("pending-auth.show", {"extension": "invented"})
        assert "invented" not in service.management.states
        # A notice from the check itself is cleared by a later empty answer.
        worker.pending = True
        await service.execute("pending-auth.show", {"extension": "invented"})
        assert service.management.sources["invented"] == "getExtensionPendingAuth"
        worker.pending = False
        await service.execute("pending-auth.show", {"extension": "invented"})
        assert "invented" not in service.management.states
    finally:
        await service.close()


async def test_page_view_does_not_clear_an_unconfirmed_notice(tmp_path):
    async with setup(tmp_path) as (client, service, worker, app):
        install_manifest(service.config.state_dir, native_manifest())
        write_record(service.config.state_dir, expires_in(3000))
        service.management.manual.add("invented")
        service.management.needs_sign_in("invented", "downloadByStrategy")
        await client.get("/extensions/invented")
        await client.get("/sign-in/invented")
        assert service.management.states["invented"] == NEEDS


# P2.5 — only an explicit success is a finished download.
@pytest.mark.parametrize("answer", [{}, {"file_path": "x"}, None, {"success": "yes"}])
async def test_download_without_explicit_success_is_a_failure(config, answer):
    service, worker = await started(config)
    service.management.manual.add("invented")
    try:
        with pytest.raises(BridgeError):
            await service.management.call("downloadByStrategy", "{}", "invented")
        worker.download_answer = answer
        with pytest.raises(BridgeError):
            await service.management.call("downloadByStrategy", "{}", "invented")
        assert service.management.states["invented"] == NEEDS
    finally:
        await service.close()


# P2.6 — automatic sign-in first; a deferred sign-out update only after a confirmed sign-out.
async def deferred_update(config) -> tuple[Any, AccountWorker]:
    install_manifest(config.state_dir, native_manifest())
    write_record(config.state_dir, expires_in(3000))
    service, worker = await started(config)
    service.config.persist(registries=[REGISTRY])
    worker.offer = {"id": "invented", "version": "2.0", "is_installed": True, "has_update": True}
    worker.offered = native_manifest("2.0", signedSession=dict(SESSION, appVersion="invented@2"))
    await service.check_updates()
    assert service.updates["invented"]["state"] == "waiting"
    return service, worker


async def test_deferred_update_waits_when_automatic_sign_in_works(config):
    service, worker = await deferred_update(config)
    try:
        service.management.needs_sign_in("invented", "downloadByStrategy")
        write_record(config.state_dir, expires_in(9000))  # the gateway issued a new session
        await settle(service)
        assert "invented" not in service.management.states
        assert worker.called("upgradeExtension") == 0
    finally:
        await service.close()


async def test_deferred_update_installs_after_a_challenge_is_shown(config):
    service, worker = await deferred_update(config)
    try:
        worker.pending = True
        service.management.needs_sign_in("invented", "downloadByStrategy")
        await settle(service)
        names = [name for name, _ in worker.calls]
        assert worker.called("upgradeExtension") == 1
        assert names.index("getExtensionPendingAuth") < names.index("upgradeExtension")
    finally:
        await service.close()


async def test_single_report_alone_does_not_install_the_deferred_update(config):
    service, worker = await deferred_update(config)
    try:
        service.management.manual.add("invented")
        service.management.needs_sign_in("invented", "downloadByStrategy", "unauthorized")
        await settle(service)
        assert worker.called("upgradeExtension") == 0
        # The daily check never installs it either while the notice stands.
        await service.check_updates()
        assert worker.called("upgradeExtension") == 0
    finally:
        await service.close()


# P2.7 — keep-alive misses count per renewal window.
async def test_keep_alive_misses_reset_when_the_expiry_moves(config, monkeypatch):
    service, worker = await keep_alive_service(config)
    worker.renews = False
    now = [1000.0]
    monkeypatch.setattr(service_module.time, "monotonic", lambda: now[0])
    try:
        write_record(config.state_dir, expires_in(1800))
        await service.check_sessions()
        assert len(worker.searches) == 1
        # A download renewed the session meanwhile: a new window.
        write_record(config.state_dir, expires_in(2400))
        now[0] += service_module.KEEP_ALIVE_SPACING
        await service.check_sessions()
        assert len(worker.searches) == 2
        assert "no_keep_alive" not in service.sessions["invented"]
    finally:
        await service.close()


async def test_failed_keep_alive_search_is_not_a_miss(config, monkeypatch):
    service, worker = await keep_alive_service(config)
    worker.renews = False
    now = [1000.0]
    monkeypatch.setattr(service_module.time, "monotonic", lambda: now[0])
    original = worker.call
    failing = [True]

    async def call(method, args=None):
        if method == "invokeExtensionAction" and args["action"] == "customSearch" and failing[0]:
            failing[0] = False
            return {"success": False, "error_type": "api_error"}
        return await original(method, args)

    worker.call = call
    try:
        write_record(config.state_dir, expires_in(3000))
        await service.check_sessions()
        now[0] += service_module.KEEP_ALIVE_RETRY
        await service.check_sessions()
        assert len(worker.searches) == 1
        assert "no_keep_alive" not in service.sessions["invented"]
    finally:
        await service.close()


# P3.9 — the internal update step is not reachable as an operation.
async def test_update_step_is_not_a_management_operation(config):
    install_manifest(config.state_dir, native_manifest())
    service, worker = await started(config)
    service.config.persist(registries=[REGISTRY])
    try:
        for operation in ("update-checked", "auto-update"):
            with pytest.raises(BridgeError):
                await service.execute(
                    operation, {"extension": "invented", "registry": REGISTRY, "all": True}
                )
        assert worker.called("downloadRepoExtension") == 0
    finally:
        await service.close()


# P3.10 — shutdown waits for an automatic upgrade; leftover packages are swept.
async def test_shutdown_waits_for_an_automatic_upgrade(config):
    install_manifest(config.state_dir, native_manifest())
    service, worker = await started(config)
    service.config.persist(registries=[REGISTRY])
    worker.offer = {"id": "invented", "version": "2.0", "is_installed": True, "has_update": True}
    entered, release, finished = asyncio.Event(), asyncio.Event(), asyncio.Event()
    original = worker.call

    async def slow(method, args=None):
        if method == "upgradeExtension":
            entered.set()
            await release.wait()
            result = await original(method, args)
            finished.set()
            return result
        return await original(method, args)

    worker.call = slow
    service.check_updates_soon()
    await asyncio.wait_for(entered.wait(), 2)
    closing = asyncio.create_task(service.close())
    await asyncio.sleep(0.05)
    assert not closing.done()
    release.set()
    await asyncio.wait_for(closing, 2)
    assert finished.is_set()
    assert not (config.state_dir / "repo" / "invented.package").exists()


async def test_start_sweeps_leftover_packages_but_keeps_the_registry_cache(config):
    config.initialize()
    repo = config.state_dir / "repo"
    for name in ("old.sflx", "old.mornye-ext"):
        (repo / name).write_bytes(b"left over")
    (repo / "store_cache.json").write_text("{}")
    (repo / ".backend-repository.lock").write_text("")
    service, worker = await started(config)
    try:
        assert sorted(path.name for path in repo.iterdir()) == [
            ".backend-repository.lock",
            "store_cache.json",
        ]
    finally:
        await service.close()


async def test_upgrade_outlives_the_ordinary_call_deadline(config):
    import sys
    from pathlib import Path

    from mornye_bridge.supervisor import Supervisor

    config.initialize()
    config.call_timeout = 0.15
    host = Supervisor(config, [sys.executable, str(Path(__file__).with_name("pipe_worker.py"))])
    try:
        assert await host.call("upgradeExtension", {"file_path": "x"}) == {"file_path": "x"}
        assert host.restarts == 0
        with pytest.raises(BridgeError, match="worker_timeout"):
            await host.call("hang")
    finally:
        await host.close()


# P3.11 — downloading does not pause preparations; offers survive a restart.
async def test_download_and_compare_do_not_pause_preparations(config):
    install_manifest(config.state_dir, native_manifest())
    service, worker = await started(config)
    service.config.persist(registries=[REGISTRY])
    worker.offer = {"id": "invented", "version": "2.0", "is_installed": True, "has_update": True}
    worker.offered = native_manifest("2.0", signedSession=dict(SESSION, appVersion="invented@2"))
    seen: list[bool] = []
    original = worker.call

    async def watching(method, args=None):
        if method == "downloadRepoExtension":
            seen.append("invented" in service.changing)
        return await original(method, args)

    worker.call = watching
    try:
        await service.check_updates()
        assert seen == [False] and service.updates["invented"]["state"] == "waiting"
    finally:
        await service.close()
    service, worker = await started(config)
    service.config.persist(registries=[REGISTRY])
    worker.offer = {"id": "invented", "version": "2.0", "is_installed": True, "has_update": True}
    try:
        assert service.updates["invented"]["state"] == "waiting"
        await service.check_updates()
        assert worker.called("downloadRepoExtension") == 0
    finally:
        await service.close()


# P3.12 — state files are private.
async def test_state_files_are_written_privately(config):
    service, worker = await keep_alive_service(config)
    try:
        write_record(config.state_dir, expires_in(1800))
        await service.check_sessions()
        service.management.manual.add("invented")
        service.management.needs_sign_in("invented", "downloadByStrategy")
        for name in ("sessions.json", "sign-in.json"):
            mode = stat.S_IMODE((config.state_dir / name).stat().st_mode)
            assert mode == 0o600, name
        service.updates["invented"] = {"version": "2.0", "registry": REGISTRY, "state": "waiting"}
        service.save_state("updates.json", service.updates)
        assert stat.S_IMODE((config.state_dir / "updates.json").stat().st_mode) == 0o600
    finally:
        await service.close()
