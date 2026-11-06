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
from pathlib import Path
from typing import Any, BinaryIO

from . import config as host_config
from .cache import Cache, Entry
from .catalog import Catalog, map_item, text
from .config import CatalogConfig, Config, secret_file
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
from .management import Management
from .supervisor import Supervisor, Worker

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
        self.changing: set[str] = set()
        self.mutation_lock = asyncio.Lock()
        self.access_token = ""
        self.admin_token = ""

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
        self.cache = Cache(self.config)

    async def close(self) -> None:
        tasks = list(self.jobs.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.wait(tasks, timeout=self.config.shutdown_timeout)
        # Kill ownership before a second cancellation can remove staging.
        await self.worker.close()
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
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
                        self.management.states[extension["id"]] = refused.code
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
            payload: dict[str, Any] = {
                "contract_version": 1,
                "service": extension["id"],
                "download_provider": extension["id"],
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
            }
            catalog_track = None
            if recording is None:
                # An extension's own track: read its details, then find and download it
                # by those details like any described recording.
                raw = await self.catalog.native_item(extension["id"], "track", native_id)
                self.check_snapshot(extension)
                catalog_track = map_item(raw, "track")
                self.job_tracks[key] = catalog_track
                recording = catalog_recording(catalog_track, raw)
            isrc, title, artist, duration, album = recording
            payload |= {
                # No source and no track ID: the planner asks the extension's
                # checkAvailability and downloads what it found, with its context.
                "source": "",
                "track_name": title,
                "artist_name": artist,
                "album_name": album,
                "duration_ms": duration,
                "isrc": isrc,
            }
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
            outcome = "ready"
            return entry
        except BridgeError as exc:
            outcome = exc.code
            now = time.monotonic()
            if exc.code in {"download_timeout", "worker_timeout", "worker_restarted"}:
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
        async with self.mutation_lock:
            ext = str(params.get("extension", ""))
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
            self.management.invalidate()
            self.catalog.isrc_failures.pop(str(params.get("extension", "")), None)
            ext = params.get("extension")
            for memory in (self.misses, self.backoffs, self.failures):
                for key in [key for key in memory if key[0] == ext]:
                    del memory[key]
                    self.failure_codes.pop(key, None)
            for found in [found for found in self.resolved if found[0] == ext]:
                del self.resolved[found]
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
            registry = params["registry"]
            if registry not in await self._execute("registries.get", {}):
                raise BridgeError("registry_not_saved", 400)
            previous = (await self.management.execute("registry.get", {}))["registry"]
            try:
                await self.management.execute("registry.set", {"url": registry})
                return await self.management.execute(
                    "available" if operation == "registry.available" else operation,
                    params,
                    private=private,
                )
            finally:
                if previous:
                    await self.management.execute("registry.set", {"url": previous})
                else:
                    await self.management.call("clearRepoRegistryUrl")
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
