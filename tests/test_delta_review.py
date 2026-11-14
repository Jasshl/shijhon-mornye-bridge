"""Regression tests for the delta review of the review fixes."""

from __future__ import annotations

import asyncio
import json
import signal
import sys
from pathlib import Path
from typing import Any

import pytest
from test_sign_in import (
    SESSION,
    expires_in,
    install_manifest,
    native_manifest,
    settle,
    started,
    write_record,
)
from test_web import REGISTRY, setup

from mornye_bridge import worker as worker_module
from mornye_bridge.errors import BridgeError
from mornye_bridge.supervisor import Supervisor

NEEDS = "needs_login_or_verification"
OFFER = {"id": "invented", "version": "2.0", "is_installed": True, "has_update": True}


async def offered(config, manifest: dict[str, Any] | None = None):
    install_manifest(config.state_dir, native_manifest())
    service, worker = await started(config)
    service.config.persist(registries=[REGISTRY])
    worker.offer = dict(OFFER)
    if manifest is not None:
        worker.offered = manifest
    return service, worker


# P2.1 — the first reporter stays while the notice stands.
async def test_weaker_later_reporter_does_not_take_over(config):
    install_manifest(config.state_dir, native_manifest())
    write_record(config.state_dir, expires_in(3000))
    service, worker = await started(config)
    management = service.management
    management.manual.add("invented")
    original = worker.call
    failing = {"customSearch": 1, "pending": 1}

    async def call(method, args=None):
        if (
            method == "invokeExtensionAction"
            and args["action"] == "customSearch"
            and failing["customSearch"]
        ):
            failing["customSearch"] -= 1
            return {"success": False, "error_type": "verification_required"}
        if method == "getExtensionPendingAuth" and failing["pending"]:
            failing["pending"] -= 1
            raise BridgeError(NEEDS)
        return await original(method, args)

    worker.call = call
    try:
        with pytest.raises(BridgeError):
            await management.call("downloadByStrategy", "{}", "invented")
        with pytest.raises(BridgeError):
            await management.raw("invented", "customSearch", ["x", {"filter": "tracks"}])
        await management.raw("invented", "customSearch", ["x", {"filter": "tracks"}])
        assert management.states["invented"] == NEEDS
        with pytest.raises(BridgeError):
            await service.execute("pending-auth.show", {"extension": "invented"})
        await service.execute("pending-auth.show", {"extension": "invented"})
        assert management.states["invented"] == NEEDS
        assert management.sources["invented"] == "downloadByStrategy"
        saved = json.loads((config.state_dir / "sign-in.json").read_text())
        assert saved["invented"]["source"] == "downloadByStrategy"
    finally:
        await service.close()


# P2.2 — an update the pinned host cannot load is never installed.
async def test_update_the_host_cannot_load_is_never_installed(config):
    manifest = native_manifest("2.0", requiredRuntimeFeatures=["signedSession@9"])
    service, worker = await offered(config, manifest)
    try:
        service.config.persist(update_all=True)
        await service.check_updates()
        assert service.updates["invented"]["state"] == "unsupported"
        assert worker.called("upgradeExtension") == 0
        assert not await service.update_waiting("invented")
        await service.check_updates()  # known: not downloaded again
        assert worker.called("downloadRepoExtension") == 1
        assert worker.called("upgradeExtension") == 0
    finally:
        await service.close()


async def test_card_shows_an_unsupported_update_without_a_button(tmp_path):
    async with setup(tmp_path) as (client, service, worker, app):
        service.updates["invented"] = {
            "version": "2.0",
            "registry": REGISTRY,
            "state": "unsupported",
        }
        page = await client.get("/")
        assert "needs more than this bridge's extension host provides" in page.text
        assert "Update to 2.0</button>" not in page.text


# P2.3 — the worker leaves its lifetime to the supervisor.
def test_worker_ignores_the_stop_signal(monkeypatch):
    seen: list[Any] = []
    previous = signal.getsignal(signal.SIGTERM)

    def run(coroutine):
        coroutine.close()
        seen.append(signal.getsignal(signal.SIGTERM))

    def exit_(code):
        raise SystemExit(code)

    monkeypatch.setattr(worker_module.asyncio, "run", run)
    monkeypatch.setattr(worker_module.os, "_exit", exit_)
    try:
        with pytest.raises(SystemExit):
            worker_module.main()
        assert seen == [worker_module.ignore_stop]
    finally:
        signal.signal(signal.SIGTERM, previous)


