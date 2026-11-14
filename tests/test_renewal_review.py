"""Regression tests for the review of the renewal by a brief download and its fixes."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any

import pytest
from test_delta_review import offered
from test_renewal_download import RECORDING, HostWorker, jobs_left, renewal_service
from test_sign_in import expires_in, install_manifest, native_manifest, write_record

from mornye_bridge import service as service_module
from mornye_bridge.errors import BridgeError
from mornye_bridge.management import PENDING_AUTH
from mornye_bridge.service import Claims
from mornye_bridge.supervisor import Supervisor

PIPE_WORKER = [sys.executable, str(Path(__file__).with_name("pipe_worker.py"))]


def download(item: str) -> str:
    return json.dumps({"item_id": item})


# P1.1 — an operation queued on the mutation lock during the package download is refused
# once the upgrade has begun, and never clears the upgrade's claim.
async def test_operation_queued_during_the_package_download_is_refused(config):
    service, worker = await offered(config)
    held, release = asyncio.Event(), asyncio.Event()
    upgrading, finish = asyncio.Event(), asyncio.Event()
    original = worker.call
    order: list[str] = []

    async def call(method: str, args: Any = None) -> Any:
        if method == "downloadRepoExtension":
            held.set()
            await release.wait()
        if method == "upgradeExtension":
            order.append("upgrade-start")
            upgrading.set()
            await finish.wait()
            result = await original(method, args)
            order.append("upgrade-end")
            return result
        if method == "setExtensionEnabled":
            order.append("set-enabled")
        return await original(method, args)

    worker.call = call
    try:
        round_ = asyncio.create_task(service.check_updates())
        await asyncio.wait_for(held.wait(), 2)
        queued = asyncio.create_task(service.execute("disable", {"extension": "invented"}))
        for _ in range(5):
            await asyncio.sleep(0)
        assert service.mutation_lock.locked() and not queued.done()
        release.set()
        await asyncio.wait_for(upgrading.wait(), 2)
        with pytest.raises(BridgeError, match="extension_updating"):
            await queued
        assert "invented" in service.changing
        finish.set()
        await round_
        assert order == ["upgrade-start", "upgrade-end"]
        assert "invented" not in service.changing
    finally:
        await service.close()


def test_claims_are_counted():
    claims = Claims()
    claims.add("invented")
    claims.add("invented")
    claims.discard("invented")
    assert "invented" in claims
    claims.discard("invented")
    assert "invented" not in claims
    claims.discard("invented")
    assert "invented" not in claims


# P2.1 — during an upgrade, an unanswered download cancel keeps the call (and so its
# staging folder and admission slot) until the upgrade ends; then the worker restarts.
async def test_unanswered_cancel_during_an_upgrade_waits_for_it(config):
    config.initialize()
    config.cancel_timeout = 0.1
    host = Supervisor(config, PIPE_WORKER)
    try:
        upgrade = asyncio.create_task(host.call("upgradeExtension", {"file_path": "x"}))
        await asyncio.sleep(0.05)
        hung = asyncio.create_task(host.call("downloadByStrategy", download("hung")))
        await asyncio.sleep(0.05)
        hung.cancel()
        await asyncio.sleep(0.15)  # past cancel_timeout, the upgrade still runs
        assert not hung.done() and not upgrade.done()
        assert host.active == 2 and host.restarts == 0
        assert await upgrade == {"file_path": "x"}
        with pytest.raises(asyncio.CancelledError):
            await hung
        assert host.restarts == 1
        assert host.active == 0 and not host.pending
    finally:
        await host.close()


# P2.2 — a renewal's unanswered cancel never restarts the worker while another call runs;
# the restart waits for the others and is marked, so no preparation is held back.
async def test_renewal_cancel_waits_for_other_calls_before_restarting(config):
    config.initialize()
    config.cancel_timeout = 0.1
    host = Supervisor(config, PIPE_WORKER)
    try:
        other = asyncio.create_task(host.call("downloadByStrategy", download("other")))
        host.gentle.add("hung")
        hung = asyncio.create_task(host.call("downloadByStrategy", download("hung")))
        await asyncio.sleep(0.05)
        hung.cancel()
        assert await other == {"success": True}
        with pytest.raises(asyncio.CancelledError):
            await hung
        assert host.restarts == 1 and host.gentle_restart_at > 0
        assert host.active == 0 and not host.pending and not host.gentle_waiting
    finally:
        await host.close()


# Delta P2-A — calls that start after the cancel are not waited for, so steady traffic
# never holds the restart back.
async def test_renewal_cancel_does_not_wait_for_later_calls(config):
    config.initialize()
    config.cancel_timeout = 0.1
    host = Supervisor(config, PIPE_WORKER)
    traffic: list[asyncio.Task[Any]] = []
    try:
        host.gentle.add("hung")
        hung = asyncio.create_task(host.call("downloadByStrategy", download("hung")))
        await asyncio.sleep(0.05)
        hung.cancel()
        loop = asyncio.get_running_loop()
        began = loop.time()
        while not hung.done() and loop.time() - began < 3:
            traffic.append(
                asyncio.create_task(
                    host.call("downloadByStrategy", download(f"later-{len(traffic)}"))
                )
            )
            await asyncio.sleep(0.05)
        assert hung.done() and loop.time() - began < 1
        assert host.restarts == 1
    finally:
        await asyncio.gather(*traffic, return_exceptions=True)
        await host.close()


# Delta P2-A — the wait for calls in flight is capped at call_timeout.
async def test_renewal_cancel_wait_is_capped(config):
    config.initialize()
    config.cancel_timeout = 0.1
    config.call_timeout = 0.2
    host = Supervisor(config, PIPE_WORKER)
    try:
        assert await host.call("ping", {"warm": True}) == {"warm": True}
        other = asyncio.create_task(host.call("downloadByStrategy", download("other")))
        host.gentle.add("hung")
        hung = asyncio.create_task(host.call("downloadByStrategy", download("hung")))
        await asyncio.sleep(0.02)
        hung.cancel()
        with pytest.raises(asyncio.CancelledError):
            await hung
        with pytest.raises(BridgeError, match="worker_restarted"):
            await other
        assert host.restarts == 1 and host.active == 0 and not host.pending
    finally:
        await host.close()


class BusyFirst(HostWorker):
    """The first renewal call fails before the host starts the download."""

    def __init__(self, state: Path) -> None:
        super().__init__(state)
        self.busy = 1

    async def call(self, method: str, args: Any = None) -> Any:
        if method == "downloadByStrategy" and self.busy:
            self.busy -= 1
            self.renewal_calls.append(json.loads(args))
            raise BridgeError("worker_busy", 503, 3)
        return await super().call(method, args)


# P2.3 — a failure before the host started does not use the session's one attempt: it is
# tried again inside the same window after a short backoff.
async def test_failure_before_the_host_starts_is_retried_in_the_window(config):
    service, _ = await renewal_service(config)
    await service.close()
    worker = BusyFirst(config.state_dir)
    service = service_module.Service(config, worker)
    worker.service = service
    await service.start()
    service.sessions["invented"] = {"no_keep_alive": "1.0"}
    await service.remember_delivery("invented", RECORDING, "native-flac")
    try:
        write_record(config.state_dir, expires_in(1800))
        item = await service.management.extension("invented")
        expires = service.management.session_expiry("invented")
        assert expires
        delay = await service.renew_by_download(item, expires)
        assert len(worker.renewal_calls) == 1
        assert "download_attempt" not in service.sessions["invented"]
        assert delay == service_module.RENEWAL_RETRY
        assert service.renew_next["invented"] > service_module.time.monotonic()
        service.renew_next["invented"] = 0.0
        await service.renew_by_download(item, expires)
        assert len(worker.renewal_calls) == 2
        assert service.sessions["invented"]["renewed_by"] == "download"
        assert jobs_left(config) == []
    finally:
        await service.close()


# P2.2 — a renewal starts only while nothing is prepared, and a preparation that starts
# stops it without using the session's attempt.
async def test_preparation_stops_a_running_renewal(config):
    service, worker = await renewal_service(config)
    worker.mode = "start_only"
    try:
        write_record(config.state_dir, expires_in(1800))
        item = await service.management.extension("invented")
        expires = service.management.session_expiry("invented")
        assert expires
        service.jobs[("invented", "track", "native-aac")] = None  # type: ignore[assignment]
        await service.renew_by_download(item, expires)
        assert not worker.renewal_calls  # its own extension prepares a file
        service.jobs.clear()
        for attempt in range(1, service_module.RENEWAL_STOPS + 1):
            service.renew_next.pop("invented", None)
            renewal = asyncio.create_task(service.renew_by_download(item, expires))
            for _ in range(50):
                await asyncio.sleep(0.01)
                if len(worker.renewal_calls) == attempt:
                    break
            assert service.renewal and service.renewal[0] == "invented"
            # Another extension's preparation does not stop it; its own does.
            other = {"id": "other", "capabilities": {}}
            if service.renewal[0] != other["id"]:
                service.renewal_stop.set()  # what begin() does for its extension
            delay = await renewal
            assert service.renewal is None and jobs_left(config) == []
            assert "download_misses" not in service.sessions["invented"]
            if attempt < service_module.RENEWAL_STOPS:
                assert delay == 120.0
                assert "download_attempt" not in service.sessions["invented"]
        # Stopped too often in this window: it gives the window up.
        assert service.sessions["invented"]["download_attempt"] == expires.isoformat()
        service.renew_next.pop("invented", None)
        await service.renew_by_download(item, expires)
        assert len(worker.renewal_calls) == service_module.RENEWAL_STOPS
    finally:
        await service.close()


async def test_other_extensions_preparations_do_not_hold_a_renewal(config):
    service, worker = await renewal_service(config)
    try:
        write_record(config.state_dir, expires_in(1800))
        item = await service.management.extension("invented")
        expires = service.management.session_expiry("invented")
        assert expires
        service.jobs[("other", "track", "native-aac")] = None  # type: ignore[assignment]
        await service.renew_by_download(item, expires)
        assert len(worker.renewal_calls) == 1
        assert service.sessions["invented"]["renewed_by"] == "download"
        service.jobs.clear()
    finally:
        await service.close()


class ReportsNothing(HostWorker):
    """The request is sent but the host reports no stage before the worker restarts."""

    def progress(self, item_id: str) -> dict[str, Any] | None:
        return {}

    async def call(self, method: str, args: Any = None) -> Any:
        if method == "downloadByStrategy":
            self.renewal_calls.append(json.loads(args))
            await asyncio.sleep(0.05)
            raise BridgeError("worker_restarted")
        return await super().call(method, args)


# Delta P3.1 — "started" means the host reported a stage, not that the request was sent.
async def test_restart_before_any_host_stage_does_not_use_the_attempt(config):
    install_manifest(config.state_dir, native_manifest())
    worker = ReportsNothing(config.state_dir)
    service = service_module.Service(config, worker)
    worker.service = service
    await service.start()
    service.sessions["invented"] = {"no_keep_alive": "1.0"}
    await service.remember_delivery("invented", RECORDING, "native-flac")
    try:
        write_record(config.state_dir, expires_in(1800))
        item = await service.management.extension("invented")
        expires = service.management.session_expiry("invented")
        assert expires
        delay = await service.renew_by_download(item, expires)
        assert len(worker.renewal_calls) == 1
        assert "download_attempt" not in service.sessions["invented"]
        assert delay == service_module.RENEWAL_RETRY
    finally:
        await service.close()


# Delta P2-A — while a renewal waits for other calls after an unanswered cancel, changes
# to its extension are refused at once instead of holding the mutation lock.
async def test_changes_are_refused_at_once_while_a_renewal_waits(config):
    service, worker = await renewal_service(config)
    worker.mode = "start_only"
    try:
        write_record(config.state_dir, expires_in(1800))
        item = await service.management.extension("invented")
        expires = service.management.session_expiry("invented")
        assert expires
        worker.gentle_waiting = set()  # type: ignore[attr-defined]
        renewal = asyncio.create_task(service.renew_by_download(item, expires))
        for _ in range(50):
            await asyncio.sleep(0.01)
            if worker.renewal_calls:
                break
        worker.gentle_waiting.add(service.renewal_job)  # type: ignore[attr-defined]
        loop = asyncio.get_running_loop()
        began = loop.time()
        with pytest.raises(BridgeError, match="extension_busy"):
            await service.execute("disable", {"extension": "invented"})
        assert loop.time() - began < 0.5
        worker.gentle_waiting.clear()  # type: ignore[attr-defined]
        await renewal
    finally:
        await service.close()


# P3.3 — removing an extension stops its renewal first.
async def test_remove_stops_the_renewal_first(config):
    service, worker = await renewal_service(config)
    worker.mode = "start_only"
    try:
        write_record(config.state_dir, expires_in(1800))
        item = await service.management.extension("invented")
        expires = service.management.session_expiry("invented")
        assert expires
        renewal = asyncio.create_task(service.renew_by_download(item, expires))
        for _ in range(50):
            await asyncio.sleep(0.01)
            if worker.renewal_calls:
                break
        await service.execute("remove", {"extension": "invented"})
        assert renewal.done()
        assert service.renewal is None and not service.delivered
        assert not (config.state_dir / "delivered.json").exists()
    finally:
        await service.close()


# P3.1 — after the search that shows searches do not renew, the renewal by download is
# looked at again soon, inside the same window.
async def test_renewal_follows_soon_after_searches_are_found_not_to_renew(config):
    service, worker = await renewal_service(config, delivered=False)
    try:
        service.sessions["invented"] = {}
        write_record(config.state_dir, expires_in(1800))
        item = await service.management.extension("invented")
        expires = service.management.session_expiry("invented")
        assert expires
        # One search in this window already succeeded without renewing.
        service.keep_alive_misses["invented"] = (expires.isoformat(), 1)
        delay = await service.keep_alive(item)
        assert service.sessions["invented"].get("no_keep_alive") == "1.0"
        assert delay == 60.0
    finally:
        await service.close()


# P3.6 — a stronger later reporter takes over from a weaker one.
async def test_stronger_later_reporter_takes_over(config):
    service, _ = await renewal_service(config, delivered=False)
    try:
        management = service.management
        management.needs_sign_in("invented", PENDING_AUTH)
        management.needs_sign_in("invented", "downloadByStrategy")
        assert management.sources["invented"] == "downloadByStrategy"
        management.needs_sign_in("invented", PENDING_AUTH)
        assert management.sources["invented"] == "downloadByStrategy"
    finally:
        await service.close()


# P3.7 — the delivered recording is kept only while renewal by a download can run.
async def test_delivery_is_kept_only_while_renewal_can_run(config):
    service, _ = await renewal_service(config, delivered=False)
    path = config.state_dir / "delivered.json"
    try:
        service.config.keep_alive = False
        await service.remember_delivery("invented", RECORDING, "native-flac")
        assert not service.delivered and not path.exists()
        service.config.keep_alive = True
        await service.remember_delivery("invented", RECORDING, "native-flac")
        assert path.exists()
        service.config.renew_by_download = False
        await service.forget_deliveries()
        assert not path.exists()
    finally:
        await service.close()
    config.keep_alive = True
    config.renew_by_download = False
    path.write_text(json.dumps({"invented": {"recording": list(RECORDING), "quality": "x"}}))
    service = service_module.Service(config, HostWorker(config.state_dir))
    await service.start()
    try:
        assert not service.delivered and not path.exists()
    finally:
        await service.close()


# P3.8 — an unsupported offer is judged again after the bridge's pinned host changes.
async def test_unsupported_offer_is_rechecked_for_another_host(config, monkeypatch):
    from test_sign_in import native_manifest

    manifest = native_manifest("2.0", requiredRuntimeFeatures=["signedSession@9"])
    service, worker = await offered(config, manifest)
    try:
        await service.check_updates()
        assert service.updates["invented"]["state"] == "unsupported"
        assert worker.called("downloadRepoExtension") == 1
        await service.check_updates()
        assert worker.called("downloadRepoExtension") == 1
        monkeypatch.setattr(service_module, "HOST_ID", "another/host")
        await service.check_updates()
        assert worker.called("downloadRepoExtension") == 2
    finally:
        await service.close()
