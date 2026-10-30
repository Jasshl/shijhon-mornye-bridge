"""Canonical byte escaping; dots escape too, avoiding collisions with literal .xx IDs."""

import re

ITEM = re.compile(r"^[A-Za-z0-9.]{1,190}$")


# This contains a non-hex escape, so no canonically encoded native album can collide.
SINGLE_PREFIX = ".single."


def single_id(track_id: str) -> str:
    value = SINGLE_PREFIX + encode_id(track_id)
    if not ITEM.fullmatch(value):
        raise ValueError("single id exceeds protocol limit")
    return value


def single_track_id(value: str) -> str:
    if not ITEM.fullmatch(value) or not value.startswith(SINGLE_PREFIX):
        raise ValueError("invalid single id")
    return decode_id(value[len(SINGLE_PREFIX) :])


def encode_id(value: str) -> str:
    encoded = "".join(
        chr(b) if chr(b).isascii() and chr(b).isalnum() else f".{b:02x}"
        for b in value.encode("utf-8")
    )
    if not ITEM.fullmatch(encoded):
        raise ValueError("native id exceeds protocol limit")
    return encoded


def decode_id(value: str) -> str:
    if not ITEM.fullmatch(value):
        raise ValueError("invalid id")
    result = bytearray()
    i = 0
    while i < len(value):
        if value[i] == ".":
            if i + 2 >= len(value):
                raise ValueError("invalid escape")
            result.append(int(value[i + 1 : i + 3], 16))
            i += 3
        else:
            result.extend(value[i].encode())
            i += 1
    decoded = result.decode("utf-8")
    if encode_id(decoded) != value:
        raise ValueError("noncanonical id")
    return decoded


# A recording the extension found by description (title, artist, length, ISRC), not by
# one of its own IDs. ".r" is no hex escape, so no canonically encoded native ID starts so.
RECORDING_PREFIX = ".rec."
_DIGITS = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
_SEPARATOR = "\x1f"
_INLINE = 1
_REFERENCE = 2
# Cache and memory keys of recordings; native IDs are never stored with this prefix.
RECORDING_KEY = "\x00"
# ISRC ("" when none), title, artist, duration in ms (0 when unknown), album ("" when none).
Recording = tuple[str, str, str, int, str]


def _base62(data: bytes) -> str:
    # The leading 0x01 keeps leading zero bytes through the integer conversion.
    number = int.from_bytes(b"\x01" + data)
    digits = []
    while number:
        number, rest = divmod(number, 62)
        digits.append(_DIGITS[rest])
    return "".join(reversed(digits))


def _unbase62(value: str) -> bytes:
    number = 0
    for character in value:
        number = number * 62 + _DIGITS.index(character)
    data = number.to_bytes((number.bit_length() + 7) // 8)
    if not data or data[0] != 1 or _base62(data[1:]) != value:
        raise ValueError("noncanonical recording id")
    return data[1:]


def recording_id(fields: Recording) -> str | None:
    """The reversible ID of (ISRC, title, artist, duration in ms, album), or None when
    it exceeds the protocol bound even without the album."""
    isrc, title, artist, duration, album = fields
    if any(_SEPARATOR in part for part in (isrc, title, artist, album)):
        return None
    for kept in (album, ""):
        payload = _SEPARATOR.join((isrc, title, artist, str(duration), kept))
        value = RECORDING_PREFIX + _base62(bytes([_INLINE]) + payload.encode())
        if ITEM.fullmatch(value):
            return value
    return None


def reference_id(digest: bytes) -> str:
    """A bounded ID naming a remembered recording too long to carry inline."""
    return RECORDING_PREFIX + _base62(bytes([_REFERENCE]) + digest)


def parse_recording(value: str) -> Recording | bytes:
    """Inline fields of a recording ID, or the digest of a reference ID."""
    if not ITEM.fullmatch(value) or not value.startswith(RECORDING_PREFIX):
        raise ValueError("invalid recording id")
    rest = value[len(RECORDING_PREFIX) :]
    if not rest.isalnum():
        raise ValueError("invalid recording id")
    data = _unbase62(rest)
    if data[:1] == bytes([_REFERENCE]) and len(data) == 17:
        return data[1:]
    if data[:1] != bytes([_INLINE]):
        raise ValueError("invalid recording id")
    parts = data[1:].decode().split(_SEPARATOR)
    if (
        len(parts) != 5
        or not parts[1]
        or not parts[2]
        or not parts[3].isascii()
        or not parts[3].isdigit()
        or str(int(parts[3])) != parts[3]
    ):
        raise ValueError("invalid recording id")
    return parts[0], parts[1], parts[2], int(parts[3]), parts[4]


def internal_id(value: str) -> str:
    """The cache/memory key of a protocol track ID: the native ID, or a recording key."""
    if value.startswith(RECORDING_PREFIX):
        parse_recording(value)
        return RECORDING_KEY + value
    return decode_id(value)


def external_id(key: str) -> str:
    return key[len(RECORDING_KEY) :] if key.startswith(RECORDING_KEY) else encode_id(key)
