from __future__ import annotations

import asyncio
import ctypes
import json
import threading

from mornye_bridge import native


class Function:
    def __init__(self, fn):
        self.fn = fn
        self.argtypes = None
        self.restype = None

    def __call__(self, *args):
        return self.fn(*args)


class Library:
    def __init__(self):
        self.freed = []
        self.buffers = []
        self.mornye_init = Function(self.initialize)
        self.mornye_call = Function(self.call)
        self.mornye_free = Function(lambda ptr, length: self.freed.append((ptr, length)))
        self.mornye_cancel = Function(lambda ident: None)
        self.mornye_subscribe = Function(lambda kind: None)
        self.mornye_unsubscribe = Function(lambda kind: None)

    def initialize(self, callback, data, size):
        self.callback = callback

    def emit(self, ident, kind, raw):
        buffer = ctypes.create_string_buffer(raw)
        self.buffers.append(buffer)
        self.callback(ident, kind, ctypes.addressof(buffer), len(raw))

    def call(self, ident, method, data, size, binary, binary_size):
        if method == b"inline":
            self.emit(ident, 0, json.dumps(json.dumps({"invented": True})).encode())
        else:
            thread = threading.Thread(target=lambda: self.emit(ident, 2, b"abc\0def"))
            thread.start()
            thread.join()


async def test_ctypes_inline_thread_binary_event_buffers(monkeypatch, tmp_path) -> None:
    library = Library()
    monkeypatch.setattr(native.C, "CDLL", lambda path: library)
    host = native.NativeHost(
        {"library": "unused", "root": str(tmp_path), "ffmpeg": "ffmpeg", "ffprobe": "ffprobe"}
    )
    ident, future = host.start("inline", None)
    assert await future == {"invented": True}
    assert ident not in host.pending
    assert await host.call("binary") == {"$binary": "YWJjAGRlZg=="}
    library.emit(0, 10, b'{"items":{}}')
    library.emit(0, 13, b'{"message":"private-token"}')
    library.emit(0, 99, b"{}")
    library.emit(0, 0, b"bad json")
    await asyncio.sleep(0)
    assert (await host.events.get())["kind"] == 10
    assert host.events.empty()
    assert len(library.freed) == 6
    assert len(set(library.freed)) == 6
