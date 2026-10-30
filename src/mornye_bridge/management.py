from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from .config import Config
from .errors import BridgeError, native_error
from .privacy import redact, safe_settings
from .supervisor import Worker

# Native health statuses that count as a passing check.
PASSING = {"loaded", "online", "healthy", "ok", "ready", "success", "passed"}


class Management:
    """Service layer shared by CLI and the authenticated management endpoint."""

    def __init__(self, config: Config, worker: Worker) -> None:
        self.config = config
        self.worker = worker
        self.states: dict[str, str] = {}
        # Extensions whose native login challenge was seen and has not yet gone away.
        self.awaiting: set[str] = set()
        self._generation = 0
        self._installed: tuple[float, list[dict[str, Any]]] = (0, [])

    def invalidate(self) -> None:
        self._generation += 1
        self._installed = (0, [])

    async def call(self, method: str, args: Any = None, extension: str = "") -> Any:
        try:
            value = await self.worker.call(method, args)
            if isinstance(value, dict) and value.get("success") is False:
                raise native_error(value)
            if extension:
                self.states.pop(extension, None)
            return value
        except BridgeError as exc:
            if extension:
                self.states[extension] = exc.code
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
                | {"state": self.states.get(x["id"], "unknown")}
                for x in await self.installed()
            ]
        if operation in ("install", "update"):
            path = await self.call(
                "downloadRepoExtension",
                {"extension_id": ext, "dest_dir": str(self.config.state_dir / "repo")},
            )
            method = "upgradeExtension" if operation == "update" else "loadExtensionFromPath"
            package = Path(path).resolve()
            root = (self.config.state_dir / "repo").resolve()
            if not package.is_relative_to(root):
                raise BridgeError("invalid_package_path", 502)
            try:
                result = await self.call(method, {"file_path": path}, ext)
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
            return result if private else redact(result)
        if operation == "pending-auth.show":
            result = await self.call("getExtensionPendingAuth", {"extension_id": ext})
            if result:
                self.states[ext] = "needs_login_or_verification"
                self.awaiting.add(ext)
            elif ext in self.awaiting:
                # The challenge was answered (callback or browser): drop the remembered
                # failure, so the status comes from a new health check or call again.
                self.awaiting.discard(ext)
                self.states.pop(ext, None)
            return (
                result
                if private
                else {
                    "pending": bool(result),
                    "state": self.states.get(ext, "unknown"),
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
                self.states.pop(ext, None)
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
