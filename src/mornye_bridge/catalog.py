"""Generic mapping of the pinned extension getTrack/getAlbum/getArtist contract.

Raw action calls avoid the host normalizer's invented false flags and track numbers.
No provider identifiers, network APIs or provider-specific branches belong here.
"""

from __future__ import annotations

import asyncio
import re
import time
from collections.abc import Callable
from typing import Any

from .errors import BridgeError
from .ids import encode_id, single_id
from .management import Management


def text(value: Any) -> str:
    return value if isinstance(value, str) else ""


def number(value: Any) -> int:
    return int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0


def _map_item(raw: dict[str, Any], kind: str) -> dict[str, Any]:
    result: dict[str, Any] = {"id": encode_id(str(raw["id"]))}
    result["name" if kind == "artist" else "title"] = text(raw.get("name") or raw.get("title"))
    artists = raw.get("artists")
    if isinstance(artists, str):
        result["artist"] = artists
    elif isinstance(artists, list):
        result["artists"] = [
            {
                "name": text(a.get("name")),
                **({"id": encode_id(str(a["id"]))} if a.get("id") else {}),
            }
            for a in artists
            if isinstance(a, dict)
        ]
        result["artist"] = ", ".join(a["name"] for a in result["artists"])
    if "artists" not in result and raw.get("artist_id"):
        # The contract supplies a single artist id alongside a display credit.
        result["artists"] = [{"id": encode_id(str(raw["artist_id"])), "name": text(artists)}]
    for source, target in (
        ("release_date", "releaseDate"),
        ("album_type", "kind"),
        ("label", "label"),
        ("upc", "upc"),
    ):
        if raw.get(source):
            result[target] = raw[source]
    for key in ("explicit", "clean"):
        if isinstance(raw.get(key), bool):
            result[key] = raw[key]
    genres = raw.get("genres") or raw.get("genre")
    if isinstance(genres, str) and genres:
        result["genres"] = [genres]
    elif isinstance(genres, list):
        result["genres"] = [x for x in genres if isinstance(x, str)]
    artwork = raw.get("cover_url") or raw.get("image_url") or raw.get("images")
    if isinstance(artwork, str) and artwork.startswith(("https://", "http://")):
        result["artworkTemplate" if "{w}" in artwork and "{h}" in artwork else "artworkURL"] = (
            artwork
        )
    if kind == "track":
        result["durationMs"] = number(raw.get("duration_ms"))
        for source, target in (("disc_number", "discNumber"), ("track_number", "trackNumber")):
            n = number(raw.get(source))
            if n > 0:
                result[target] = n
        isrc = text(raw.get("isrc")).upper()
        if re.fullmatch(r"[A-Z0-9]{12}", isrc):
            result["isrc"] = isrc
        result["album"] = {"title": text(raw.get("album_name"))}
        if raw.get("album_id"):
            result["album"]["id"] = encode_id(str(raw["album_id"]))
        elif not result["album"]["title"]:
            result["album"] = {
                "id": single_id(str(raw["id"])),
                "title": result["title"] + " - Single",
            }
            result["discNumber"] = result["trackNumber"] = 1
    elif kind == "album":
        if number(raw.get("total_tracks")) > 0:
            result["trackCount"] = number(raw["total_tracks"])
    return result


def map_item(raw: dict[str, Any], kind: str) -> dict[str, Any]:
    try:
        return _map_item(raw, kind)
    except (ValueError, KeyError, TypeError, OverflowError):
        raise BridgeError("invalid_native_metadata", 502) from None


