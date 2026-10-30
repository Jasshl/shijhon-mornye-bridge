"""Deny-by-default views of untrusted native control-plane data."""

import re
from typing import Any

SENSITIVE = re.compile(r"token|secret|password|cookie|authorization|grant|key|code|session", re.I)


def safe_settings(values: dict[str, Any], schema: list[dict[str, Any]]) -> dict[str, Any]:
    public = {
        s["key"]
        for s in schema
        if s.get("secret") is False and not SENSITIVE.search(s.get("key", ""))
    }
    # Strings are private unless explicitly declared public. Even an unmarked media token is hidden.
    public.update(
        s["key"]
        for s in schema
        if s.get("type") in ("boolean", "number", "integer")
        and not s.get("secret")
        and not SENSITIVE.search(s.get("key", ""))
    )
    return {k: v if k in public else "[redacted]" for k, v in values.items()}


def redact(value: Any) -> Any:
    """Conservative operator output; full URLs and unstructured strings stay private."""
    if isinstance(value, dict):
        return {
            k: "[redacted]" if SENSITIVE.search(k) or "url" in k.lower() else redact(v)
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [redact(v) for v in value]
    if isinstance(value, str):
        return "[redacted]"
    return value
