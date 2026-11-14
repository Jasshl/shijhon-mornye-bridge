"""Renewal by a brief download, with a fake host that renews (or not) during a download."""

from __future__ import annotations

import asyncio
import json
import logging
import stat
from pathlib import Path
from typing import Any

from conftest import EXT, FakeWorker
from test_sign_in import (
    expires_in,
    install_manifest,
    native_manifest,
    write_record,
)

from mornye_bridge import service as service_module
from mornye_bridge.ids import RECORDING_KEY
from mornye_bridge.service import Service
from mornye_bridge.supervisor import Supervisor

RECORDING = ("ZZABC2600001", "Invented Song", "Invented Artist", 1000, "Invented Album")


class HostWorker(FakeWorker):
    """downloadByStrategy reports preparing stages, then (mode) renews the session, starts
    the transfer, fails, or delivers a real file. Searches never renew."""

    def __init__(self, state: Path) -> None:
        super().__init__()
        self.state = state
        self.mode = "renew"
        self.items: dict[str, dict[str, Any]] = {}
        self.renewal_calls: list[dict[str, Any]] = []
        self.cancelled_at: str | None = None
        self.service: Service | None = None
        self.seen_jobs: list[int] = []
        self.version = "1.0"

    def progress(self, item_id: str) -> dict[str, Any] | None:
        return self.items.get(item_id)

    async def call(self, method: str, args: Any = None) -> Any:
        if method == "getInstalledExtensions":
            return [EXT | {"enabled": True, "version": self.version}]
        if method == "getExtensionPendingAuth":
            return ""
        if method == "checkExtensionHealth":
            return {"status": "online"}
        if method != "downloadByStrategy" or self.mode == "deliver":
            return await super().call(method, args)
        payload = json.loads(args)
        self.renewal_calls.append(payload)
        item = payload["item_id"]
        if self.service:
            self.seen_jobs.append(len(self.service.jobs) + len(self.service.job_tracks))
        # A partial file in the private output directory.
        (Path(payload["output_dir"]) / "partial.audio").write_bytes(b"\0" * 1024)
        self.items[item] = {"status": "preparing", "stage": "checking_session"}
        stage = "start"
        try:
            await asyncio.sleep(0.25)
            self.items[item] = {"status": "preparing", "stage": "resolving_stream"}
            stage = "resolving_stream"
            await asyncio.sleep(0.25)
            if self.mode == "fail":
                return {"success": False, "error_type": "api_error"}
            if self.mode == "renew":
                # The extension's first signed request: the host renews the session.
                write_record(self.state, expires_in(7200))
                stage = "renewed"
                await asyncio.sleep(0.25)
            self.items[item] = {"status": "downloading", "bytes_received": 0}
            stage = "downloading"
            await asyncio.sleep(30)
            return {"success": True}
        except asyncio.CancelledError:
            self.cancelled_at = stage
            raise


async def renewal_service(config, *, delivered: bool = True) -> tuple[Service, HostWorker]:
    install_manifest(config.state_dir, native_manifest())
    worker = HostWorker(config.state_dir)
    service = Service(config, worker)
    worker.service = service
    await service.start()
    service.start_maintenance()
    # Its searches are known not to renew the session.
    service.sessions["invented"] = {"no_keep_alive": "1.0"}
    if delivered:
        await service.remember_delivery("invented", RECORDING, "native-flac")
    return service, worker


def jobs_left(config) -> list[str]:
    return [path.name for path in (config.state_dir / "jobs").iterdir()]


async def test_renews_once_inside_the_window_and_cancels_at_once(config, caplog):
    caplog.set_level(logging.INFO, "mornye_bridge")
    service, worker = await renewal_service(config)
    try:
        write_record(config.state_dir, expires_in(3 * 3600))
        await service.check_sessions()
        assert not worker.renewal_calls
        write_record(config.state_dir, expires_in(1800))
        await service.check_sessions()
        assert len(worker.renewal_calls) == 1
        payload = worker.renewal_calls[0]
        assert payload["track_name"] == "Invented Song" and payload["quality"] == "native-flac"
        # Cancelled as soon as the expiry moved, before the transfer began.
        assert worker.cancelled_at == "renewed"
        assert jobs_left(config) == []
        info = service.sessions["invented"]
        assert info["renewed_by"] == "download" and info["renewed"]
        view = service.session_view("invented")
        assert view["renewed_by"] == "download" and view["keep_alive"] == "download"
        assert "renewal extension=invented kind=brief_download outcome=renewed" in caplog.text
        assert "preparation extension" not in caplog.text
        # Not a delivery or a Shijhon-visible preparation.
        assert worker.seen_jobs == [0]
        assert service.cache and not list(service.cache.entries("invented"))
        assert service.delivered["invented"] == (RECORDING, "native-flac")
        # The renewed session is outside the window: nothing more.
        await service.check_sessions()
        assert len(worker.renewal_calls) == 1
    finally:
        await service.close()


