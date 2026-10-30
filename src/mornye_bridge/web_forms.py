"""Validated, write-only views of extension settings and transient action forms."""

from __future__ import annotations

import math
import re
from typing import Any

from .errors import BridgeError
from .privacy import SENSITIVE

IDENTIFIER = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,63}$")
TYPES = {"string", "text", "password", "otp", "number", "integer", "boolean", "select", "button"}


def secret(field: dict[str, Any]) -> bool:
    return field.get("type", "string") in {"string", "text", "password", "otp"} and bool(
        field.get("secret")
        or field.get("type") in {"password", "otp"}
        or SENSITIVE.search(str(field.get("key", "")))
        or re.search(r"credential|auth|login.*url", str(field.get("key", "")), re.I)
    )


def schema(item: dict[str, Any]) -> list[dict[str, Any]]:
    fields = [dict(field) for field in item.get("settings") or []]
    # Native quality-specific settings share the extension settings object.
    known = {field["key"] for field in fields}
    for quality in item.get("quality_options") or []:
        for field in quality.get("settings") or []:
            if field["key"] not in known:
                fields.append(dict(field) | {"group": quality.get("label", quality["id"])})
                known.add(field["key"])
    return fields


def fields_view(
    fields: list[dict[str, Any]], values: dict[str, Any], *, transient: bool = False
) -> list[dict[str, Any]]:
    result = []
    for index, field in enumerate(fields):
        kind = field.get("type", "string")
        private = secret(field)
        value = values.get(field["key"], field.get("default", ""))
        # Form defaults never prefill credentials; action text inputs start empty as upstream.
        view: dict[str, Any] = {
            key: field[key]
            for key in ("key", "label", "description", "required", "action", "group", "options")
            if key in field
        }
        view.update(
            type=kind,
            secret=private,
            supported=kind in TYPES,
            value="" if private or (transient and kind != "select") else value,
            stored=values.get(field["key"], field.get("default")) not in (None, "")
            if private
            else False,
            input_id=f"field-{index}",
        )
        if private:
            view.pop("options", None)
        result.append(view)
    return result


def parse_values(
    fields: list[dict[str, Any]],
    data: dict[str, str],
    *,
    stored: dict[str, Any] | None = None,
    transient: bool = False,
) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for field in fields:
        key = field["key"]
        kind = field.get("type", "string")
        if kind == "button" or kind not in TYPES:
            continue
        private = secret(field)
        value: Any = data.get("value:" + key, "")
        clear = private and not transient and data.get("clear:" + key) == "yes"
        if private and not transient and not value and not clear:
            if field.get("required") and (stored or {}).get(key, field.get("default")) in (
                None,
                "",
            ):
                raise BridgeError("required_setting_missing", 400)
            continue
        if clear:
            result[key] = ""
            continue
        if field.get("required") and not value and kind != "boolean":
            raise BridgeError("required_setting_missing", 400)
        if len(value) > 4096:
            raise BridgeError("setting_too_long", 400)
        if kind == "boolean":
            value = value == "true"
        elif kind in {"number", "integer"} and value:
            try:
                value = int(value) if kind == "integer" else float(value)
            except ValueError:
                raise BridgeError("invalid_setting_number", 400) from None
            if not math.isfinite(value):
                raise BridgeError("invalid_setting_number", 400)
        elif kind == "select" and value and value not in field.get("options", []):
            raise BridgeError("invalid_setting_option", 400)
        result[key] = value
    return result


def action_form(result: Any) -> dict[str, Any] | None:
    payload = result.get("result", result) if isinstance(result, dict) else {}
    if not isinstance(payload, dict):
        return None
    raw = payload.get("action_form", payload.get("byoa_form"))
    if raw is None:
        return None
    if not isinstance(raw, dict) or raw.get("version") != 1:
        raise BridgeError("invalid_action_form", 502)
    for key in ("submit_action", "cancel_action"):
        value = raw.get(key)
        if key == "cancel_action" and not value:
            continue
        if not isinstance(value, str) or not IDENTIFIER.fullmatch(value):
            raise BridgeError("invalid_action_form", 502)
    for key, limit in (("title", 256), ("description", 4096)):
        value = raw.get(key)
        if value is not None and (not isinstance(value, str) or len(value) > limit):
            raise BridgeError("invalid_action_form", 502)
    fields = raw.get("fields")
    if not isinstance(fields, list) or not 1 <= len(fields) <= 12:
        raise BridgeError("invalid_action_form", 502)
    keys = set()
    for field in fields:
        if not isinstance(field, dict):
            raise BridgeError("invalid_action_form", 502)
        field_key = field.get("key")
        if (
            not isinstance(field_key, str)
            or not IDENTIFIER.fullmatch(field_key)
            or field_key in keys
        ):
            raise BridgeError("invalid_action_form", 502)
        keys.add(field_key)
        if field.get("type") not in {"string", "password", "otp", "number", "select"}:
            raise BridgeError("invalid_action_form", 502)
        if not isinstance(field.get("label", ""), str) or len(field.get("label", "")) > 256:
            raise BridgeError("invalid_action_form", 502)
        if field["type"] == "select":
            options = field.get("options")
            if (
                not isinstance(options, list)
                or not 1 <= len(options) <= 30
                or any(
                    not isinstance(option, str) or not option or len(option) > 256
                    for option in options
                )
                or len(set(options)) != len(options)
            ):
                raise BridgeError("invalid_action_form", 502)
            if field.get("default") is not None and field["default"] not in options:
                raise BridgeError("invalid_action_form", 502)
    return {
        key: raw[key]
        for key in ("submit_action", "cancel_action", "title", "description", "fields")
        if key in raw
    }
