"""The host's saved signed-session records, read for whether they hold a session and
when it expires. Nothing else from a record is kept, shown or logged.

The pinned host saves one record per signed-session scope in
data/signed_sessions/<namespace>-<16 hex>.json, named by a SHA-256 of the manifest's
namespace, base URL, app version and platform after its defaults
(signed_session/protocol.rs defaults, namespace and filename).
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

RECORD_LIMIT = 1024 * 1024


def namespace(value: str) -> str:
    cleaned = "".join(
        character
        for character in value.strip().lower()
        if character.isascii() and (character.isalnum() or character in ".-_")
    )
    return cleaned.strip(".-_")


def record_name(session: dict[str, Any]) -> str:
    """The host's record file name for a manifest's signedSession; empty if it has none."""
    name = namespace(str(session.get("namespace") or ""))
    if not name:
        return ""
    scope = "\n".join(
        [
            name,
            str(session.get("baseUrl") or "").strip().lower(),
            (str(session.get("appVersion") or "").strip() or "ext-1.0").strip().lower(),
            (str(session.get("platform") or "").strip() or "extension").strip().lower(),
        ]
    )
    return f"{name}-{hashlib.sha256(scope.encode()).hexdigest()[:16]}.json"


def parse_time(value: Any) -> datetime | None:
    """An RFC 3339 time with its offset, as the host writes expires_at."""
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip().upper().replace(",", ".")
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    # Python reads at most microseconds; the host keeps nanoseconds.
    text = re.sub(r"(\.\d{6})\d+", r"\1", text)
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed.astimezone(UTC) if parsed.tzinfo else None


def saved_session(data_dir: Path, manifest: dict[str, Any]) -> tuple[bool, datetime | None] | None:
    """Whether the extension's saved signed session holds a session, and its expiry.
    None when the extension has no signed session or its record cannot be read."""
    session = manifest.get("signedSession")
    name = record_name(session) if isinstance(session, dict) else ""
    if not name:
        return None
    path = data_dir / "signed_sessions" / name
    try:
        if path.is_symlink() or path.stat().st_size > RECORD_LIMIT:
            return None
        record = json.loads(path.read_bytes())
    except (OSError, ValueError):
        return None
    if not isinstance(record, dict):
        return None
    held = all(str(record.get(key) or "").strip() for key in ("session_id", "session_secret"))
    expires = parse_time(record.get("expires_at"))
    record.clear()
    return held, expires
