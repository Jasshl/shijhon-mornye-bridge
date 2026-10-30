from __future__ import annotations

import asyncio
import json
import wave
from pathlib import Path
from typing import Any

import pytest

from mornye_bridge.config import Config

EXT = {
    "id": "invented",
    "display_name": "Invented Music",
    "version": "1.0",
    "enabled": True,
    "has_download_provider": True,
    "has_metadata_provider": True,
    "quality_options": [{"id": "native-aac", "label": "Native"}, {"id": "native-flac"}],
    "search_behavior": {"filters": [{"id": "tracks"}, {"id": "album"}, {"id": "artists"}]},
    "settings": [
        {"key": "region", "secret": False, "type": "string"},
        {"key": "mediaUserToken", "type": "string"},
        {"key": "other", "type": "string"},
    ],
}
TRACK = {
    "id": "track:one",
    "name": "Invented Song",
    "artists": "Invented Artist",
    "duration_ms": 1000,
    "album_id": "album:one",
    "album_name": "Invented Album",
    "artist_id": "artist:one",
    "isrc": "ZZABC2600001",
}
ALBUM = {
    "id": "album:one",
    "name": "Invented Album",
    "total_tracks": 1,
    "tracks": [TRACK],
    "label": "Invented Label",
    "upc": "123456789012",
    "genre": "Invented Genre",
}


class FakeWorker:
    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []
        self.enabled = True
        self.settings: dict[str, Any] = {
            "region": "xx",
            "mediaUserToken": "private-token",
            "other": "secret",
        }
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.release.set()
        self.cancelled = False
        self.fail: Exception | None = None

    async def call(self, method: str, args: Any = None) -> Any:
        self.calls.append((method, args))
        if self.fail and method not in ("getInstalledExtensions", "cancelDownload"):
            raise self.fail
        if method == "getInstalledExtensions":
            return [EXT | {"enabled": self.enabled}]
        if method == "getExtensionSettings":
            return dict(self.settings)
        if method == "setExtensionSettings":
            self.settings = json.loads(args["settings"])
        if method == "setExtensionEnabled":
            self.enabled = args["enabled"]
        if method == "getExtensionPendingAuth":
            return {"auth_url": "https://private.example.test/verify?grant=secret"}
        if method == "invokeExtensionAction":
            action = args["action"]
            arguments = json.loads(args.get("arguments_json", "[]"))
            if action == "customSearch":
                filtered = arguments[1]["filter"]
                return {
                    "tracks": [TRACK],
                    "album": [ALBUM],
                    "artists": [{"id": "artist:one", "name": "Invented Artist"}],
                }[filtered]
            if action == "getTrack":
                return TRACK | {"id": arguments[0]}
            if action == "getAlbum":
                return ALBUM
            if action == "getArtist":
                if arguments[0] == "next:page":
                    return {"id": "artist:one", "albums": [ALBUM | {"id": "album:two"}]}
                return {
                    "id": "artist:one",
                    "name": "Invented Artist",
                    "albums": [ALBUM],
                    "top_tracks": [TRACK],
                    "albums_next": "next:page",
                }
            return {
                "success": True,
                "setting_updates": {"region": "yy"},
                "action_form": {"submit_action": "continue", "fields": []},
            }
        if method == "downloadByStrategy":
            payload = json.loads(args)
            self.started.set()
            try:
                await self.release.wait()
            except asyncio.CancelledError:
                self.cancelled = True
                raise
            output = Path(payload["output_dir"]) / "invented.wav"
            with wave.open(str(output), "wb") as wav:
                wav.setnchannels(1)
                wav.setsampwidth(2)
                wav.setframerate(8000)
                wav.writeframes(b"\0" * 16000)
            return {"success": True, "file_path": str(output)}
        return None

    async def close(self) -> None:
        pass


@pytest.fixture
def config(tmp_path: Path) -> Config:
    return Config(library=tmp_path / "unused", state_dir=tmp_path / "state", stream_wait=0.02)
