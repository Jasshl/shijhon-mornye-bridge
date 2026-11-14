from __future__ import annotations

import json
import re
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .config import Config, write_private_json
from .errors import BridgeError, native_error
from .privacy import redact, safe_settings
from .sessions import parse_time, saved_session
from .supervisor import Worker
from .updates import installed_manifest, session_scope
from .web_forms import IDENTIFIER

# Native health statuses that count as a passing check.
PASSING = {"loaded", "online", "healthy", "ok", "ready", "success", "passed"}
NEEDS_AUTH = {
    "needs_login_or_verification",
    "needs_login",
    "unauthorized",
    "auth_required",
    "verification_required",
    "needs_verification",
}
# Successes that show an extension's sign-in works, whatever operation reported it
# missing: a finished download and a completed native session grant.
PROVES_SIGN_IN = {"downloadByStrategy", "completeExtensionSessionGrant"}
SIGN_IN_FILE = "sign-in.json"
PENDING_AUTH = "getExtensionPendingAuth"
LOGIN_ACTION = re.compile(
    r"log.?in|sign.?in|auth|connect|renew|refresh.?(?:token|session|login)", re.I
)
NOT_LOGIN = re.compile(
    r"log.?out|sign.?out|disconnect|clear|reset|remove|delete|forget|cancel|revoke", re.I
)


def login_action(extension: dict[str, Any]) -> str:
    """A declared action that signs in again by its key, action or label (never one
    that signs out or clears anything); empty when there is none."""
    for field in extension.get("settings") or []:
        action = field.get("action")
        if not isinstance(action, str) or not IDENTIFIER.fullmatch(action):
            continue
        words = " ".join(str(field.get(key) or "") for key in ("key", "action", "label"))
        if LOGIN_ACTION.search(words) and not NOT_LOGIN.search(words):
            return action
    return ""


def browser_url(value: Any) -> str:
    if isinstance(value, str):
        if len(value) > 16384 or any(ord(character) < 32 for character in value):
            return ""
        parsed = urlsplit(value)
        if parsed.scheme in {"https", "http"} and parsed.hostname and not parsed.username:
            return value
    return ""


def result_browser_url(result: Any) -> str:
    """A browser link an action result asks the operator to open, if any."""
    payload = result.get("result", result) if isinstance(result, dict) else {}
    if not isinstance(payload, dict):
        return ""
    urls = [payload.get(key) for key in ("auth_url", "login_url", "url")]
    updates = payload.get("setting_updates") or {}
    if isinstance(updates, dict):
        urls += [
            value
            for key, value in updates.items()
            if "url" in key.lower() and any(word in key.lower() for word in ("auth", "login"))
        ]
    return next((url for value in urls if (url := browser_url(value))), "")


def has_action_form(result: Any) -> bool:
    payload = result.get("result", result) if isinstance(result, dict) else {}
    return isinstance(payload, dict) and (
        payload.get("action_form") is not None or payload.get("byoa_form") is not None
    )


def report_strength(source: str) -> int:
    """How strongly an operation's report shows a missing sign-in: a download needs the
    account, an availability check usually, a search or an action perhaps, and a health
    probe or pending-auth check only says what the host sees right now."""
    if source == "downloadByStrategy":
        return 3
    if source == "invokeExtensionAction:checkAvailability":
        return 2
    if source in {PENDING_AUTH, "checkExtensionHealth"}:
        return 0
    return 1


def method_key(method: str, args: Any) -> str:
    if isinstance(args, dict) and isinstance(args.get("action"), str):
        return method + ":" + str(args["action"])
    return method


