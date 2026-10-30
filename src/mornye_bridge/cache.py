from __future__ import annotations

import json
import os
import secrets
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import Config
from .errors import BridgeError

# Descriptions behind digest recording IDs (too long to carry inline): kept across
# restarts while in use, pruned after 30 days unused and beyond 20,000 rows.
RECORDING_DAYS = 30
RECORDING_ROWS = 20_000


@dataclass
class Entry:
    selector: str
    extension: str
    native_id: str
    quality: str
    path: Path
    metadata: dict[str, Any]
    audio: dict[str, Any]


class Cache:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.root = config.state_dir / "audio"
        self.db = sqlite3.connect(config.state_dir / "cache.sqlite")
        self.db.execute("""CREATE TABLE IF NOT EXISTS files (
            selector TEXT PRIMARY KEY, extension TEXT, native_id TEXT, quality TEXT,
            suffix TEXT, size INTEGER, created REAL, accessed REAL, metadata TEXT, audio TEXT,
            UNIQUE(extension, native_id, quality))""")
        self.db.execute("""CREATE TABLE IF NOT EXISTS recordings (
            digest BLOB PRIMARY KEY, description TEXT, used REAL)""")
        # Older M4A cache entries predate faststart finalization. Reprepare only
        # those disposable files, once; preserve other formats and native state.
        if self.db.execute("PRAGMA user_version").fetchone()[0] < 1:
            self.db.execute("DELETE FROM files WHERE suffix = '.m4a'")
            self.db.execute("PRAGMA user_version = 1")
        self.db.commit()
        self.leases: dict[str, int] = {}
        # Atomic publication can leave an orphan across a crash; no orphan is advertised.
        known = {row[0] + row[1] for row in self.db.execute("SELECT selector,suffix FROM files")}
        for path in self.root.iterdir():
            if path.is_file() and path.name not in known:
                path.unlink()
        self.evict()

    def _entry(self, row: Any) -> Entry | None:
        if not row:
            return None
        selector, ext, native, quality, suffix, _, _, _, metadata, audio = row
        path = self.root / (selector + suffix)
        if not path.is_file():
            self.db.execute("DELETE FROM files WHERE selector=?", (selector,))
            self.db.commit()
            return None
        return Entry(selector, ext, native, quality, path, json.loads(metadata), json.loads(audio))

    def get(self, extension: str, native_id: str, quality: str) -> Entry | None:
        self.evict()
        entry = self._entry(
            self.db.execute(
                "SELECT * FROM files WHERE extension=? AND native_id=? AND quality=?",
                (extension, native_id, quality),
            ).fetchone()
        )
        return self.touch(entry)

    def selector(self, selector: str) -> Entry | None:
        self.evict()
        return self.touch(
            self._entry(
                self.db.execute("SELECT * FROM files WHERE selector=?", (selector,)).fetchone()
            )
        )

    def touch(self, entry: Entry | None) -> Entry | None:
        if entry:
            self.db.execute(
                "UPDATE files SET accessed=? WHERE selector=?", (time.time(), entry.selector)
            )
            self.db.commit()
        return entry

    def entries(self, extension: str) -> list[Entry]:
        self.evict()
        return [
            entry
            for row in self.db.execute(
                "SELECT * FROM files WHERE extension=?", (extension,)
            ).fetchall()
            if (entry := self._entry(row))
        ]

    def evict(self, required: int = 0) -> None:
        rows = self.db.execute(
            "SELECT selector,suffix,size,created FROM files ORDER BY accessed"
        ).fetchall()
        total = sum(row[2] for row in rows)
        now = time.time()
        for selector, suffix, size, created in rows:
            if self.leases.get(selector):
                continue
            if now - created > self.config.cache_age or total + required > self.config.cache_bytes:
                (self.root / (selector + suffix)).unlink(missing_ok=True)
                self.db.execute("DELETE FROM files WHERE selector=?", (selector,))
                total -= size
        self.db.commit()
        if required and total + required > self.config.cache_bytes:
            raise BridgeError("cache_full")

    def publish(
        self,
        extension: str,
        native_id: str,
        quality: str,
        path: Path,
        metadata: dict[str, Any],
        audio: dict[str, Any],
    ) -> Entry:
        existing = self.get(extension, native_id, quality)
        if existing:
            if path != existing.path:
                path.unlink(missing_ok=True)
            return existing
        size = path.stat().st_size
        self.evict(size)
        selector = secrets.token_urlsafe(32)
        suffix = path.suffix
        target = self.root / (selector + suffix)
        with path.open("rb") as stream:
            os.fsync(stream.fileno())
        os.replace(path, target)
        try:
            self.db.execute(
                "INSERT INTO files VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    selector,
                    extension,
                    native_id,
                    quality,
                    suffix,
                    size,
                    time.time(),
                    time.time(),
                    json.dumps(metadata),
                    json.dumps(audio),
                ),
            )
            self.db.commit()
        except BaseException:
            self.db.rollback()
            target.unlink(missing_ok=True)
            raise
        return Entry(selector, extension, native_id, quality, target, metadata, audio)

    def remember_recording(self, digest: bytes, description: list[Any]) -> None:
        now = time.time()
        self.db.execute(
            "INSERT INTO recordings VALUES(?,?,?) ON CONFLICT(digest) DO UPDATE SET used=?",
            (digest, json.dumps(description), now, now),
        )
        self.db.execute(
            "DELETE FROM recordings WHERE used < ? OR digest NOT IN "
            "(SELECT digest FROM recordings ORDER BY used DESC LIMIT ?)",
            (now - RECORDING_DAYS * 86400, RECORDING_ROWS),
        )
        self.db.commit()

    def recording(self, digest: bytes) -> list[Any] | None:
        row = self.db.execute(
            "SELECT description FROM recordings WHERE digest=? AND used >= ?",
            (digest, time.time() - RECORDING_DAYS * 86400),
        ).fetchone()
        if not row:
            return None
        self.db.execute("UPDATE recordings SET used=? WHERE digest=?", (time.time(), digest))
        self.db.commit()
        value = json.loads(row[0])
        return value if isinstance(value, list) else None

    def acquire(self, selector: str) -> Entry | None:
        entry = self.selector(selector)
        if entry:
            self.leases[selector] = self.leases.get(selector, 0) + 1
        return entry

    def release(self, selector: str) -> None:
        self.leases[selector] = max(0, self.leases.get(selector, 0) - 1)

    def close(self) -> None:
        self.db.close()
