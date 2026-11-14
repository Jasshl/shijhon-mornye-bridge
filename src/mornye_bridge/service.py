from __future__ import annotations

import asyncio
import fcntl
import hashlib
import json
import logging
import math
import os
import re
import secrets
import shutil
import time
import unicodedata
from collections import OrderedDict
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, BinaryIO

from . import config as host_config
from .cache import Cache, Entry
from .catalog import Catalog, map_item, text
from .config import CatalogConfig, Config, secret_file, write_private_json
from .errors import BridgeError, native_error
from .finalize import finalize
from .ids import (
    RECORDING_KEY,
    RECORDING_PREFIX,
    Recording,
    external_id,
    internal_id,
    parse_recording,
    recording_id,
    reference_id,
)
from .management import (
    NEEDS_AUTH,
    Management,
    has_action_form,
    login_action,
    result_browser_url,
)
from .sessions import parse_time, saved_session
from .supervisor import Supervisor, Worker
from .updates import (
    PACKAGE_SUFFIXES,
    host_accepts,
    installed_manifest,
    keeps_sign_in,
    newer,
    package_manifest,
)

# One line per preparation and per resolve: codes and timings, never titles, artists,
# IDs, URLs, tokens or native message text.
log = logging.getLogger("mornye_bridge")

# Remembered answers per (extension, track, format): expiry and the track's metadata.
type Memory = OrderedDict[tuple[str, str, str], tuple[float, dict[str, Any] | None]]
MEMORY_ENTRIES = 2048
MISS_SECONDS = 300
FAILURE_SECONDS = 300
FOUND_SECONDS = 600
MAX_TEXT = 500
# The pinned host's checkAvailability Track fields (downloads/metadata.rs host_track),
# with the defaults it sends for facts not known. Its options' per-service track IDs
# are left out: the bridge has none, and they would only be empty.
TRACK_DEFAULTS: dict[str, Any] = {
    "album_artist": "",
    "cover_url": "",
    "release_date": "",
    "track_number": 0,
    "total_tracks": 0,
    "disc_number": 0,
    "total_discs": 0,
    "genre": "",
    "label": "",
    "copyright": "",
    "composer": "",
    "comment": "",
    "explicit": False,
    "album_type": "",
    "upc": "",
}


# Automatic sign-in: the first retry after a failed attempt waits ten minutes, doubling
# up to six hours. An attempt that needs the operator is not repeated until a sign-in
# works again.
SIGN_IN_RETRY = 600.0
SIGN_IN_RETRY_MAX = 21600.0
# Signed sessions are checked locally by the host; an unusable one asks its gateway.
SESSION_CHECK_SECONDS = 1800.0
SESSION_CHECK_FIRST = 120.0
UPDATE_CHECK_SECONDS = 86400.0
UPDATE_CHECK_FIRST = 600.0
# An extension still preparing files is updated on a later try.
UPDATE_BUSY_RETRY = 1800.0
CHECK_TIMEOUT = 60.0
# Keep-alive: the host renews a signed session during a signed request made in the last
# hour before it expires. One one-result catalog search is made about fifty minutes
# before expiry, at most every twenty minutes per extension. When two such searches leave
# the expiry unchanged, the extension's searches are not signed: that version is not
# kept alive. Failed searches back off from thirty minutes up to six hours.
KEEP_ALIVE_WINDOW = 3600.0
KEEP_ALIVE_LEAD = 3000.0
KEEP_ALIVE_LATEST = 120.0
KEEP_ALIVE_SPACING = 1200.0
KEEP_ALIVE_RETRY = 1800.0
KEEP_ALIVE_MISSES = 2
KEEP_ALIVE_QUERY = "music"
SESSIONS_FILE = "sessions.json"
# The pinned host an "unsupported" offer was judged against.
HOST_ID = f"{host_config.HOST_COMMIT}/{host_config.HOST_VERSION}"
UPDATES_FILE = "updates.json"
DELIVERED_FILE = "delivered.json"
# Renewal by a brief download: poll the session's expiry and the download's progress
# this often, and stop the attempt after this long at most.
RENEWAL_POLL = 0.1
RENEWAL_LIMIT = 60.0
RENEWAL_MISSES = 2
# A renewal that failed before the host started its download is tried again inside the
# window after this long, doubling up to KEEP_ALIVE_RETRY.
RENEWAL_RETRY = 300.0
# A renewal stopped this often in one window (preparations of its extension) gives up
# that window.
RENEWAL_STOPS = 3
# Supervisor failures that happen before the host starts a download.
NOT_STARTED = {
    "worker_busy",
    "worker_stopped",
    "worker_start_failed",
    "worker_crashed",
    "worker_restarted",
}
# Host progress statuses that mean the audio transfer itself has begun.
TRANSFER_STATUSES = {"downloading", "finalizing", "completed", "complete", "done"}


class Claims:
    """Extensions some operation is changing, counted: one operation finishing never
    clears another's claim on the same extension."""

    def __init__(self) -> None:
        self.counts: dict[str, int] = {}

    def add(self, key: str) -> None:
        self.counts[key] = self.counts.get(key, 0) + 1

    def discard(self, key: str) -> None:
        count = self.counts.get(key, 0) - 1
        if count > 0:
            self.counts[key] = count
        else:
            self.counts.pop(key, None)

    def clear(self) -> None:
        self.counts.clear()

    def __contains__(self, key: object) -> bool:
        return key in self.counts


def quality_options(extension: dict[str, Any]) -> list[dict[str, Any]]:
    return extension.get("quality_options") or []


def selected_format(extension: dict[str, Any], requested: str | None = None) -> str:
    options = quality_options(extension)
    default = next(
        (x["id"] for x in options if x.get("default")),
        next(
            (
                x["id"]
                for x in options
                if str(x.get("kind", "")).casefold() != "spatial" and not x.get("spatial")
            ),
            options[0]["id"] if options else "",
        ),
    )
    chosen = default if requested is None else requested
    if options and chosen not in {x["id"] for x in options}:
        raise BridgeError("unsupported_format", 400)
    if not options and chosen:
        raise BridgeError("unsupported_format", 400)
    return str(chosen)


def transfer_slots(extension: dict[str, Any]) -> int:
    """The manifest's capabilities.downloadTransfer.maxConcurrentDownloads as the host
    reads it: a number, rounded and bounded to 1-3; 3 when absent or not a number."""
    capabilities = extension.get("capabilities")
    transfer = capabilities.get("downloadTransfer") if isinstance(capabilities, dict) else None
    value = transfer.get("maxConcurrentDownloads") if isinstance(transfer, dict) else None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return 3
    return max(1, min(3, math.floor(value + 0.5)))


def wanted_recording(wanted: Mapping[str, str]) -> Recording:
    """The recording a caller describes: title and artist (required), durationMs, isrc
    and album. An ISRC that is not 12 letters or digits is no ISRC."""
    title, artist, album = (wanted.get(key, "") for key in ("title", "artist", "album"))
    if not title.strip() or not artist.strip() or max(map(len, (title, artist, album))) > MAX_TEXT:
        raise BridgeError("invalid_recording", 400)
    try:
        duration = float(wanted.get("durationMs") or 0)
    except ValueError:
        raise BridgeError("invalid_recording", 400) from None
    if not math.isfinite(duration) or duration < 0 or duration > 2**31:
        raise BridgeError("invalid_recording", 400)
    isrc = "".join(ch for ch in wanted.get("isrc", "") if ch.isascii() and ch.isalnum()).upper()
    return (isrc if len(isrc) == 12 else "", title, artist, round(duration), album)


def strategy_payload(
    extension: str, directory: Path, quality: str, job_id: str, recording: Recording
) -> dict[str, Any]:
    """The host planner's request for a described recording: no source and no track ID,
    so it asks the extension's checkAvailability and downloads what it found."""
    isrc, title, artist, duration, album = recording
    return {
        "contract_version": 1,
        "service": extension,
        "download_provider": extension,
        "output_dir": str(directory),
        "filename_format": "audio",
        "quality": quality,
        "item_id": job_id,
        "use_extensions": True,
        "use_fallback": False,
        "embed_metadata": False,
        "embed_lyrics": False,
        "post_processing_enabled": False,
        "network_concurrency_limit": 3,
        "source": "",
        "track_name": title,
        "artist_name": artist,
        "album_name": album,
        "duration_ms": duration,
        "isrc": isrc,
    }


def recording_track(ident: str, recording: Recording) -> dict[str, Any]:
    isrc, title, artist, duration, album = recording
    track: dict[str, Any] = {
        "id": ident,
        "title": title,
        "artist": artist,
        "durationMs": duration,
        "album": {"title": album},
    }
    if isrc:
        track["isrc"] = isrc
    return track


def availability_arguments(recording: Recording) -> list[Any]:
    isrc, title, artist, duration, album = recording
    track = TRACK_DEFAULTS | {
        "id": "",
        "name": title,
        "artists": artist,
        "album_name": album,
        "duration_ms": duration,
        "isrc": isrc,
    }
    return [isrc, title, artist, {"duration_ms": duration, "track": track}]