class Management:
    """Service layer shared by CLI and the authenticated management endpoint."""

    def __init__(self, config: Config, worker: Worker) -> None:
        self.config = config
        self.worker = worker
        # Missing sign-ins per extension (a NEEDS_AUTH code), the operation that reported
        # each and the signed session's expiry at that report. Saved together in
        # SIGN_IN_FILE, so the notice survives a restart until the sign-in works again.
        self.states: dict[str, str] = {}
        self.sources: dict[str, str] = {}
        self.expiries: dict[str, str] = {}
        # Other remembered failures per extension; they never replace a missing sign-in.
        self.errors: dict[str, str] = {}
        # Submit actions of forms that a sign-in action returned, per extension.
        self.login_steps: dict[str, set[str]] = {}
        # Extensions whose native login challenge was seen and has not yet gone away.
        self.awaiting: set[str] = set()
        # Extensions whose sign-in cannot finish automatically: left for the sign-in page.
        self.manual: set[str] = set()
        # Called with an extension ID whenever an operation reports a missing sign-in.
        self.on_needs_sign_in: Callable[[str], None] | None = None
        self._generation = 0
        self._installed: tuple[float, list[dict[str, Any]]] = (0, [])

    def invalidate(self) -> None:
        self._generation += 1
        self._installed = (0, [])

    def state(self, extension: str, default: str = "") -> str:
        """A missing sign-in first, else another remembered failure."""
        return self.states.get(extension) or self.errors.get(extension) or default

    def load_sign_in(self) -> None:
        try:
            saved = json.loads((self.config.state_dir / SIGN_IN_FILE).read_text())
        except (OSError, ValueError):
            return
        if not isinstance(saved, dict):
            return
        for extension, entry in saved.items():
            if not isinstance(extension, str) or not isinstance(entry, dict):
                continue
            code = entry.get("code")
            self.states[extension] = code if code in NEEDS_AUTH else "needs_login_or_verification"
            self.sources[extension] = str(entry.get("source") or "")
            if isinstance(entry.get("expires"), str):
                self.expiries[extension] = entry["expires"]

    def _save_sign_in(self) -> None:
        saved = {
            extension: {
                "code": code,
                "source": self.sources.get(extension, ""),
                "expires": self.expiries.get(extension),
            }
            for extension, code in self.states.items()
        }
        path = self.config.state_dir / SIGN_IN_FILE
        try:
            if saved:
                write_private_json(path, saved)
            else:
                path.unlink(missing_ok=True)
        except OSError:
            # Only the notice after a restart depends on this file.
            pass

    def session_expiry(self, extension: str) -> datetime | None:
        """The expiry of the extension's saved signed session, if it holds one."""
        manifest = installed_manifest(self.config.state_dir / "extensions", extension)
        saved = saved_session(self.config.state_dir / "data", manifest)
        return saved[1] if saved and saved[0] else None

    def session_renewed(self, extension: str) -> bool:
        """Whether the signed session expires later than when the sign-in was reported
        missing: a new or renewed session."""
        now = self.session_expiry(extension)
        before = parse_time(self.expiries.get(extension))
        return now is not None and (before is None or now > before)

    def needs_sign_in(self, extension: str, source: str, code: str = "") -> None:
        code = code if code in NEEDS_AUTH else "needs_login_or_verification"
        expiry = self.session_expiry(extension)
        expires = expiry.isoformat() if expiry else None
        self.states[extension] = code
        # The strongest reporter is kept while the notice stands: a weaker later one (a
        # search, a pending-auth check) must not take over and clear it by succeeding,
        # and a stronger later one (a download) takes over from a weaker one.
        current = self.sources.get(extension)
        if current is None or report_strength(source) > report_strength(current):
            self.sources[extension] = source
        if expires:
            self.expiries[extension] = expires
        else:
            self.expiries.pop(extension, None)
        self.errors.pop(extension, None)
        self._save_sign_in()
        if self.on_needs_sign_in:
            self.on_needs_sign_in(extension)

    def signed_in(self, extension: str) -> None:
        """The extension's sign-in works again: forget the remembered failure."""
        self.awaiting.discard(extension)
        self.manual.discard(extension)
        self.login_steps.pop(extension, None)
        self.sources.pop(extension, None)
        self.expiries.pop(extension, None)
        if self.states.pop(extension, None) is not None:
            self._save_sign_in()

    def succeeded(self, extension: str, source: str) -> None:
        """Forget remembered failures after a success. A missing sign-in is forgotten
        only when the operation that reported it succeeds, or one that proves sign-in:
        a catalog search can work without the account a download needs."""
        self.errors.pop(extension, None)
        if extension not in self.states or source == PENDING_AUTH:
            # A pending-auth answer is judged by the pending-auth operation itself.
            return
        if source in PROVES_SIGN_IN or self.sources.get(extension, source) == source:
            self.signed_in(extension)

    def signed_session(self, extension: str) -> bool:
        """Whether the extension signs in through the host's signed session, whose
        native pending-auth check also confirms a working session."""
        return (
            session_scope(installed_manifest(self.config.state_dir / "extensions", extension))
            is not None
        )

    async def call(self, method: str, args: Any = None, extension: str = "") -> Any:
        try:
            value = await self.worker.call(method, args)
            if isinstance(value, dict) and value.get("success") is False:
                raise native_error(value)
            if method == "downloadByStrategy" and not (
                isinstance(value, dict) and value.get("success") is True
            ):
                # Only an explicit success is a finished download.
                raise native_error(value)
            if extension:
                self.succeeded(extension, method_key(method, args))
            return value
        except BridgeError as exc:
            if extension:
                if exc.code in NEEDS_AUTH:
                    self.needs_sign_in(extension, method_key(method, args), exc.code)
                elif extension not in self.states:
                    # A missing sign-in stays; another failure is kept beside it.
                    self.errors[extension] = exc.code
            raise

    async def installed(self) -> list[dict[str, Any]]:
        if self._installed[0] > time.monotonic():
            return self._installed[1]
        generation = self._generation
        result = await self.call("getInstalledExtensions")
        if not isinstance(result, list):
            raise BridgeError("invalid_native_result", 502)
        if generation == self._generation:
            self._installed = (time.monotonic() + 3, result)
        return result

    async def extension(self, extension: str) -> dict[str, Any]:
        for item in await self.installed():
            if item["id"] == extension:
                return item
        raise BridgeError("extension_not_found", 404)

    async def download(self, extension: str) -> Path:
        path = await self.call(
            "downloadRepoExtension",
            {"extension_id": extension, "dest_dir": str(self.config.state_dir / "repo")},
        )
        package = Path(path).resolve()
        root = (self.config.state_dir / "repo").resolve()
        if not package.is_relative_to(root):
            raise BridgeError("invalid_package_path", 502)
        return package

    async def raw(self, extension: str, action: str, arguments: list[Any]) -> Any:
        return await self.call(
            "invokeExtensionAction",
            {"extension_id": extension, "action": action, "arguments_json": json.dumps(arguments)},
            extension,
        )

    async def execute(
        self, operation: str, params: dict[str, Any], *, private: bool = False
    ) -> Any:
        ext = str(params.get("extension", ""))
        if operation in {
            "install",
            "update",
            "enable",
            "disable",
            "remove",
            "settings.set",
            "action",
        }:
            self.invalidate()
        if operation == "registry.get":
            return {"registry": await self.call("getRepoRegistryUrl")}
        if operation == "registry.set":
            url = params.get("url")
            if not isinstance(url, str) or not url.startswith("https://"):
                raise BridgeError("registry_must_use_https", 400)
            await self.call("setRepoRegistryUrl", {"registry_url": url})
            return {"saved": True}
        if operation == "available":
            entries = await self.call(
                "getRepoExtensions", {"force_refresh": params.get("refresh", False)}
            )
            return [
                {
                    k: item.get(k)
                    for k in (
                        "id",
                        "display_name",
                        "version",
                        "category",
                        "is_installed",
                        "has_update",
                        "min_app_version",
                    )
                }
                for item in entries
            ]
        if operation == "list":
            # Do not expose raw installed settings or defaults to an operator listing.
            return [
                {
                    k: x.get(k)
                    for k in (
                        "id",
                        "display_name",
                        "version",
                        "enabled",
                        "has_metadata_provider",
                        "has_download_provider",
                    )
                }
                | {"state": self.state(x["id"], "unknown")}
                for x in await self.installed()
            ]
        if operation in ("install", "update"):
            package = await self.download(ext)
            method = "upgradeExtension" if operation == "update" else "loadExtensionFromPath"
            try:
                result = await self.call(method, {"file_path": str(package)}, ext)
            finally:
                package.unlink(missing_ok=True)
                self.invalidate()
            return {k: result.get(k) for k in ("id", "display_name", "version", "enabled")}
        if operation in ("enable", "disable", "remove"):
            method = "removeExtension" if operation == "remove" else "setExtensionEnabled"
            args: dict[str, Any] = {"extension_id": ext}
            if operation != "remove":
                args["enabled"] = operation == "enable"
            await self.call(method, args, ext)
            if operation == "remove":
                # Its settings and sign-in are gone with it.
                self.signed_in(ext)
            return {"saved": True}
        if operation == "describe":
            item = await self.extension(ext)
            schema = []
            for field in item.get("settings") or []:
                view = {
                    k: field[k]
                    for k in (
                        "key",
                        "type",
                        "label",
                        "description",
                        "required",
                        "options",
                        "action",
                    )
                    if k in field
                }
                view["secret"] = (
                    safe_settings({field["key"]: "probe"}, [field])[field["key"]] == "[redacted]"
                )
                schema.append(view)
            return {
                "id": item["id"],
                "settings": schema,
                "quality_options": [
                    {k: option[k] for k in ("id", "kind", "label", "description") if k in option}
                    for option in item.get("quality_options") or []
                ],
            }
        if operation == "settings.show":
            item = await self.extension(ext)
            values = await self.call("getExtensionSettings", {"extension_id": ext})
            return safe_settings(values, item.get("settings") or [])
        if operation == "settings.set":
            values = params.get("values")
            if not isinstance(values, dict):
                raise BridgeError("settings_must_be_object", 400)
            # Native set replaces the object: merge for CLI's per-key updates.
            current = await self.call("getExtensionSettings", {"extension_id": ext})
            await self.call(
                "setExtensionSettings",
                {"extension_id": ext, "settings": json.dumps(current | values)},
                ext,
            )
            return {"saved": True}
        if operation == "actions":
            item = await self.extension(ext)
            return [
                {k: s[k] for k in ("key", "type", "label", "action") if k in s}
                for s in item.get("settings") or []
                if s.get("action")
            ]
        if operation == "action":
            action = params.get("action")
            if not isinstance(action, str) or not action:
                raise BridgeError("action_required", 400)
            if "arguments" in params:
                result = await self.raw(ext, action, params["arguments"])
            else:
                result = await self.call(
                    "invokeExtensionAction", {"extension_id": ext, "action": action}, ext
                )
            payload = result.get("result", result) if isinstance(result, dict) else result
            if isinstance(payload, dict) and payload.get("success") is False:
                raise native_error(payload)
            if isinstance(payload, dict) and isinstance(payload.get("setting_updates"), dict):
                await self.execute(
                    "settings.set", {"extension": ext, "values": payload["setting_updates"]}
                )
            # Only the sign-in action and the form steps it started count as a sign-in;
            # another declared action (an index rebuild, say) proves nothing.
            steps = self.login_steps.setdefault(ext, set())
            if action == login_action(await self.extension(ext)) or action in steps:
                form = (
                    payload.get("action_form", payload.get("byoa_form"))
                    if isinstance(payload, dict)
                    else None
                )
                if has_action_form(result):
                    # Another step follows; a malformed form is never a finished sign-in.
                    if isinstance(form, dict) and isinstance(form.get("submit_action"), str):
                        steps.add(form["submit_action"])
                elif not result_browser_url(result):
                    self.signed_in(ext)
            return result if private else redact(result)
        if operation == "pending-auth.show":
            # For a signed-session extension the host checks the saved session first and,
            # when it is not usable, asks its gateway again: a new session (signed in
            # without a browser) or a new challenge.
            result = await self.call(PENDING_AUTH, {"extension_id": ext}, ext)
            if result:
                self.awaiting.add(ext)
                # A weaker reporter than the one kept: the notice keeps its source, and
                # only that operation or a renewed session clears it.
                self.needs_sign_in(ext, PENDING_AUTH)
            elif ext in self.states:
                if self.signed_session(ext):
                    # No challenge only means the saved session has not expired locally.
                    # It counts when the notice came from this check, or the session
                    # expires later than when the sign-in was reported missing.
                    if self.sources.get(ext) == PENDING_AUTH or self.session_renewed(ext):
                        self.signed_in(ext)
                elif ext in self.awaiting:
                    # The challenge seen earlier was answered (callback or browser).
                    self.signed_in(ext)
            return (
                result
                if private
                else {
                    "pending": bool(result),
                    "state": self.state(ext, "unknown"),
                    "details": "Use --private-output to receive the private browser/callback URLs",
                }
            )
        if operation == "pending-auth.grant":
            completed = await self.call(
                "completeExtensionSessionGrant",
                {"extension_id": ext, "grant": params["grant"]},
                ext,
            )
            if completed is not True:
                raise BridgeError("grant_not_completed")
            self.signed_in(ext)
            return {"completed": True}
        if operation == "pending-auth.callback":
            accepted = await self.call("queueExtensionCallbackUrl", {"url": params["url"]})
            if accepted is not True:
                raise BridgeError("callback_not_accepted", 400)
            await self.call("processPendingExtensionCallbacks")
            return {
                "submitted": True,
                "note": "Check pending auth and retry; callback submission is not proof of login",
            }
        if operation == "health":
            result = await self.call("checkExtensionHealth", {"extension_id": ext})
            if str(result.get("status", "")).lower() in PASSING:
                # Probes do not show a working account: a missing sign-in reported by a
                # download stays until that works again or the sign-in is completed.
                self.succeeded(ext, "checkExtensionHealth")
            return {
                "extension_id": ext,
                "status": result.get("status", "unknown"),
                "checks": [
                    {
                        k: c.get(k)
                        for k in (
                            "id",
                            "label",
                            "status",
                            "http_status",
                            "latency_ms",
                            "checked_at",
                        )
                    }
                    for c in result.get("checks", [])
                ],
            }
        raise BridgeError("unknown_management_operation", 400)