# P2.3 / P3.1 — shutdown waits for an upgrade within shutdown_timeout, collects it and
# saves the round's state.
async def test_shutdown_bounds_the_upgrade_wait_and_saves_state(config):
    config.shutdown_timeout = 0.3
    service, worker = await offered(config)
    entered = asyncio.Event()
    original = worker.call

    async def hanging(method, args=None):
        if method == "upgradeExtension":
            entered.set()
            await asyncio.sleep(30)
        return await original(method, args)

    worker.call = hanging
    service.check_updates_soon()
    await asyncio.wait_for(entered.wait(), 2)
    upgrading = service.upgrading
    loop = asyncio.get_running_loop()
    started_at = loop.time()
    await asyncio.wait_for(service.close(), 3)
    assert loop.time() - started_at < 2
    assert upgrading and upgrading.done()
    saved = json.loads((config.state_dir / "updates.json").read_text())
    assert saved["invented"]["version"] == "2.0"
    assert not (config.state_dir / "repo" / "invented.package").exists()


# P3.2 — no worker restart for other calls during an upgrade; the lock is not held.
async def test_other_timeouts_do_not_restart_the_worker_during_an_upgrade(config):
    config.initialize()
    config.call_timeout = 0.15
    host = Supervisor(config, [sys.executable, str(Path(__file__).with_name("pipe_worker.py"))])
    try:
        upgrade = asyncio.create_task(host.call("upgradeExtension", {"file_path": "x"}))
        await asyncio.sleep(0.05)
        with pytest.raises(BridgeError, match="worker_timeout"):
            await host.call("hang")
        assert await upgrade == {"file_path": "x"}
        assert host.restarts == 0
    finally:
        await host.close()


async def test_upgrade_runs_without_the_mutation_lock(config):
    service, worker = await offered(config)
    entered, release = asyncio.Event(), asyncio.Event()
    original = worker.call

    async def slow(method, args=None):
        if method == "upgradeExtension":
            entered.set()
            await release.wait()
        return await original(method, args)

    worker.call = slow
    try:
        round_ = asyncio.create_task(service.check_updates())
        await asyncio.wait_for(entered.wait(), 2)
        assert not service.mutation_lock.locked()
        with pytest.raises(BridgeError, match="extension_updating"):
            await service.execute("settings.set", {"extension": "invented", "values": {}})
        assert "invented" in service.changing
        release.set()
        await round_
        assert service.updates["invented"]["state"] == "installed"
        assert "invented" not in service.changing and not service.upgrading_extension
    finally:
        await service.close()


# P3.3 — a waiting offer stays waiting while the extension prepares; nothing downloads.
async def test_waiting_offer_stays_waiting_while_preparing(config):
    service, worker = await offered(
        config, native_manifest("2.0", signedSession=dict(SESSION, appVersion="invented@2"))
    )
    try:
        await service.check_updates()
        assert service.updates["invented"]["state"] == "waiting"
        downloads = worker.called("downloadRepoExtension")
        service.jobs[("invented", "track", "native-aac")] = None  # type: ignore[assignment]
        assert not await service.update_waiting("invented")
        assert service.updates["invented"]["state"] == "waiting"
        assert worker.called("downloadRepoExtension") == downloads
        service.jobs.clear()
    finally:
        await service.close()


# P3.4 — a login form without a valid submit action is not a finished sign-in.
async def test_malformed_login_form_is_not_a_sign_in(config):
    service, worker = await started(config)
    worker.extension["settings"] = [{"key": "login", "label": "Log in", "action": "login"}]
    service.management.manual.add("invented")
    try:
        service.management.needs_sign_in("invented", "downloadByStrategy")
        worker.action_answer = {"success": True, "action_form": {"version": 1, "fields": []}}
        await service.execute("action", {"extension": "invented", "action": "login"})
        assert service.management.states["invented"] == NEEDS
    finally:
        await service.close()


# P3.5 — a stale offer never installs another, the same or an older version.
@pytest.mark.parametrize(
    "registry_version,package_version,installed",
    [("2.1", "2.1", False), ("2.0", "2.1", False), ("1.0", "1.0", False), ("2.0", "2.0", True)],
)
async def test_stale_offer_is_rechecked_before_installing(
    config, registry_version, package_version, installed
):
    service, worker = await offered(config)
    try:
        service.updates["invented"] = {"version": "2.0", "registry": REGISTRY, "state": "waiting"}
        worker.offer = dict(OFFER, version=registry_version)
        worker.offered = native_manifest(package_version)
        if registry_version == "1.0":
            service.updates["invented"]["version"] = "1.0"
        assert await service.update_waiting("invented") is installed
        assert worker.called("upgradeExtension") == (1 if installed else 0)
        if not installed:
            assert "invented" not in service.updates
            assert not (config.state_dir / "repo" / "invented.package").exists()
    finally:
        await service.close()


async def test_extension_without_a_challenge_keeps_its_update_waiting(config):
    # Neither a signed session nor a declared sign-in action: the update keeps waiting for
    # the operator (sign-in step button or Update on the card).
    service, worker = await offered(config, native_manifest("2.0", permissions={}))
    install_manifest(config.state_dir, native_manifest(signedSession=None))
    try:
        await service.check_updates()
        assert service.updates["invented"]["state"] == "waiting"
        service.management.needs_sign_in("invented", "downloadByStrategy")
        await settle(service)
        assert worker.called("upgradeExtension") == 0
    finally:
        await service.close()