def availability_refusal(result: dict[str, Any]) -> BridgeError | None:
    """A login/verification need or a rate limit behind available:false, by the rules
    for native errors (its code, else its reason); None for genuine absence."""
    error = native_error(
        {
            "error_type": result.get("error_type") or result.get("code") or "",
            "error": result.get("reason") or result.get("error") or "",
            "retry_after_seconds": result.get("retry_after_seconds", 30),
        }
    )
    return error if error.code in ("needs_login_or_verification", "rate_limited") else None


def verified_track(
    ident: str, recording: Recording, result: dict[str, Any], audio: dict[str, Any]
) -> dict[str, Any]:
    """The prepared file's metadata, checked against the wanted recording by Shijhon's
    rules: a probed length within 3 s, or without a wanted or probed length the wanted
    ISRC reported by the result. A different reported ISRC alone is no mismatch (the same
    recording is often released under several), as in Shijhon's matching. A mismatch is a
    recording miss (404)."""
    isrc, title, artist, duration, album = recording
    reported = "".join(ch for ch in text(result.get("isrc")) if ch.isascii() and ch.isalnum())
    reported = reported.upper() if len(reported) == 12 else ""
    probed = audio.get("duration")
    length = round(probed * 1000) if isinstance(probed, (int, float)) and probed > 0 else 0
    if duration and length:
        if abs(length - duration) > 3000:
            raise BridgeError("different_recording_length", 404)
    elif not (isrc and reported == isrc):
        raise BridgeError("unverified_recording", 404)
    track = recording_track(ident, ("", title, artist, length or duration, album))
    if reported:
        track["isrc"] = reported
    # The host reports explicit as true only when the extension says so; false may be
    # clean or unknown, so only explicit is kept.
    if result.get("explicit") is True:
        track["explicit"] = True
    return track


def catalog_recording(track: dict[str, Any], raw: dict[str, Any]) -> Recording:
    """The description of an extension's own track; 404 without a title or artist."""
    title, artist = track["title"], track.get("artist", "")
    if not title.strip() or not artist.strip() or max(len(title), len(artist)) > MAX_TEXT:
        raise BridgeError("track_lacks_details", 404)
    # Synthetic release names are bridge identities, not native matching hints.
    album = text(raw.get("album_name"))
    return track.get("isrc", ""), title, artist, track["durationMs"], album[:MAX_TEXT]


def confirmed_catalog_track(
    catalog: dict[str, Any], recording: Recording, checked: dict[str, Any]
) -> dict[str, Any]:
    """The catalog's track with the checked length and ISRC. Its explicit/clean flags
    stay only when the result reported the track's own ISRC (the same recording)."""
    track = {k: v for k, v in catalog.items() if k not in ("isrc", "explicit", "clean")}
    track["durationMs"] = checked["durationMs"]
    if "isrc" in checked:
        track["isrc"] = checked["isrc"]
    if recording[0] and checked.get("isrc") == recording[0]:
        track |= {k: catalog[k] for k in ("explicit", "clean") if k in catalog}
    if checked.get("explicit"):
        track["explicit"] = True
    return track


def manifest(extension: dict[str, Any], config: Config) -> dict[str, Any]:
    resources = ["search"]
    if extension.get("has_download_provider"):
        # Songs are found by description, as the host's planner does; never by search.
        resources += ["stream", "availability", "cached", "resolve"]
    result: dict[str, Any] = {
        "id": "mornye." + extension["id"],
        "name": extension.get("display_name") or extension["id"],
        "version": extension["version"],
        "resources": resources,
        "allowDownloads": True,
        "settings": [],
    }
    if extension.get("has_metadata_provider"):
        resources += ["catalog", "fullcatalog"]
        catalog = config.catalogs.get(extension["id"], CatalogConfig())
        key = catalog.key or re.sub("[^a-z0-9]", "", extension["id"].lower())
        # Never invent a silently different catalog identity.
        try:
            catalog = CatalogConfig(
                key=key, region=catalog.region, client_artwork=catalog.client_artwork
            )
        except ValueError:
            raise BridgeError("configure_valid_catalog_key", 503) from None
        result["fullCatalog"] = {
            "key": catalog.key,
            "region": catalog.region,
            "clientArtwork": catalog.client_artwork,
        }
    options = quality_options(extension)
    if options and extension.get("has_download_provider"):
        result["settings"] = [
            {
                "key": "format",
                "label": "Format",
                "type": "select",
                "default": selected_format(extension),
                "options": [
                    {"value": x["id"], "label": x.get("label") or x["id"]} for x in options
                ],
            }
        ]
    return result


