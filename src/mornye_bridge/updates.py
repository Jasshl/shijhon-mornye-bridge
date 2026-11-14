"""Extension manifests read as plain files, and whether an update keeps the sign-in.

The pinned host upgrades an extension by replacing only its code directory: its
settings, storage and saved signed-session records stay in native state under the
same extension ID (manager/install.rs upgrade_package). A signed-session record is
filed by the manifest's signedSession namespace, base URL, app version and platform
(signed_session/protocol.rs filename and normalize_scope), and the rest of that object
(endpoints, header prefix, scheme label, time window) shapes every signed request, so
any change to it is treated as signing the extension out. Storage and the host's
sign-in services need the storage permission (environment.rs load_internal), and the
host loads an extension only when its required runtime features and minimum app
version are met (environment.rs validate_gates).
"""

from __future__ import annotations

import json
import posixpath
import re
import zipfile
from pathlib import Path
from typing import Any

from .config import HOST_VERSION
from .web_forms import secret

MANIFEST_LIMIT = 1024 * 1024
EXTENSION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
SCOPE_KEYS = ("namespace", "baseUrl", "appVersion", "platform")
# The file names the pinned host's registry download writes (repository/package.rs
# destination_path).
PACKAGE_SUFFIXES = {".mornye-ext", ".sflx"}
NUMBER = re.compile(r"[0-9]+")
# The pinned host's runtime features and their versions (environment.rs).
HOST_FEATURES = {
    "signedSession": 3,
    "sessionRefresh": 1,
    "sessionGrant": 1,
    "globalAction": 1,
    "webviewAuth": 1,
    "downloadSegments": 1,
    "patternedFileTransform": 1,
    "preparedContext": 1,
    "accountForms": 1,
    "accountDownloadMode": 1,
    "directMedia": 1,
}


def parse_manifest(raw: bytes) -> dict[str, Any]:
    if len(raw) > MANIFEST_LIMIT:
        return {}
    try:
        value = json.loads(raw.decode("utf-8", errors="replace"))
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def installed_manifest(extensions_dir: Path, extension: str) -> dict[str, Any]:
    """The installed manifest; the host keeps each extension in a directory named by
    its ID. Empty when it cannot be read."""
    if not EXTENSION_ID.fullmatch(extension):
        return {}
    path = extensions_dir / extension / "manifest.json"
    try:
        if path.is_symlink() or path.stat().st_size > MANIFEST_LIMIT:
            return {}
        return parse_manifest(path.read_bytes())
    except OSError:
        return {}


def package_manifest(package: Path) -> dict[str, Any]:
    """The root manifest.json of a downloaded package (a zip archive). Empty when it
    cannot be read; the native host still validates the package on installation."""
    try:
        with zipfile.ZipFile(package) as archive:
            # The host reads manifest.json at the archive root after cleaning the path
            # ("./manifest.json" too); a nested manifest is not a package it installs.
            entries = [
                entry
                for entry in archive.infolist()
                if not entry.is_dir() and posixpath.normpath(entry.filename) == "manifest.json"
            ]
            if len(entries) != 1 or entries[0].file_size > MANIFEST_LIMIT:
                return {}
            with archive.open(entries[0]) as stream:
                return parse_manifest(stream.read(MANIFEST_LIMIT + 1))
    except (OSError, zipfile.BadZipFile, RuntimeError, ValueError):
        return {}


def session_scope(manifest: dict[str, Any]) -> tuple[str, ...] | None:
    session = manifest.get("signedSession")
    if not isinstance(session, dict):
        return None
    return tuple(str(session.get(key) or "").strip() for key in SCOPE_KEYS)


def settings_keys(manifest: dict[str, Any], *, secret_only: bool = False) -> set[str]:
    fields = [field for field in manifest.get("settings") or [] if isinstance(field, dict)]
    for option in manifest.get("qualityOptions") or []:
        if isinstance(option, dict):
            fields += [field for field in option.get("settings") or [] if isinstance(field, dict)]
    return {
        str(field.get("key"))
        for field in fields
        if isinstance(field.get("key"), str) and (not secret_only or secret(field))
    }


def storage(manifest: dict[str, Any]) -> bool:
    permissions = manifest.get("permissions")
    return isinstance(permissions, dict) and permissions.get("storage") is True


def version_parts(version: str) -> list[int] | None:
    """Numeric version parts as the host compares them; None for anything else."""
    text = version.strip()
    text = text[1:] if text.startswith("v") else text
    parts = text.split(".")
    if not text or any(not NUMBER.fullmatch(part) for part in parts):
        return None
    return [int(part) for part in parts]


def host_accepts(manifest: dict[str, Any]) -> bool:
    """True only when every load gate is known to be met by the pinned host: each
    required runtime feature at its version, and the minimum app version."""
    features = manifest.get("requiredRuntimeFeatures", [])
    if not isinstance(features, list):
        return False
    for raw in features:
        if not isinstance(raw, str):
            return False
        name, version = raw.strip(), "1"
        if not name:
            continue
        if "@" in name:
            name, version = name.rsplit("@", 1)
        if not NUMBER.fullmatch(version) or int(version) < 1:
            return False
        if HOST_FEATURES.get(name, 0) < int(version):
            return False
    minimum = manifest.get("minAppVersion", "")
    if not isinstance(minimum, str):
        return False
    if minimum.strip():
        wanted, provided = version_parts(minimum), version_parts(HOST_VERSION)
        if wanted is None or provided is None:
            return False
        width = max(len(wanted), len(provided))
        if provided + [0] * (width - len(provided)) < wanted + [0] * (width - len(wanted)):
            return False
    return True


def newer(candidate: str, current: str) -> bool:
    """Whether a version is later than another, compared as the host compares them
    (environment.rs compare_versions: dot-separated numbers, other parts as 0)."""

    def parts(version: str) -> list[int]:
        text = version.strip()
        text = text[1:] if text.startswith("v") else text
        return [int(part) if NUMBER.fullmatch(part) else 0 for part in text.split(".")]

    left, right = parts(candidate), parts(current)
    width = max(len(left), len(right))
    return left + [0] * (width - len(left)) > right + [0] * (width - len(right))


def keeps_sign_in(old: dict[str, Any], new: dict[str, Any]) -> bool:
    """True when the host keeps everything the extension's sign-in depends on: the same
    signedSession object (or none in both), storage access kept and every saved
    credential-like setting still declared. Anything unreadable or unknown counts as
    signing out. Whether the host can load the new version at all is host_accepts()."""
    if not old or not new or old.get("name") != new.get("name"):
        return False
    sessions = old.get("signedSession"), new.get("signedSession")
    if sessions != (None, None):
        # The whole object, exactly: any difference or a malformed value signs out.
        if not all(isinstance(session, dict) for session in sessions):
            return False
        if json.dumps(sessions[0], sort_keys=True) != json.dumps(sessions[1], sort_keys=True):
            return False
    if storage(old) and not storage(new):
        return False
    return settings_keys(old, secret_only=True) <= settings_keys(new)