class Catalog:
    def __init__(
        self, management: Management, guard: Callable[[str], None] = lambda _: None
    ) -> None:
        self.management = management
        self.guard = guard
        self.isrc_failures: dict[str, tuple[int, float]] = {}

    async def isrc(self, extension: dict[str, Any], isrc: str) -> dict[str, Any]:
        isrc = isrc.upper()
        if not re.fullmatch(r"[A-Z0-9]{12}", isrc):
            raise BridgeError("invalid_isrc", 400)
        self.guard(extension["id"])
        count, expires = self.isrc_failures.get(extension["id"], (0, 0))
        if expires <= time.monotonic():
            count = 0
        elif count >= 3:
            raise BridgeError("isrc_search_unsupported_temporarily", 404)
        # Search only the declared track filter, or the extension's default search.
        filters = (extension.get("search_behavior") or {}).get("filters") or []
        filters = [
            x
            for x in filters
            if isinstance(x, dict) and x.get("id") in {"track", "tracks", "song", "songs"}
        ]
        result = await self.search(extension | {"search_behavior": {"filters": filters}}, isrc)
        tracks = result.get("tracks", [])
        if tracks and not any(track.get("isrc") for track in tracks):
            current, until = self.isrc_failures.get(extension["id"], (0, 0))
            if until > time.monotonic():
                count = max(count, current)
            self.isrc_failures[extension["id"]] = (count + 1, time.monotonic() + 900)
        elif tracks:
            self.isrc_failures.pop(extension["id"], None)
        return {"tracks": [track for track in tracks if track.get("isrc") == isrc]}

    async def search(
        self, extension: dict[str, Any], query: str, limit: int = 20
    ) -> dict[str, Any]:
        if not query or len(query) > 500 or not 1 <= limit <= 100:
            raise BridgeError("invalid_search", 400)
        self.guard(extension["id"])
        filters = (extension.get("search_behavior") or {}).get("filters") or []
        selected: list[tuple[str, str]] = []
        for kind in ("track", "album", "artist"):
            for item in filters:
                name = item.get("id", "")
                aliases = {kind, kind + "s"} | ({"song", "songs"} if kind == "track" else set())
                if name in aliases:
                    selected.append((kind, name))
                    break
        if not selected:
            # An absent/unknown filter schema means use the extension's own default search.
            values = await self.management.raw(
                extension["id"], "customSearch", [query, {"limit": limit, "offset": 0}]
            )
            if not isinstance(values, list):
                raise BridgeError("invalid_native_search", 502)
            results: dict[str, Any] = {"tracks": [], "albums": [], "artists": []}
            for value in values:
                if not isinstance(value, dict) or not value.get("id"):
                    continue
                kind = value.get("item_type") or value.get("type") or "track"
                if kind in ("song", "songs", "tracks"):
                    kind = "track"
                if kind in ("track", "album", "artist"):
                    results[kind + "s"].append(map_item(value, kind))
            return results

        async def search_kind(kind: str, native_filter: str) -> tuple[str, list[dict[str, Any]]]:
            self.guard(extension["id"])
            values = await self.management.raw(
                extension["id"],
                "customSearch",
                [query, {"filter": native_filter, "limit": limit, "offset": 0}],
            )
            if not isinstance(values, list):
                raise BridgeError("invalid_native_search", 502)
            return kind + "s", [
                map_item(x, kind) for x in values if isinstance(x, dict) and x.get("id")
            ]

        return dict(await asyncio.gather(*(search_kind(*s) for s in selected)))

    async def single(self, extension: str, native_track_id: str) -> dict[str, Any]:
        track = await self.lookup(extension, "track", native_track_id)
        ident = single_id(native_track_id)
        if track["album"].get("id") != ident:
            raise BridgeError("not_found", 404)
        return {
            "id": ident,
            "title": track["album"]["title"],
            "kind": "single",
            "trackCount": 1,
            "tracks": [track],
            **{
                key: track[key]
                for key in (
                    "artist",
                    "artists",
                    "artworkURL",
                    "artworkTemplate",
                    "releaseDate",
                    "explicit",
                    "clean",
                    "genres",
                )
                if key in track
            },
        }

    async def native_item(self, extension: str, kind: str, ident: str) -> dict[str, Any]:
        self.guard(extension)
        value = await self.management.raw(extension, "get" + kind.title(), [ident])
        if value is None:
            raise BridgeError("not_found", 404)
        if not isinstance(value, dict) or not value.get("id"):
            raise BridgeError("invalid_native_metadata", 502)
        return value

    async def lookup(self, extension: str, kind: str, ident: str) -> dict[str, Any]:
        value = await self.native_item(extension, kind, ident)
        result = map_item(value, kind)
        if kind == "album":
            tracks = value.get("tracks") or []
            if len(tracks) > 500:
                raise BridgeError("album_exceeds_protocol_limit", 502)
            result["tracks"] = [
                map_item(t | {"album_id": value["id"], "album_name": result["title"]}, "track")
                for t in tracks
                if isinstance(t, dict) and t.get("id")
            ]
            for track in result["tracks"]:
                track["album"] = {"id": result["id"], "title": result["title"]}
        if kind == "artist":
            result["albums"] = [
                map_item(x, "album")
                for x in (value.get("albums") or value.get("releases") or [])
                if isinstance(x, dict) and x.get("id")
            ]
            result["topTracks"] = [
                map_item(x, "track")
                for x in value.get("top_tracks", [])
                if isinstance(x, dict) and x.get("id")
            ]
            cursor = value.get("albums_next") or None
            if cursor is not None and (not isinstance(cursor, str) or len(cursor) > 512):
                raise BridgeError("native_cursor_exceeds_protocol_limit", 502)
            result["releasesNext"] = cursor
            # Continuation pages may return the cursor rather than the original profile ID.
            result["id"] = encode_id(ident)
        return result

    async def releases(self, extension: str, cursor: str) -> dict[str, Any]:
        if not cursor or len(cursor) > 512:
            raise BridgeError("invalid_cursor", 400)
        self.guard(extension)
        value = await self.management.raw(extension, "getArtist", [cursor])
        if not isinstance(value, dict):
            raise BridgeError("invalid_native_metadata", 502)
        albums = value.get("albums") or value.get("releases") or []
        next_cursor = value.get("albums_next") or None
        if next_cursor is not None and (not isinstance(next_cursor, str) or len(next_cursor) > 512):
            raise BridgeError("native_cursor_exceeds_protocol_limit", 502)
        return {
            "albums": [map_item(x, "album") for x in albums if isinstance(x, dict) and x.get("id")],
            "next": next_cursor,
        }