class Service:
    def __init__(self, config: Config, worker: Worker | None = None) -> None:
        self.config = config
        self.worker = worker or Supervisor(config)
        self.management = Management(config, self.worker)
        self.catalog = Catalog(self.management, self.assert_available)
        self.cache: Cache | None = None
        self.jobs: dict[tuple[str, str, str], asyncio.Task[Entry]] = {}
        self.job_ids: dict[tuple[str, str, str], str] = {}
        self.job_tracks: dict[tuple[str, str, str], dict[str, Any]] = {}
        self.misses: Memory = OrderedDict()
        self.backoffs: Memory = OrderedDict()
        # Other extension failures, with their classified code.
        self.failures: Memory = OrderedDict()
        self.failure_codes: dict[tuple[str, str, str], str] = {}
        # Resolve answers per (extension, recording key): expiry and whether it was found.
        self.resolved: OrderedDict[tuple[str, str], tuple[float, bool]] = OrderedDict()
        self.generations: dict[str, int] = {}
        self.disabled: set[str] = set()
        self.lock_file: BinaryIO | None = None
        self.changing = Claims()
        self.mutation_lock = asyncio.Lock()
        self.access_token = ""
        self.admin_token = ""
        # Automatic sign-in per extension: earliest next attempt and failures in a row.
        self.sign_in_next: dict[str, float] = {}
        self.sign_in_failures: dict[str, int] = {}
        self.sign_in_tasks: dict[str, asyncio.Task[None]] = {}
        # Offered updates per extension: version, registry and state (available,
        # waiting for a sign-in, busy, installed or failed).
        self.updates: dict[str, dict[str, str]] = {}
        self.update_lock = asyncio.Lock()
        self.maintenance: list[asyncio.Task[Any]] = []
        # Signed sessions per extension, saved in SESSIONS_FILE: last seen expiry and
        # observed renewal (ISO times), and the version that no keep-alive renews.
        self.sessions: dict[str, dict[str, Any]] = {}
        # Keep-alive per extension (not saved): earliest next search, failed searches, and
        # searches that left the expiry unchanged, counted per expiry (renewal window).
        self.keep_alive_next: dict[str, float] = {}
        self.keep_alive_misses: dict[str, tuple[str, int]] = {}
        self.keep_alive_failures: dict[str, int] = {}
        # The last recording each extension delivered (its description and format),
        # saved in DELIVERED_FILE: what a brief renewal download asks for again.
        self.delivered: dict[str, tuple[Recording, str]] = {}
        self.renew_next: dict[str, float] = {}
        self.renew_failures: dict[str, int] = {}
        self.delivered_lock = asyncio.Lock()
        # The renewal download in progress (extension and task) and its stop signal.
        self.renewal: tuple[str, asyncio.Task[tuple[str, bool]]] | None = None
        self.renewal_stop = asyncio.Event()
        self.renewal_job = ""
        self.renew_stops: dict[str, tuple[str, int]] = {}
        # Set when shutdown begins: no new renewal starts.
        self.closing = False
        # The automatic upgrade in progress and its extension; shutdown waits for it.
        self.upgrading: asyncio.Task[dict[str, Any]] | None = None
        self.upgrading_extension = ""

    async def start(self) -> None:
        self.config.initialize()
        self.lock_file = (self.config.state_dir / "owner.lock").open("a+b")
        try:
            fcntl.flock(self.lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.lock_file.close()
            self.lock_file = None
            raise BridgeError("state_already_in_use") from None
        self.access_token = (self.config.state_dir / "access.token").read_text().strip()
        self.admin_token = secret_file(
            self.config.state_dir / "admin.token", secrets.token_urlsafe(32)
        )
        # Only the lock owner cleans crash leftovers. Never reuse an unfinished native output.
        for name in ("jobs", "temp"):
            for path in (self.config.state_dir / name).iterdir():
                if path.is_dir() and not path.is_symlink():
                    shutil.rmtree(path)
                else:
                    path.unlink()
        # Downloaded packages are removed after installing; leftovers are from an
        # interrupted run. The registry cache in the same directory stays.
        for path in (self.config.state_dir / "repo").iterdir():
            if path.suffix.lower() in PACKAGE_SUFFIXES and (path.is_file() or path.is_symlink()):
                path.unlink()
        self.cache = Cache(self.config)
        self.management.load_sign_in()
        self.sessions = self.load_state(
            SESSIONS_FILE,
            {
                "expires",
                "renewed",
                "renewed_by",
                "no_keep_alive",
                "download_attempt",
                "download_misses",
                "no_download_renewal",
            },
        )
        self.delivered = self.load_delivered() if self.renewing_by_download else {}
        if not self.renewing_by_download:
            (self.config.state_dir / DELIVERED_FILE).unlink(missing_ok=True)
        self.updates = self.load_state(UPDATES_FILE, {"version", "registry", "state", "host"})

    def load_state(self, name: str, keys: set[str]) -> dict[str, dict[str, Any]]:
        try:
            saved = json.loads((self.config.state_dir / name).read_text())
        except (OSError, ValueError):
            return {}
        if not isinstance(saved, dict):
            return {}
        return {
            key: {k: v for k, v in value.items() if k in keys and isinstance(v, str)}
            for key, value in saved.items()
            if isinstance(key, str) and isinstance(value, dict)
        }

    def load_delivered(self) -> dict[str, tuple[Recording, str]]:
        try:
            saved = json.loads((self.config.state_dir / DELIVERED_FILE).read_text())
        except (OSError, ValueError):
            return {}
        result: dict[str, tuple[Recording, str]] = {}
        for extension, entry in saved.items() if isinstance(saved, dict) else []:
            value = entry.get("recording") if isinstance(entry, dict) else None
            quality = entry.get("quality") if isinstance(entry, dict) else None
            if (
                isinstance(value, list)
                and len(value) == 5
                and all(isinstance(x, str) for x in value[:3] + value[4:])
                and isinstance(value[3], int)
                and isinstance(quality, str)
            ):
                result[extension] = ((value[0], value[1], value[2], value[3], value[4]), quality)
        return result

    async def remember_delivery(self, extension: str, recording: Recording, quality: str) -> None:
        """Remember the recording this extension last delivered (its description only),
        when renewal by a brief download is on. Written off the event loop."""
        if not self.renewing_by_download or self.delivered.get(extension) == (
            recording,
            quality,
        ):
            return
        self.delivered[extension] = (recording, quality)
        await self.save_delivered()

    async def save_delivered(self) -> None:
        async with self.delivered_lock:
            snapshot = {
                key: {"recording": list(value), "quality": chosen}
                for key, (value, chosen) in self.delivered.items()
            }
            path = self.config.state_dir / DELIVERED_FILE
            try:
                if snapshot:
                    await asyncio.to_thread(write_private_json, path, snapshot)
                else:
                    path.unlink(missing_ok=True)
            except OSError:
                pass

    @property
    def renewing_by_download(self) -> bool:
        """Renewal by a brief download runs only with keep-alive on; only then is the last
        delivered recording kept."""
        return self.config.keep_alive and self.config.renew_by_download

    async def forget_deliveries(self, extension: str | None = None) -> None:
        """Forget one extension's delivered recording, or all of them."""
        if extension is None:
            self.delivered.clear()
        elif self.delivered.pop(extension, None) is None:
            return
        await self.save_delivered()

    def save_state(self, name: str, value: dict[str, Any]) -> None:
        try:
            write_private_json(self.config.state_dir / name, value)
        except OSError:
            # Only the dashboard and avoiding a repeated download depend on these files.
            pass

    def start_maintenance(self) -> None:
        """Background checks and automatic sign-in of a serving bridge (not of one-off
        CLI commands)."""
        self.management.on_needs_sign_in = self.needs_sign_in
        self.maintenance += [
            asyncio.create_task(self._repeat(self.check_sessions, SESSION_CHECK_FIRST)),
            asyncio.create_task(self._repeat(self.check_updates, UPDATE_CHECK_FIRST)),
        ]

    async def _repeat(self, job: Any, delay: float) -> None:
        while True:
            await asyncio.sleep(delay)
            try:
                delay = await job()
            except BridgeError as exc:
                log.info("maintenance job=%s outcome=%s", job.__name__, exc.code)
                delay = SIGN_IN_RETRY
            except Exception:
                # Never let one failed round stop the checks; never log native details.
                log.info("maintenance job=%s outcome=internal_error", job.__name__)
                delay = SIGN_IN_RETRY

    def check_updates_soon(self) -> None:
        self.maintenance = [task for task in self.maintenance if not task.done()]
        self.maintenance.append(asyncio.create_task(self._once(self.check_updates)))

    async def _once(self, job: Any) -> None:
        try:
            await job()
        except Exception:
            log.info("maintenance job=%s outcome=internal_error", job.__name__)

    def begin_shutdown(self) -> None:
        """Shutdown has begun (the stop signal): start no renewal, stop a running one."""
        self.closing = True
        self.renewal_stop.set()

    async def stop_renewal(self, extension: str | None = None) -> None:
        """Ask a renewal in progress (on this extension, or any) to stop, and wait for it
        within cancel_timeout; refuse the caller if it is still running then."""
        if not self.renewal or (extension and self.renewal[0] != extension):
            return
        self.renewal_stop.set()
        task = self.renewal[1]
        if self.renewal_job in getattr(self.worker, "gentle_waiting", ()):
            # Its cancel went unanswered and it waits for other calls: refuse at once
            # rather than hold the caller (and the mutation lock).
            raise BridgeError("extension_busy", 503, 5)
        await asyncio.wait({task}, timeout=self.config.cancel_timeout + 1)
        if not task.done():
            raise BridgeError("extension_busy", 503, 5)

    async def close(self) -> None:
        self.begin_shutdown()
        background = self.maintenance + list(self.sign_in_tasks.values())
        if self.renewal:
            background.append(self.renewal[1])
        tasks = list(self.jobs.values())
        for task in [*background, *tasks]:
            task.cancel()
        # An automatic upgrade is not cancelled. Background work (a renewal waiting for an
        # unanswered cancel included), preparations and the upgrade share one
        # shutdown_timeout, so the whole stop stays within the services' stop allowance
        # (graceful HTTP shutdown, then this, then the worker); what is left is finished
        # below once the worker is stopped.
        upgrading = [self.upgrading] if self.upgrading and not self.upgrading.done() else []
        waited = [*background, *tasks, *upgrading]
        if waited:
            await asyncio.wait(waited, timeout=self.config.shutdown_timeout)
        # Kept for the next start: offers and renewals of an interrupted round.
        self.save_state(UPDATES_FILE, self.updates)
        self.save_sessions()
        # Kill ownership before a second cancellation can remove staging.
        await self.worker.close()
        for task in [*tasks, *upgrading, *background]:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, *upgrading, *background, return_exceptions=True)
        if self.cache:
            self.cache.close()
        if self.lock_file:
            self.lock_file.close()
            self.lock_file = None

    def assert_available(self, extension: str) -> None:
        if extension in self.changing:
            raise BridgeError("extension_changing", retry_after=2)
        if extension in self.disabled:
            raise BridgeError("extension_disabled", 404)

    def check_snapshot(self, extension: dict[str, Any]) -> None:
        self.assert_available(extension["id"])
        if not extension.get("enabled"):
            raise BridgeError("extension_disabled", 404)
        if extension.get("_generation", 0) != self.generations.get(extension["id"], 0):
            raise BridgeError("extension_changed_retry", retry_after=2)

    async def extension(self, extension: str) -> dict[str, Any]:
        self.assert_available(extension)
        generation = self.generations.get(extension, 0)
        result = dict(await self.management.extension(extension))
        result["_generation"] = generation
        self.check_snapshot(result)
        return result

    @staticmethod
    def remember(
        memory: OrderedDict[Any, Any], key: Any, value: Any, limit: int = MEMORY_ENTRIES
    ) -> None:
        memory[key] = value
        memory.move_to_end(key)
        while len(memory) > limit:
            memory.popitem(last=False)

    def remembered(
        self, extension: str, wanted: Mapping[str, str], quality: str
    ) -> BridgeError | None:
        """The remembered answer for a track ID or a described recording: a miss (404),
        a timeout backoff or another extension failure (its classified code)."""
        now = time.monotonic()
        for memory in (self.misses, self.backoffs, self.failures):
            for key in list(memory):
                if memory[key][0] <= now:
                    del memory[key]
                    self.failure_codes.pop(key, None)
        ident = internal_id(wanted["id"]) if wanted.get("id") else None
        for memory in (self.misses, self.backoffs, self.failures):
            for key, (expires, track) in memory.items():
                if (
                    key[0] == extension
                    and key[2] == quality
                    and (key[1] == ident or (track is not None and self.matches(track, wanted)))
                ):
                    if memory is self.misses:
                        return BridgeError("recording_unavailable", 404)
                    retry = int(expires - now) + 1
                    if memory is self.backoffs:
                        return BridgeError("download_backoff", retry_after=retry)
                    return BridgeError(self.failure_codes.get(key, "extension_failed"), 502)
        return None

    def unavailable(self, extension: str, wanted: Mapping[str, str], quality: str) -> bool:
        return self.remembered(extension, wanted, quality) is not None

    def recording_ident(self, recording: Recording) -> str:
        ident = recording_id(recording)
        if ident is None:
            digest = hashlib.sha256(json.dumps(recording).encode()).digest()[:16]
            assert self.cache
            self.cache.remember_recording(digest, list(recording))
            ident = reference_id(digest)
        return ident

    def known_recording(self, ident: str) -> Recording | None:
        """The recording of a recording ID; None for a native ID or a forgotten one."""
        if not ident.startswith(RECORDING_PREFIX):
            return None
        parsed = parse_recording(ident)
        if not isinstance(parsed, bytes):
            return parsed
        assert self.cache
        stored = self.cache.recording(parsed)
        if not stored or len(stored) != 5 or not isinstance(stored[3], int):
            return None
        isrc, title, artist, duration, album = stored
        recording = (str(isrc), str(title), str(artist), duration, str(album))
        # A row is used only for the digest it was stored under.
        digest = hashlib.sha256(json.dumps(recording).encode()).digest()[:16]
        return recording if digest == parsed else None

    def recording(self, ident: str) -> Recording | None:
        """known_recording, failing for a forgotten reference (pruned): the caller looks
        the song up again."""
        recording = self.known_recording(ident)
        if recording is None and ident.startswith(RECORDING_PREFIX):
            raise BridgeError("not_found", 404)
        return recording

    def slots_free(self, extension: dict[str, Any]) -> bool:
        own = sum(1 for key in self.jobs if key[0] == extension["id"])
        return len(self.jobs) < self.config.preparations and own < transfer_slots(extension)

    def begin(
        self,
        extension: dict[str, Any],
        native_id: str,
        quality: str,
        recording: Recording | None = None,
    ) -> asyncio.Task[Entry]:
        """Start (or join) preparing a track: a native ID, or a recording key with its
        recording."""
        self.check_snapshot(extension)
        if native_id.startswith(RECORDING_KEY) and recording is None:
            raise BridgeError("not_found", 404)
        key = (extension["id"], native_id, quality)
        entry = self.ready(extension["id"], native_id, quality, recording)
        if entry:

            async def cached() -> Entry:
                return entry

            return asyncio.create_task(cached())
        task = self.jobs.get(key)
        if task:
            return task
        remembered = self.remembered(extension["id"], {"id": external_id(native_id)}, quality)
        if remembered:
            raise remembered
        if not self.slots_free(extension):
            raise BridgeError("preparation_busy", retry_after=3)
        if self.renewal and self.renewal[0] == extension["id"]:
            # A preparation is never delayed by a renewal on its extension: ask it to stop.
            self.renewal_stop.set()
        task = asyncio.create_task(self._prepare(extension, native_id, quality, key, recording))
        self.jobs[key] = task
        if recording is not None:
            self.job_tracks[key] = recording_track(external_id(native_id), recording)

        def finished(done: asyncio.Task[Entry]) -> None:
            self.jobs.pop(key, None)
            self.job_ids.pop(key, None)
            self.job_tracks.pop(key, None)
            if not done.cancelled():
                # Consume failures even after the requesting connection is gone.
                done.exception()

        task.add_done_callback(finished)
        return task

    def ready(
        self, extension: str, native_id: str, quality: str, recording: Recording | None
    ) -> Entry | None:
        assert self.cache
        entry = self.cache.get(extension, native_id, quality)
        if entry is None and recording is not None and recording[0]:
            # The same ISRC is the same recording, whichever ID prepared it.
            entry = self.cached_match(extension, {"isrc": recording[0]}, quality=quality)
        return entry

    async def stream(self, extension: dict[str, Any], ident: str, quality: str) -> Entry:
        native_id = internal_id(ident)
        assert self.cache
        entry = self.cache.get(extension["id"], native_id, quality)
        if entry:
            return entry
        task = self.begin(extension, native_id, quality, self.recording(ident))
        try:
            return await asyncio.wait_for(asyncio.shield(task), self.config.stream_wait)
        except TimeoutError:
            # Shijhon asks again after Retry-After while its budget lasts.
            raise BridgeError("preparing", retry_after=2) from None

    async def resolve(self, extension: dict[str, Any], wanted: Mapping[str, str]) -> dict[str, Any]:
        """Find a described recording as the host's download planner does: the
        extension's checkAvailability with ISRC, title, artist and options. The item
        echoes the recording under a recording ID; stream/<that ID> downloads it."""
        self.check_snapshot(extension)
        recording = wanted_recording(wanted)
        ident = self.recording_ident(recording)
        key = (extension["id"], RECORDING_KEY + ident)
        started = time.monotonic()
        source, isrc = "memory", recording[0]
        remembered = self.resolved.get(key)
        if remembered and remembered[0] > started:
            found = remembered[1]
        elif self.cache and any(
            entry.native_id == key[1] or (isrc and entry.metadata.get("isrc") == isrc)
            for entry in self.cache.entries(extension["id"])
        ):
            source, found = "cache", True
        else:
            source = "native"
            try:
                result = await self.management.raw(
                    extension["id"], "checkAvailability", availability_arguments(recording)
                )
                self.check_snapshot(extension)
                # As the host: no answer is "not available"; only available:true is found.
                if result is not None and (
                    not isinstance(result, dict) or not isinstance(result.get("available"), bool)
                ):
                    raise BridgeError("invalid_native_result", 502)
            except BridgeError as exc:
                self.log_resolve(extension["id"], source, started, exc.code)
                raise
            found = result is not None and result["available"]
            if result is not None and not found:
                refused = availability_refusal(result)
                if refused:
                    if refused.code == "needs_login_or_verification":
                        self.management.needs_sign_in(
                            extension["id"], "invokeExtensionAction:checkAvailability"
                        )
                    self.log_resolve(extension["id"], source, started, refused.code)
                    raise refused
            seconds = FOUND_SECONDS if found else MISS_SECONDS
            self.remember(self.resolved, key, (time.monotonic() + seconds, found))
        self.log_resolve(extension["id"], source, started, "found" if found else "not_found")
        if not found:
            raise BridgeError("recording_unavailable", 404)
        item = {k: v for k, v in recording_track(ident, recording).items() if k != "album"}
        if not recording[3]:
            del item["durationMs"]
        return {"item": item}

    @staticmethod
    def log_resolve(extension: str, source: str, started: float, outcome: str) -> None:
        log.info(
            "resolve extension=%s source=%s time=%.2fs outcome=%s",
            extension,
            source,
            time.monotonic() - started,
            outcome,
        )

    async def _prepare(
        self,
        extension: dict[str, Any],
        native_id: str,
        quality: str,
        key: tuple[str, str, str],
        recording: Recording | None = None,
    ) -> Entry:
        assert self.cache
        job_id = "bridge-" + secrets.token_hex(16)
        self.job_ids[key] = job_id
        started = time.monotonic()
        directory = (self.config.state_dir / "jobs" / job_id).resolve()
        directory.mkdir(mode=0o700)
        # Phase timings for the log: each phase ends when the next begins.
        phases = {"metadata": 0.0, "native": 0.0, "finalize": 0.0}
        current, mark, outcome = "metadata", time.monotonic(), "internal_error"

        def phase(following: str) -> None:
            nonlocal current, mark
            now = time.monotonic()
            phases[current] += now - mark
            current, mark = following, now

        try:
            catalog_track = None
            if recording is None:
                # An extension's own track: read its details, then find and download it
                # by those details like any described recording.
                raw = await self.catalog.native_item(extension["id"], "track", native_id)
                self.check_snapshot(extension)
                catalog_track = map_item(raw, "track")
                self.job_tracks[key] = catalog_track
                recording = catalog_recording(catalog_track, raw)
            payload = strategy_payload(extension["id"], directory, quality, job_id, recording)
            phase("native")
            result = await self.management.call(
                "downloadByStrategy", json.dumps(payload), extension["id"]
            )
            phase("finalize")
            if not isinstance(result, dict) or not result.get("success"):
                raise native_error(result)
            path = Path(result.get("file_path", "")).resolve()
            if not path.is_relative_to(directory) or not path.is_file():
                raise BridgeError("invalid_download_path", 502)
            path, audio = await finalize(path, result, self.config, self.worker)
            # Publish what the file is, never only what was asked for.
            track = verified_track(external_id(native_id), recording, result, audio)
            if catalog_track is not None:
                track = confirmed_catalog_track(catalog_track, recording, track)
            entry = self.cache.publish(extension["id"], native_id, quality, path, track, audio)
            await self.remember_delivery(extension["id"], recording, quality)
            outcome = "ready"
            return entry
        except BridgeError as exc:
            outcome = exc.code
            now = time.monotonic()
            restarted_by_renewal = (
                exc.code == "worker_restarted"
                and getattr(self.worker, "gentle_restart_at", 0.0) >= started
            )
            if restarted_by_renewal:
                # The worker restart that ended it came from a renewal's cancel: no backoff.
                pass
            elif exc.code in {"download_timeout", "worker_timeout", "worker_restarted"}:
                self.remember(
                    self.backoffs,
                    key,
                    (now + self.config.download_backoff, self.job_tracks.get(key)),
                )
            elif exc.status == 404:
                self.remember(self.misses, key, (now + MISS_SECONDS, self.job_tracks.get(key)))
            elif exc.status == 502:
                # Extension and native-answer failures repeat for a while; remember them.
                self.remember(self.failures, key, (now + FAILURE_SECONDS, self.job_tracks.get(key)))
                self.failure_codes[key] = exc.code
                for gone in set(self.failure_codes) - set(self.failures):
                    del self.failure_codes[gone]
            raise
        except asyncio.CancelledError:
            # Supervisor waits for C + item cancellation before allowing staging removal.
            outcome = "cancelled"
            raise
        finally:
            shutil.rmtree(directory, ignore_errors=True)
            phase(current)
            log.info(
                "preparation extension=%s kind=%s format=%s metadata=%.2fs native=%.2fs "
                "finalize=%.2fs outcome=%s",
                extension["id"],
                "recording" if native_id.startswith(RECORDING_KEY) else "native",
                quality,
                phases["metadata"],
                phases["native"],
                phases["finalize"],
                outcome,
            )

    async def cancel(self, extension: str, ident: str, quality: str) -> dict[str, bool]:
        key = (extension, internal_id(ident), quality)
        task = self.jobs.get(key)
        if task:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        return {"cancelled": task is not None}

    @staticmethod
    def matches(track: dict[str, Any], wanted: Mapping[str, str]) -> bool:
        if wanted.get("id") and track["id"] == wanted["id"]:
            return True
        isrc = wanted.get("isrc", "").upper()
        if isrc and track.get("isrc") == isrc:
            return True
        if not wanted.get("title") or not wanted.get("artist"):
            return False
        try:
            duration = int(wanted.get("durationMs", "0"))
        except ValueError:
            return False

        def normalize(value: str) -> str:
            return " ".join(
                "".join(
                    c
                    for c in unicodedata.normalize("NFKD", value.casefold())
                    if not unicodedata.combining(c)
                ).split()
            )

        def artists(value: str) -> set[str]:
            return {
                normalize(x)
                for x in re.split(
                    r"\s*(?:,|;|&|/|\b(?:feat|ft)\.?(?=\s|$)|\band\b)\s*", value, flags=re.I
                )
                if normalize(x)
            }

        if (
            normalize(track["title"]) != normalize(wanted["title"])
            or not artists(track.get("artist", "")).intersection(artists(wanted["artist"]))
            or duration <= 0
            or abs(track["durationMs"] - duration) > 3000
        ):
            return False
        version = wanted.get("version")
        return not version or track.get("explicit") is (version == "explicit")

    def cached_match(
        self, extension: str, wanted: dict[str, str], *, quality: str | None = None
    ) -> Entry | None:
        assert self.cache
        entries = [
            entry
            for entry in self.cache.entries(extension)
            if quality is None or entry.quality == quality
        ]
        for key in ("id", "isrc"):
            if wanted.get(key):
                exact = next(
                    (entry for entry in entries if entry.metadata.get(key) == wanted[key]), None
                )
                if exact:
                    return exact
        return next((entry for entry in entries if self.matches(entry.metadata, wanted)), None)

    def preparing(self, extension: str, wanted: dict[str, str], quality: str) -> bool:
        return any(
            key[0] == extension and key[2] == quality and self.matches(track, wanted)
            for key, track in self.job_tracks.items()
        )

    async def execute(
        self, operation: str, params: dict[str, Any], *, private: bool = False
    ) -> Any:
        serialized = {
            "rotate-tokens",
            "available",
            "registry.get",
            "registries.get",
            "install",
            "update",
            "remove",
            "enable",
            "disable",
            "registry.set",
            "registry.available",
            "registries.save",
            "catalog.save",
            "settings.set",
            "action",
            "pending-auth.grant",
            "pending-auth.callback",
        }
        if operation not in serialized:
            return await self._execute(operation, params, private=private)
        if self.upgrading_extension and params.get("extension") == self.upgrading_extension:
            # Its automatic upgrade runs without the mutation lock; wait for it.
            raise BridgeError("extension_updating", 503, 5)
        async with self.mutation_lock:
            ext = str(params.get("extension", ""))
            if self.upgrading_extension and ext == self.upgrading_extension:
                # Queued while the upgrade's package was downloaded: still refused.
                raise BridgeError("extension_updating", 503, 5)
            if ext:
                await self.stop_renewal(ext)
            self.changing.add(ext)
            try:
                result = await self._execute(operation, params, private=private)
                if operation in {"disable", "remove"}:
                    self.disabled.add(ext)
                elif operation in {"enable", "install", "update"}:
                    self.disabled.discard(ext)
                return result
            finally:
                if operation in {
                    "enable",
                    "disable",
                    "remove",
                    "install",
                    "update",
                    "settings.set",
                    "action",
                }:
                    self.generations[ext] = self.generations.get(ext, 0) + 1
                    self.management.invalidate()
                self.changing.discard(ext)

    async def _execute(
        self, operation: str, params: dict[str, Any], *, private: bool = False
    ) -> Any:
        if operation in (
            "settings.set",
            "action",
            "pending-auth.grant",
            "pending-auth.callback",
            "enable",
            "disable",
            "remove",
            "update",
        ):
            self.forget(str(params.get("extension", "")))
        if operation == "remove":
            # A removed extension delivers nothing to renew with.
            await self.forget_deliveries(str(params.get("extension", "")))
        if operation in ("remove", "update"):
            # An update installed by hand or a removal replaces any automatic one.
            if self.updates.pop(str(params.get("extension", "")), None) is not None:
                self.save_state(UPDATES_FILE, self.updates)
        if operation in ("disable", "remove", "update"):
            extension = params.get("extension")
            tasks = [task for key, task in self.jobs.items() if key[0] == extension]
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        if operation == "registries.get":
            if self.config.registries is not None:
                return self.config.registries
            current = (await self.management.execute("registry.get", {}))["registry"]
            return [current] if current else []
        if operation == "registries.save":
            from urllib.parse import urlsplit

            urls = params["urls"]
            if (
                not isinstance(urls, list)
                or len(urls) > 20
                or any(
                    not isinstance(url, str)
                    or urlsplit(url).scheme != "https"
                    or not urlsplit(url).hostname
                    or urlsplit(url).username
                    or urlsplit(url).password
                    or urlsplit(url).fragment
                    for url in urls
                )
            ):
                raise BridgeError("registry_must_use_https", 400)
            self.config.persist(registries=list(dict.fromkeys(urls)))
            return {"saved": True}
        if operation == "catalog.save":
            await self.management.extension(params["extension"])
            catalog = CatalogConfig.model_validate(params["catalog"])
            catalogs = {
                key: value.model_dump(exclude_none=True)
                for key, value in self.config.catalogs.items()
            }
            catalogs[params["extension"]] = catalog.model_dump(exclude_none=True)
            self.config.persist(catalogs=catalogs)
            return {"saved": True}
        if operation == "registry.available" or (
            operation in ("install", "update") and "registry" in params
        ):
            return await self.on_registry(
                params["registry"],
                self.management.execute(
                    "available" if operation == "registry.available" else operation,
                    params,
                    private=private,
                ),
            )
        if operation == "cancel":
            extension = await self.extension(params["extension"])
            return await self.cancel(
                extension["id"], params["id"], selected_format(extension, params.get("format"))
            )
        if operation == "rotate-tokens":
            for name in ("access", "admin"):
                value = secrets.token_urlsafe(32)
                target = self.config.state_dir / (name + ".token")
                temporary = self.config.state_dir / (".token-" + secrets.token_hex(12))
                try:
                    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                    with os.fdopen(fd, "w") as stream:
                        stream.write(value)
                        stream.flush()
                        os.fsync(stream.fileno())
                    os.replace(temporary, target)
                finally:
                    temporary.unlink(missing_ok=True)
                setattr(self, name + "_token", value)
            return {"rotated": True, "recopy_addon_links": True}
        if operation == "check":
            installed = await self.management.installed()
            return {
                "ok": True,
                "extensions": len(installed),
                "host_version": host_config.HOST_VERSION,
                "worker_isolated": True,
            }
        if operation == "links":
            if not private:
                raise BridgeError("links_require_private_output", 400)
            origin = self.config.public_url.rstrip("/") or f"http://127.0.0.1:{self.config.port}"
            from urllib.parse import quote

            return {
                item[
                    "id"
                ]: f"{origin}/addons/{self.access_token}/{quote(item['id'], safe='')}/manifest.json"
                for item in await self.management.installed()
                if item.get("enabled")
            }
        return await self.management.execute(operation, params, private=private)

    async def on_registry(self, registry: str, work: Any) -> Any:
        """Run native registry work with this saved registry selected, then restore the
        prior selection (the pinned host has one active registry). Hold mutation_lock."""
        try:
            if registry not in await self._execute("registries.get", {}):
                raise BridgeError("registry_not_saved", 400)
            previous = (await self.management.execute("registry.get", {}))["registry"]
        except BaseException:
            work.close()
            raise
        try:
            await self.management.execute("registry.set", {"url": registry})
            return await work
        finally:
            if previous:
                await self.management.execute("registry.set", {"url": previous})
            else:
                await self.management.call("clearRepoRegistryUrl")

    def forget(self, extension: str) -> None:
        """Drop remembered answers of an extension whose settings, sign-in or code changed."""
        self.management.invalidate()
        self.catalog.isrc_failures.pop(extension, None)
        for memory in (self.misses, self.backoffs, self.failures):
            for key in [key for key in memory if key[0] == extension]:
                del memory[key]
                self.failure_codes.pop(key, None)
        for found in [found for found in self.resolved if found[0] == extension]:
            del self.resolved[found]

    def needs_sign_in(self, extension: str) -> None:
        """An operation reported a missing sign-in: install an update that was waiting
        for this, then try signing in automatically when that is due."""
        if extension in self.sign_in_tasks:
            return
        waiting = self.config.auto_update and (
            self.updates.get(extension, {}).get("state") == "waiting"
        )
        automatic = extension not in self.management.manual and (
            time.monotonic() >= self.sign_in_next.get(extension, 0)
        )
        if not waiting and not automatic:
            return
        task = asyncio.create_task(self._after_sign_out(extension, automatic))
        self.sign_in_tasks[extension] = task

        def finished(done: asyncio.Task[None]) -> None:
            if self.sign_in_tasks.get(extension) is done:
                del self.sign_in_tasks[extension]
            if not done.cancelled():
                done.exception()

        task.add_done_callback(finished)

    async def _after_sign_out(self, extension: str, automatic: bool) -> None:
        """Try signing in automatically first. Install an update that waits for a sign-in
        only once the extension is shown to be signed out: the automatic try needs the
        operator, or the host has opened a challenge. A single report alone is not enough."""
        outcome = await self.sign_in_automatically(extension) if automatic else ""
        if not self.config.auto_update or self.updates.get(extension, {}).get("state") != "waiting":
            return
        if outcome != "needs_operator" and not (outcome == "" and await self.challenge(extension)):
            return
        try:
            await self.update_waiting(extension)
        except BridgeError:
            pass

    async def challenge(self, extension: str) -> bool:
        """Whether the host shows a sign-in challenge for the extension."""
        try:
            async with asyncio.timeout(CHECK_TIMEOUT):
                return bool(
                    await self.execute("pending-auth.show", {"extension": extension}, private=True)
                )
        except (BridgeError, TimeoutError):
            return False

    async def sign_in_automatically(self, extension: str) -> str:
        """Sign in again once without the operator: a signed-session extension asks the
        host to check its saved session (the gateway may issue a new one); another
        extension runs its declared sign-in action. Anything that asks for input or a
        browser page is left for the sign-in page."""
        failures = self.sign_in_failures.get(extension, 0)
        self.sign_in_next[extension] = time.monotonic() + min(
            SIGN_IN_RETRY * 2**failures, SIGN_IN_RETRY_MAX
        )
        params = {"extension": extension}
        try:
            async with asyncio.timeout(CHECK_TIMEOUT * 3):
                item = await self.management.extension(extension)
                action = login_action(item)
                if not item.get("enabled") or extension in self.disabled:
                    outcome = "disabled"
                elif self.management.signed_session(extension):
                    pending = await self.execute("pending-auth.show", params, private=True)
                    if pending:
                        outcome = "needs_operator"
                    elif extension in self.management.states:
                        # No challenge, but nothing shows the session works: the saved
                        # session has merely not expired. Leave it for the operator.
                        outcome = "unconfirmed"
                    else:
                        outcome = "signed_in"
                elif not action:
                    outcome = "no_automatic_sign_in"
                else:
                    result = await self.execute("action", params | {"action": action}, private=True)
                    if has_action_form(result) or result_browser_url(result):
                        outcome = "needs_operator"
                        self.management.needs_sign_in(extension, "invokeExtensionAction:" + action)
                    elif await self.execute("pending-auth.show", params, private=True):
                        outcome = "needs_operator"
                    elif extension in self.management.states:
                        outcome = "unconfirmed"
                    else:
                        outcome = "signed_in"
        except BridgeError as exc:
            outcome = "needs_operator" if exc.code in NEEDS_AUTH else exc.code
        except TimeoutError:
            outcome = "timeout"
        if outcome == "signed_in":
            self.sign_in_failures.pop(extension, None)
            try:
                async with asyncio.timeout(CHECK_TIMEOUT):
                    await self.execute("health", params)
            except (BridgeError, TimeoutError):
                pass
        elif outcome in {"needs_operator", "no_automatic_sign_in", "unconfirmed"}:
            self.management.manual.add(extension)
        elif outcome != "disabled":
            self.sign_in_failures[extension] = failures + 1
        log.info("sign-in extension=%s automatic=yes outcome=%s", extension, outcome)
        return outcome

    async def check_sessions(self) -> float:
        """Notice lapsed sign-ins before playback does: the host checks each saved
        signed session locally and asks its gateway only when it is not usable. Then keep
        signed-in sessions alive. Returns the delay until the next check."""
        delay = SESSION_CHECK_SECONDS
        for item in await self.management.installed():
            extension = item["id"]
            if (
                not item.get("enabled")
                or extension in self.disabled
                or extension in self.changing
                or any(key[0] == extension for key in self.jobs)
            ):
                continue
            if self.management.states.get(extension) in NEEDS_AUTH:
                self.needs_sign_in(extension)
            elif self.management.signed_session(extension):
                try:
                    async with asyncio.timeout(CHECK_TIMEOUT):
                        await self.execute(
                            "pending-auth.show", {"extension": extension}, private=True
                        )
                except (BridgeError, TimeoutError):
                    pass
                if self.management.states.get(extension) not in NEEDS_AUTH:
                    delay = min(delay, await self.keep_alive(item))
        return max(60.0, delay)

    def session(self, extension: str) -> tuple[bool, datetime | None] | None:
        """Read the saved signed session and record an observed renewal: a later expiry
        than the last one seen."""
        manifest = installed_manifest(self.config.state_dir / "extensions", extension)
        saved = saved_session(self.config.state_dir / "data", manifest)
        if saved is None:
            return None
        held, expires = saved
        info = self.sessions.setdefault(extension, {})
        seen = info.get("expires")
        current = expires.isoformat() if held and expires else None
        if current != seen:
            previous = parse_time(seen)
            if current and expires and previous and expires > previous:
                info["renewed"] = datetime.now(UTC).isoformat(timespec="seconds")
                info.pop("renewed_by", None)
                # Renewed by anything, a download included: misses start over.
                self.keep_alive_misses.pop(extension, None)
            info["expires"] = current
            self.save_sessions()
        return held, expires

    def save_sessions(self) -> None:
        self.save_state(SESSIONS_FILE, self.sessions)

    async def keep_alive(self, item: dict[str, Any]) -> float:
        """Make one signed request inside the renewal window of a signed-in session, so
        the host renews it. Never a download: a one-result catalog search. Returns the
        delay until this extension needs another look."""
        extension = item["id"]
        saved = self.session(extension)
        if not self.config.keep_alive or saved is None or not saved[0] or saved[1] is None:
            return SESSION_CHECK_SECONDS
        expires = saved[1]
        info = self.sessions.setdefault(extension, {})
        version = str(item.get("version") or "")
        if info.get("no_keep_alive") == version:
            # Its searches do not renew the session: try a brief download instead.
            return await self.renew_by_download(item, expires)
        if info.get("no_keep_alive"):
            # Another version may sign its searches.
            del info["no_keep_alive"]
            self.save_sessions()
        remaining = (expires - datetime.now(UTC)).total_seconds()
        if remaining <= KEEP_ALIVE_LATEST:
            # Too late to renew; the next use asks the gateway again.
            return SESSION_CHECK_SECONDS
        if remaining > KEEP_ALIVE_WINDOW:
            return remaining - KEEP_ALIVE_LEAD
        wait = self.keep_alive_next.get(extension, 0) - time.monotonic()
        if wait > 0:
            return min(wait, remaining - KEEP_ALIVE_LATEST)
        if not item.get("has_metadata_provider"):
            outcome = "no_signed_request"
            info["no_keep_alive"] = version
        else:
            filters = [
                str(entry["id"])
                for entry in (item.get("search_behavior") or {}).get("filters") or []
                if isinstance(entry, dict) and isinstance(entry.get("id"), str)
            ]
            # The smallest answer first: one artist, else one album, else the default.
            chosen = next(
                (x for x in filters if "artist" in x.lower()),
                next((x for x in filters if "album" in x.lower()), filters[0] if filters else ""),
            )
            options: dict[str, Any] = {"limit": 1, "offset": 0}
            if chosen:
                options["filter"] = chosen
            self.keep_alive_next[extension] = time.monotonic() + KEEP_ALIVE_SPACING
            try:
                async with asyncio.timeout(CHECK_TIMEOUT):
                    await self.management.raw(
                        extension, "customSearch", [KEEP_ALIVE_QUERY, options]
                    )
            except (BridgeError, TimeoutError) as exc:
                failures = self.keep_alive_failures.get(extension, 0) + 1
                self.keep_alive_failures[extension] = failures
                self.keep_alive_next[extension] = time.monotonic() + min(
                    KEEP_ALIVE_RETRY * 2 ** (failures - 1), SIGN_IN_RETRY_MAX
                )
                code = exc.code if isinstance(exc, BridgeError) else "timeout"
                log.info("keep-alive extension=%s outcome=%s", extension, code)
                return SESSION_CHECK_SECONDS
            self.keep_alive_failures.pop(extension, None)
            after = self.session(extension)
            if after and after[0] and after[1] and after[1] > expires:
                outcome = "renewed"
                self.keep_alive_misses.pop(extension, None)
            else:
                # Searched, yet the expiry did not move: count it for this renewal window
                # only. A failed search above is not a miss.
                window, misses = self.keep_alive_misses.get(extension, ("", 0))
                misses = misses + 1 if window == expires.isoformat() else 1
                self.keep_alive_misses[extension] = (expires.isoformat(), misses)
                outcome = "not_renewed"
                if misses >= KEEP_ALIVE_MISSES:
                    outcome = "no_signed_request"
                    info["no_keep_alive"] = version
                    self.keep_alive_misses.pop(extension, None)
        self.save_sessions()
        log.info("keep-alive extension=%s outcome=%s", extension, outcome)
        if info.get("no_keep_alive") == version:
            # Renewal by a brief download may still fit this window: look again soon.
            return 60.0
        return SESSION_CHECK_SECONDS

    async def renew_by_download(self, item: dict[str, Any], expires: datetime) -> float:
        """Renew a session whose extension signs only its downloads: inside the renewal
        window, at most once per session, start preparing the last recording this
        extension delivered and cancel it as soon as the session is renewed or the audio
        transfer begins. The file is never kept, published or counted as a delivery.
        Returns the delay until this extension needs another look."""
        extension = item["id"]
        info = self.sessions.setdefault(extension, {})
        version = str(item.get("version") or "")
        if not self.config.renew_by_download:
            return SESSION_CHECK_SECONDS
        if info.get("no_download_renewal") not in (None, version):
            # Another version is tried again.
            info.pop("no_download_renewal", None)
            info.pop("download_misses", None)
            self.save_sessions()
        if info.get("no_download_renewal") == version:
            return SESSION_CHECK_SECONDS
        now = datetime.now(UTC)
        remaining = (expires - now).total_seconds()
        if remaining <= KEEP_ALIVE_LATEST:
            return SESSION_CHECK_SECONDS
        if remaining > KEEP_ALIVE_WINDOW:
            return remaining - KEEP_ALIVE_LEAD
        # One attempt per expiry: a renewal by anything else (a real download) moves the
        # expiry, which starts a new session lifetime with its own window.
        if info.get("download_attempt") == expires.isoformat():
            return SESSION_CHECK_SECONDS
        if self.closing or self.renewal:
            return SESSION_CHECK_SECONDS
        wait = self.renew_next.get(extension, 0) - time.monotonic()
        if wait > 0:
            return min(wait, remaining - KEEP_ALIVE_LATEST)
        delivered = self.delivered.get(extension)
        if delivered is None:
            # Nothing delivered yet: never invent or search for a recording.
            return SESSION_CHECK_SECONDS
        if (
            extension in self.changing
            or extension in self.disabled
            or extension == self.upgrading_extension
            or self.management.states.get(extension) in NEEDS_AUTH
            # Not while the extension prepares a file (that renews it anyway) or while no
            # slot is free; other extensions' preparations continue beside it.
            or any(key[0] == extension for key in self.jobs)
            or not self.slots_free(item)
        ):
            return min(300.0, remaining - KEEP_ALIVE_LATEST)
        recording, quality = delivered
        try:
            quality = selected_format(item, quality)
        except BridgeError:
            return SESSION_CHECK_SECONDS
        self.renewal_stop = asyncio.Event()
        task = asyncio.create_task(self.brief_download(extension, recording, quality, expires))
        self.renewal = (extension, task)
        try:
            outcome, started = await asyncio.shield(task)
        finally:
            if task.done() and self.renewal and self.renewal[1] is task:
                self.renewal = None
        used = outcome != "stopped" and (started or outcome in {"renewed", "not_renewed"})
        if used:
            # The host started the download: this session's one attempt is used.
            info["download_attempt"] = expires.isoformat()
        delay = SESSION_CHECK_SECONDS
        if outcome == "stopped":
            # Stopped for a preparation, a change or shutdown: tried again shortly, a few
            # times per window at most.
            window, stops = self.renew_stops.get(extension, ("", 0))
            stops = stops + 1 if window == expires.isoformat() else 1
            self.renew_stops[extension] = (expires.isoformat(), stops)
            if stops >= RENEWAL_STOPS:
                outcome = "stopped_too_often"
                info["download_attempt"] = expires.isoformat()
            else:
                delay = 120.0
                self.renew_next[extension] = time.monotonic() + delay
        elif outcome == "renewed":
            self.renew_failures.pop(extension, None)
            info.pop("download_misses", None)
            info["renewed_by"] = "download"
        elif outcome == "not_renewed":
            misses = int(info.get("download_misses") or 0) + 1
            info["download_misses"] = str(misses)
            if misses >= RENEWAL_MISSES:
                outcome = "gave_up"
                info["no_download_renewal"] = version
        else:
            # Not a miss. Before the host started, tried again inside this window after a
            # short backoff; after it started, this session's attempt is used and the
            # backoff applies to the next session's.
            failures = self.renew_failures.get(extension, 0) + 1
            self.renew_failures[extension] = failures
            if used:
                backoff = min(KEEP_ALIVE_RETRY * 2 ** (failures - 1), SIGN_IN_RETRY_MAX)
            else:
                backoff = min(RENEWAL_RETRY * 2 ** (failures - 1), KEEP_ALIVE_RETRY)
                delay = backoff
            self.renew_next[extension] = time.monotonic() + backoff
        self.save_sessions()
        log.info("renewal extension=%s kind=brief_download outcome=%s", extension, outcome)
        return delay

    def download_progress(self, job_id: str) -> dict[str, Any] | None:
        progress = getattr(self.worker, "progress", None)
        value = progress(job_id) if callable(progress) else None
        return value if isinstance(value, dict) else None

    async def brief_download(
        self, extension: str, recording: Recording, quality: str, expires: datetime
    ) -> tuple[str, bool]:
        """Start the host's download of the recording in a private directory and cancel
        it at the first sign that it did its work: a later session expiry, or the audio
        transfer starting (the extension's signed requests come before it). Also stops
        when asked (a preparation starts, the extension changes, shutdown). Returns the
        outcome (renewed, not_renewed, or a failure code) and whether the host started."""
        job_id = "bridge-renewal-" + secrets.token_hex(12)
        self.renewal_job = job_id
        directory = (self.config.state_dir / "jobs" / job_id).resolve()
        try:
            directory.mkdir(mode=0o700)
        except OSError:
            return "staging_failed", False
        payload = strategy_payload(extension, directory, quality, job_id, recording)
        # A renewal's cancel never restarts the worker while other calls run.
        gentle = getattr(self.worker, "gentle", None)
        if isinstance(gentle, set):
            gentle.add(job_id)
        # Directly to the worker: not a preparation, delivery or cache entry.
        task = asyncio.create_task(self.worker.call("downloadByStrategy", json.dumps(payload)))
        deadline = time.monotonic() + RENEWAL_LIMIT
        started = reached = timed_out = stopped = False
        try:
            while not task.done():
                await asyncio.wait({task}, timeout=RENEWAL_POLL)
                later = self.management.session_expiry(extension)
                if later is not None and later > expires:
                    break
                progress = self.download_progress(job_id)
                # Started once the host reported any stage, not when the request was sent.
                started = started or bool(progress)
                received = (progress or {}).get("bytes_received")
                if str((progress or {}).get("status") or "").lower() in TRANSFER_STATUSES or (
                    isinstance(received, (int, float)) and received > 0
                ):
                    reached = True
                    break
                if self.renewal_stop.is_set():
                    stopped = True
                    break
                if time.monotonic() > deadline:
                    # Slow before the transfer (an availability check, say): not a miss.
                    timed_out = True
                    break
        finally:
            if not task.done():
                task.cancel()
            # The supervisor keeps the call until the native download answers its
            # cancellation (or the worker stops); only then is the folder removed. If
            # shutdown cancels this wait, the folder stays: the worker is killed first and
            # the next start sweeps jobs/.
            results = await asyncio.gather(task, return_exceptions=True)
            shutil.rmtree(directory, ignore_errors=True)
            if isinstance(gentle, set):
                gentle.discard(job_id)
        after = self.session(extension)
        if after and after[0] and after[1] and after[1] > expires:
            return "renewed", True
        failure = results[0]
        if isinstance(failure, BridgeError):
            started = started or failure.code not in NOT_STARTED
            if failure.code in NEEDS_AUTH and await self.installed_and_enabled(extension):
                self.management.needs_sign_in(extension, "downloadByStrategy", failure.code)
            return failure.code, started
        if not isinstance(failure, BaseException) and not (
            isinstance(failure, dict) and failure.get("success") is True
        ):
            code = native_error(failure).code
            if code in NEEDS_AUTH and await self.installed_and_enabled(extension):
                self.management.needs_sign_in(extension, "downloadByStrategy", code)
            return code, True
        if stopped:
            return "stopped", started
        if timed_out:
            return "renewal_timeout", started
        # The transfer began or the download finished without a renewal.
        if reached or isinstance(failure, dict):
            return "not_renewed", True
        return "cancelled", started

    async def installed_and_enabled(self, extension: str) -> bool:
        if extension in self.disabled:
            return False
        try:
            item = await self.management.extension(extension)
        except BridgeError:
            return False
        return bool(item.get("enabled"))

    def session_view(self, extension: str) -> dict[str, str] | None:
        """What the dashboard shows about a signed session; None without one."""
        saved = self.session(extension)
        if saved is None:
            return None
        info = self.sessions.get(extension, {})
        if not self.config.keep_alive:
            keep_alive = "off"
        elif not info.get("no_keep_alive"):
            keep_alive = "on"
        elif self.config.renew_by_download and not info.get("no_download_renewal"):
            keep_alive = "download" if extension in self.delivered else "download_later"
        else:
            keep_alive = "none"
        return {
            "expires": info.get("expires") or "",
            "renewed": info.get("renewed") or "",
            "renewed_by": info.get("renewed_by") or "",
            "keep_alive": keep_alive,
        }

    async def check_updates(self) -> float:
        """Install offered updates one extension at a time, when automatic updates are on."""
        if not self.config.auto_update:
            return UPDATE_CHECK_SECONDS
        async with self.update_lock:
            offers: dict[str, tuple[str, str]] = {}
            for registry in await self.execute("registries.get", {}):
                try:
                    entries = await self.execute(
                        "registry.available", {"registry": registry, "refresh": True}
                    )
                except BridgeError:
                    continue
                for entry in entries:
                    if entry.get("is_installed") and entry.get("has_update"):
                        offers.setdefault(
                            str(entry["id"]), (registry, str(entry.get("version") or ""))
                        )
            for extension in [key for key in self.updates if key not in offers]:
                del self.updates[extension]
            busy = False
            for extension, (registry, version) in offers.items():
                known = self.updates.get(extension, {})
                if known.get("version") == version and (
                    (known.get("state") == "unsupported" and known.get("host") == HOST_ID)
                    or (known.get("state") == "waiting" and not self.config.update_all)
                ):
                    # Already downloaded and found to sign the extension out: it waits
                    # for a confirmed sign-out, not for this check.
                    continue
                self.updates[extension] = {
                    "version": version,
                    "registry": registry,
                    "state": "available",
                }
                state = await self._update(extension, sign_out=self.config.update_all)
                busy |= state == "busy"
            self.save_state(UPDATES_FILE, self.updates)
        return UPDATE_BUSY_RETRY if busy else UPDATE_CHECK_SECONDS

    async def update_waiting(self, extension: str) -> bool:
        """Install an update that waited for this extension to be signed out; callers
        have established that it is (or the operator asked for it)."""
        if not self.config.auto_update or self.updates.get(extension, {}).get("state") != "waiting":
            return False
        async with self.update_lock:
            if self.updates.get(extension, {}).get("state") != "waiting":
                return False
            installed = await self._update(extension, sign_out=True) == "installed"
            self.save_state(UPDATES_FILE, self.updates)
            return installed

    async def auto_update(
        self, extension: str, registry: str, version: str, *, sign_out: bool
    ) -> dict[str, Any]:
        """Download the offered package and install it when it keeps the sign-in, or when
        sign_out allows it, and only when the pinned host can load it and it is still the
        offered, newer version. Never while the extension prepares a file; preparations
        of other extensions continue, and the native upgrade runs without the mutation
        lock, pausing only this extension."""
        result: dict[str, Any] = {"installed": False, "keeps_sign_in": False, "version": version}
        if any(key[0] == extension for key in self.jobs):
            return result | {"busy": True}

        async def fetch() -> Path | None:
            # The registry must still offer exactly this version as an update.
            entries = await self.management.execute("available", {"refresh": True})
            current: dict[str, Any] = next((x for x in entries if x.get("id") == extension), {})
            if not current.get("has_update") or str(current.get("version") or "") != version:
                return None
            return await self.management.download(extension)

        async with self.mutation_lock:
            package = await self.on_registry(registry, fetch())
            if package is None:
                return result | {"stale": True}
            handed_over = False
            try:
                offered = package_manifest(package)
                installed = installed_manifest(self.config.state_dir / "extensions", extension)
                result["keeps_sign_in"] = keeps = keeps_sign_in(installed, offered)
                if str(offered.get("version") or "") != version or not newer(
                    version, str(installed.get("version") or "")
                ):
                    # A stale offer: never reinstall, downgrade or install another version.
                    return result | {"stale": True}
                if not host_accepts(offered):
                    return result | {"unsupported": True}
                if not keeps and not sign_out:
                    return result
                if any(key[0] == extension for key in self.jobs):
                    return result | {"busy": True}
                try:
                    await self.stop_renewal(extension)
                except BridgeError:
                    # A renewal on it did not stop in time: like a preparation, it waits.
                    return result | {"busy": True}
                self.changing.add(extension)
                self.upgrading_extension = extension
                handed_over = True
            finally:
                if not handed_over:
                    package.unlink(missing_ok=True)
            # The upgrade runs as its own task: cancelling this caller (shutdown of the
            # checks) never interrupts it, and close() waits for it.
            self.upgrading = asyncio.create_task(self._upgrade(extension, package, result))
            self.upgrading.add_done_callback(
                lambda done: None if done.cancelled() else done.exception()
            )
        return await asyncio.shield(self.upgrading)

    async def _upgrade(
        self, extension: str, package: Path, result: dict[str, Any]
    ) -> dict[str, Any]:
        try:
            answer = await self.management.call(
                "upgradeExtension", {"file_path": str(package)}, extension
            )
            version = answer.get("version") if isinstance(answer, dict) else None
            return result | {"installed": True, "version": str(version or result["version"])}
        finally:
            package.unlink(missing_ok=True)
            self.forget(extension)
            self.generations[extension] = self.generations.get(extension, 0) + 1
            self.changing.discard(extension)
            self.upgrading_extension = ""

    async def _update(self, extension: str, *, sign_out: bool) -> str:
        offer = self.updates[extension]
        previous = offer.get("state")
        try:
            result = await self.auto_update(
                extension, offer["registry"], offer["version"], sign_out=sign_out
            )
        except BridgeError as exc:
            offer["state"] = "failed"
            log.info("update extension=%s outcome=%s", extension, exc.code)
            return "failed"
        if result.get("stale"):
            # The registry no longer offers this version: looked at again next check.
            del self.updates[extension]
            log.info("update extension=%s outcome=stale", extension)
            return "stale"
        if result.get("busy"):
            # A waiting offer keeps waiting; it is tried again once preparations end.
            state = "waiting" if previous == "waiting" else "busy"
        elif result.get("unsupported"):
            state = "unsupported"
            # Looked at again when the bridge's pinned host changes.
            offer["host"] = HOST_ID
        elif not result.get("installed"):
            state = "waiting"
        else:
            state = "installed"
            offer["version"] = str(result.get("version") or offer["version"])
            self.health_after_update(extension)
        offer["state"] = state
        log.info(
            "update extension=%s keeps_sign_in=%s outcome=%s",
            extension,
            "yes" if result.get("keeps_sign_in") else "no",
            state,
        )
        return state

    def health_after_update(self, extension: str) -> None:
        async def recheck() -> None:
            params = {"extension": extension}
            try:
                async with asyncio.timeout(CHECK_TIMEOUT):
                    await self.execute("health", params)
                    if self.management.signed_session(extension):
                        await self.execute("pending-auth.show", params, private=True)
            except (BridgeError, TimeoutError):
                pass

        self.maintenance = [task for task in self.maintenance if not task.done()]
        self.maintenance.append(asyncio.create_task(recheck()))

    def prepare_wanted(
        self, extension: dict[str, Any], wanted: Mapping[str, str], quality: str
    ) -> bool:
        """Start preparing a described recording, as stream/<its recording ID> would;
        the host's planner finds it. False when it cannot start now."""
        self.check_snapshot(extension)
        try:
            recording = wanted_recording(wanted)
        except BridgeError:
            return False
        ident = self.recording_ident(recording)
        if self.ready(extension["id"], RECORDING_KEY + ident, quality, recording):
            return False
        try:
            self.begin(extension, RECORDING_KEY + ident, quality, recording)
        except BridgeError as exc:
            # Busy, backed off, or a remembered miss or failure: nothing starts now.
            if exc.status in (404, 502) or exc.code in {"preparation_busy", "download_backoff"}:
                return False
            raise
        return True
