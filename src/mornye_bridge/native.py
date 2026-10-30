"""The only module that loads the operator-built library. Imported by the worker only."""

from __future__ import annotations

import asyncio
import base64
import ctypes as C
import itertools
import json
from pathlib import Path
from typing import Any

from .config import HOST_VERSION

CALLBACK = C.CFUNCTYPE(None, C.c_uint64, C.c_int32, C.c_void_p, C.c_size_t)


class NativeFailure(Exception):
    def __init__(self, payload: Any) -> None:
        self.payload = payload
        super().__init__("native_failure")


def decoded(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            pass
    return value


class NativeHost:
    def __init__(self, config: dict[str, Any]) -> None:
        self.loop = asyncio.get_running_loop()
        self.lib = C.CDLL(config["library"])
        self.config = config
        self.ids = itertools.count(1)
        self.pending: dict[int, asyncio.Future[Any]] = {}
        self.events: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=128)
        signatures = {
            "mornye_init": [CALLBACK, C.c_void_p, C.c_size_t],
            "mornye_call": [C.c_uint64, C.c_char_p, C.c_void_p, C.c_size_t, C.c_void_p, C.c_size_t],
            "mornye_cancel": [C.c_uint64],
            "mornye_subscribe": [C.c_int32],
            "mornye_unsubscribe": [C.c_int32],
            "mornye_free": [C.c_void_p, C.c_size_t],
        }
        for name, args in signatures.items():
            fn = getattr(self.lib, name)
            fn.argtypes = args
            fn.restype = None
        self.callback = CALLBACK(self._callback)
        root = Path(config["root"])
        raw = json.dumps(
            {
                "app_support_dir": str(root / "support"),
                "documents_dir": str(root / "documents"),
                "temp_dir": str(root / "temp"),
                "ffmpeg_path": config["ffmpeg"],
                "ffprobe_path": config["ffprobe"],
                "app_version": HOST_VERSION,
            }
        ).encode()
        self.lib.mornye_init(self.callback, raw, len(raw))

    def _callback(self, ident: int, kind: int, pointer: int, length: int) -> None:
        try:
            try:
                raw = C.string_at(pointer, length) if pointer else b""
            finally:
                if pointer:
                    self.lib.mornye_free(pointer, length)
            value = {"$binary": base64.b64encode(raw).decode()} if kind == 2 else json.loads(raw)
            self.loop.call_soon_threadsafe(self._deliver, ident, kind, value)
        except BaseException:
            # Never unwind through Rust or print a buffer. Parent deadline recovers a bad reply.
            pass

    def _deliver(self, ident: int, kind: int, value: Any) -> None:
        if kind in (0, 1, 2):
            future = self.pending.pop(ident, None)
            if future is not None and not future.done():
                if kind == 1:
                    future.set_exception(NativeFailure(value))
                else:
                    future.set_result(decoded(value))
        elif kind in (10, 11, 12) and not self.events.full():
            self.events.put_nowait({"kind": kind, "value": value})
        # Kind 13 logs are deliberately discarded; they may contain credentials.

    def start(self, method: str, args: Any) -> tuple[int, asyncio.Future[Any]]:
        ident = next(self.ids)
        future = self.loop.create_future()
        self.pending[ident] = future
        raw = json.dumps(args).encode()
        self.lib.mornye_call(ident, method.encode(), raw, len(raw), None, 0)
        return ident, future

    async def call(self, method: str, args: Any = None) -> Any:
        _, future = self.start(method, args)
        return await future

    def cancel(self, ident: int) -> None:
        self.lib.mornye_cancel(ident)

    async def initialize(self) -> None:
        root = Path(self.config["root"])
        await self.call(
            "initExtensionSystem",
            {
                "extensions_dir": str(root / "extensions"),
                "data_dir": str(root / "data"),
                "master_key": (root / "master.key").read_text(),
                "allowed_directories": [str(root / "jobs")],
                "lyrics_providers_json": "[]",
                "lyrics_options_json": "{}",
            },
        )
        await self.call("loadExtensionsFromDir", {"dir_path": str(root / "extensions")})
        await self.call("initExtensionRepo", {"cache_dir": str(root / "repo")})
        self.lib.mornye_subscribe(10)