async def test_cancels_when_the_transfer_begins_and_gives_up_per_version(config):
    service, worker = await renewal_service(config)
    worker.mode = "start_only"
    try:
        write_record(config.state_dir, expires_in(1800))
        await service.check_sessions()
        # Not cancelled on the preparing stages; cancelled when the transfer began.
        assert worker.cancelled_at == "downloading"
        assert jobs_left(config) == []
        assert service.sessions["invented"]["download_misses"] == "1"
        # At most once per session lifetime.
        await service.check_sessions()
        assert len(worker.renewal_calls) == 1
        # Next session lifetime: the second miss gives up for this version.
        write_record(config.state_dir, expires_in(1700))
        service.sessions["invented"].pop("renewed", None)
        await service.check_sessions()
        assert len(worker.renewal_calls) == 2
        assert service.sessions["invented"]["no_download_renewal"] == "1.0"
        assert service.session_view("invented")["keep_alive"] == "none"
        write_record(config.state_dir, expires_in(1600))
        service.sessions["invented"].pop("renewed", None)
        await service.check_sessions()
        assert len(worker.renewal_calls) == 2
        # A new version is tried again.
        worker.version = "1.1"
        service.management.invalidate()
        service.sessions["invented"]["no_keep_alive"] = "1.1"  # its searches do not renew
        write_record(config.state_dir, expires_in(1500))
        service.sessions["invented"].pop("renewed", None)
        await service.check_sessions()
        assert len(worker.renewal_calls) == 3
        assert service.sessions["invented"]["download_misses"] == "1"
    finally:
        await service.close()


async def test_renewal_by_other_means_and_unknown_recording_skip(config):
    service, worker = await renewal_service(config, delivered=False)
    try:
        write_record(config.state_dir, expires_in(1800))
        await service.check_sessions()
        assert not worker.renewal_calls  # nothing delivered yet: nothing invented
        assert service.session_view("invented")["keep_alive"] == "download_later"
        await service.remember_delivery("invented", RECORDING, "native-flac")
        # A renewal by something else (a real download) moves the expiry out of the window.
        write_record(config.state_dir, expires_in(7200))
        await service.check_sessions()
        assert not worker.renewal_calls
    finally:
        await service.close()


async def test_never_while_preparing_needing_sign_in_or_switched_off(config):
    service, worker = await renewal_service(config)
    try:
        write_record(config.state_dir, expires_in(1800))
        item = await service.management.extension("invented")
        expires = service.management.session_expiry("invented")
        assert expires
        service.jobs[("invented", "track", "native-aac")] = None  # type: ignore[assignment]
        await service.renew_by_download(item, expires)
        service.jobs.clear()
        service.management.manual.add("invented")
        service.management.needs_sign_in("invented", "downloadByStrategy")
        await service.renew_by_download(item, expires)
        service.management.signed_in("invented")
        service.config.persist(renew_by_download=False)
        await service.check_sessions()
        assert not worker.renewal_calls
        assert "renew_by_download = false" in (config.state_dir / "settings.toml").read_text()
    finally:
        await service.close()


async def test_failed_renewal_backs_off_and_is_not_a_miss(config):
    service, worker = await renewal_service(config)
    worker.mode = "fail"
    try:
        write_record(config.state_dir, expires_in(1800))
        await service.check_sessions()
        assert len(worker.renewal_calls) == 1
        assert "download_misses" not in service.sessions["invented"]
        assert service.renew_next["invented"] > service_module.time.monotonic()
        assert jobs_left(config) == []
    finally:
        await service.close()


async def test_real_preparation_records_the_delivery_privately(config):
    install_manifest(config.state_dir, native_manifest())
    worker = HostWorker(config.state_dir)
    worker.mode = "deliver"
    service = Service(config, worker)
    await service.start()
    try:
        extension = await service.extension("invented")
        ident = service.recording_ident(RECORDING)
        await service.begin(extension, RECORDING_KEY + ident, "native-aac", RECORDING)
        assert service.delivered["invented"] == (RECORDING, "native-aac")
        path = config.state_dir / "delivered.json"
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        saved = json.loads(path.read_text())
        assert saved == {"invented": {"recording": list(RECORDING), "quality": "native-aac"}}
    finally:
        await service.close()
    service = Service(config, HostWorker(config.state_dir))
    await service.start()
    try:
        assert service.delivered["invented"] == (RECORDING, "native-aac")
        await service.execute("remove", {"extension": "invented"})
        assert not service.delivered
        assert not (config.state_dir / "delivered.json").exists()
    finally:
        await service.close()


def test_supervisor_reports_merged_progress(config):
    host = Supervisor(config)
    host.downloads["item"] = (0.0, {"status": "downloading", "bytes_received": 0})
    assert host.progress("item") == {"status": "downloading", "bytes_received": 0}
    assert host.progress("missing") is None


async def test_dashboard_shows_the_brief_download_renewal_and_setting(tmp_path):
    from test_web import csrf, setup

    async with setup(tmp_path) as (client, service, worker, app):
        install_manifest(service.config.state_dir, native_manifest())
        write_record(service.config.state_dir, expires_in(5 * 3600))
        service.sessions["invented"] = {
            "no_keep_alive": "1.0",
            "renewed": "2026-11-11T09:30:00+00:00",
            "renewed_by": "download",
        }
        await service.remember_delivery("invented", RECORDING, "native-flac")
        page = await client.get("/")
        assert "last renewed 2026-11-11 09:30 UTC by a brief download" in page.text
        assert "Kept alive by briefly starting the last song" in page.text
        assert "Invented Song" not in page.text
        assert 'name="renew_by_download" value="yes" checked' in page.text
        await client.post(
            "/", data={"csrf": csrf(page), "operation": "sessions.save", "keep_alive": "yes"}
        )
        assert service.config.renew_by_download is False and service.config.keep_alive
