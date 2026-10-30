"""Public-safe failures. Never include native messages in exception strings."""

import re
from typing import Any


class BridgeError(Exception):
    # retry_after: when asking again is expected to help (sent as Retry-After); None
    # for failures that need something else first, such as a login.
    def __init__(self, code: str, status: int = 503, retry_after: int | None = None) -> None:
        self.code = code
        self.status = status
        self.retry_after = retry_after
        super().__init__(code)


def native_error(value: Any) -> BridgeError:
    # Classify structured codes before inspecting dedicated error text. Never scan
    # serialized answers: they may echo titles containing words such as "cancelled".
    data = value if isinstance(value, dict) else {}
    code = str(data.get("error_type") or data.get("code") or "").casefold()
    auth = (
        "verification_required",
        "verify_required",
        "unauthorized",
        "needs login",
        "not logged",
        "authentication",
        "login_required",
        "login required",
        "verification required",
        "auth_required",
        "auth_failed",
        "not authenticated",
        "session_expired",
        "token_expired",
        "invalid_credentials",
    )
    rates = ("rate_limit", "rate limit", "too many requests")
    generic = {
        "",
        "error",
        "extension_error",
        "api_error",
        "download_error",
        "runtime_error",
        "unknown",
    }
    message = next(
        (
            data[key].casefold()
            for key in ("error", "error_message", "message")
            if isinstance(data.get(key), str) and data[key]
        ),
        "",
    )
    # Dedicated messages can still quote the requested title; mask those echoes too.
    for key in ("title", "track_name", "name"):
        if isinstance(data.get(key), str) and data[key]:
            message = message.replace(data[key].casefold(), "")
    message = re.sub(r'https?://\S+|"[^"]*"', "", message)
    fallback = message if code in generic else ""
    if any(x in code for x in auth) or any(x in fallback for x in auth):
        return BridgeError("needs_login_or_verification")
    if any(x in code for x in rates) or any(x in fallback for x in rates):
        retry = data.get("retry_after_seconds", 30)
        try:
            seconds = max(1, min(3600, int(retry)))
        except (ValueError, TypeError):
            seconds = 30
        return BridgeError("rate_limited", 429, seconds)
    if code in {"not_found", "quality_unavailable"}:
        return BridgeError("recording_unavailable", 404)
    if code in {"cancelled", "canceled"}:
        return BridgeError("cancelled")
    # Rust's stopped() preserves an extension availability reason but labels an
    # unclassified reason extension_error. Only explicit recording/audio absence is
    # a miss; "lookup failed", network errors and provider_unavailable remain failures.
    if code in (
        "error",
        "extension_error",
        "api_error",
        "download_error",
        "runtime_error",
        "unknown",
    ):
        if any(
            reason in message
            for reason in (
                "not found",
                "not playable",
                "not streamable",
                "does not offer a full playable stream",
            )
        ) or ("no supported full " in message and "stream is available" in message):
            return BridgeError("recording_unavailable", 404)
    if "cancelled" in fallback:
        return BridgeError("cancelled")
    return BridgeError("extension_failed", 502)
