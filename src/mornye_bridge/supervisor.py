from __future__ import annotations

import asyncio
import itertools
import json
import os
import signal
import sys
import time
from collections.abc import Sequence
from contextlib import suppress
from typing import Any, Protocol

from .config import Config
from .errors import BridgeError, native_error


class Worker(Protocol):
    async def call(self, method: str, args: Any = None) -> Any: ...
    async def close(self) -> None: ...


class Supervisor:
    """One library owner, bounded admission, restart on crash/deadline; no native imports."""

    def __init__(self, config: Config, command: Sequence[str] | None = None) -> None:
        self.config = config
        self.command = list(command or [sys.executable, "-m", "mornye_bridge.worker"])
        self.process: asyncio.subprocess.Process | None = None
        self.pending: dict[int, asyncio.Future[Any]] = {}
        self.events: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=128)
        self.ids = itertools.count(1)
        self.lock = asyncio.Lock()
        self.readers: list[asyncio.Task[None]] = []
        self.ready: asyncio.Future[None] | None = None
        self.active = 0
        self.restarts = 0
        self.stderr_bytes = 0
        self.closed = False
        self.healthy = False
        self.downloads: dict[str, tuple[float, dict[str, Any]]] = {}
        self.drainers: set[asyncio.Task[None]] = set()

    async def _ensure(self) -> asyncio.subprocess.Process:
        async with self.lock:
            if self.closed:
                raise BridgeError("worker_stopped")
            if (
                self.process
                and self.healthy
                and self.process.returncode is None
                and self.ready
                and self.ready.done()
            ):
                return self.process
            await self._stop()
            process = await asyncio.create_subprocess_exec(
                *self.command,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                limit=16 * 1024**2,
                start_new_session=True,
            )
            self.process = process
            self.healthy = True
            self.ready = asyncio.get_running_loop().create_future()
            self.readers = [
                asyncio.create_task(self._read(process)),
                asyncio.create_task(self._stderr(process)),
            ]
            assert process.stdin is not None
            process.stdin.write(json.dumps(self.config.worker_config()).encode() + b"\n")
            await process.stdin.drain()
            try:
                await asyncio.wait_for(asyncio.shield(self.ready), self.config.call_timeout)
            except (TimeoutError, BridgeError):
                await self._stop()
                raise BridgeError("worker_start_failed") from None
            return process

    async def _read(self, process: asyncio.subprocess.Process) -> None:
        assert process.stdout is not None
        try:
            while line := await process.stdout.readline():
                message = json.loads(line)
                if "ready" in message:
                    if self.ready and not self.ready.done():
                        self.ready.set_result(None)
                elif "event" in message:
                    self._progress(message["event"])
                    if not self.events.full():
                        self.events.put_nowait(message["event"])
                else:
                    future = self.pending.pop(message["id"], None)
                    if future is not None and not future.done():
                        if "error" in message:
                            future.set_exception(native_error(message["error"]))
                        else:
                            future.set_result(message.get("result"))
        except (ValueError, KeyError):
            pass
        finally:
            if process is self.process:
                self.healthy = False
                self._fail_pending()
                if self.ready and not self.ready.done():
                    self.ready.set_exception(BridgeError("worker_crashed"))

    async def _stderr(self, process: asyncio.subprocess.Process) -> None:
        assert process.stderr is not None
        while chunk := await process.stderr.read(4096):
            # Capture and redact at the boundary. No retained text, including partial lines.
            self.stderr_bytes += len(chunk)

    def _fail_pending(self) -> None:
        for future in self.pending.values():
            if not future.done():
                future.set_exception(BridgeError("worker_restarted"))
        self.pending.clear()

    async def _stop(self) -> None:
        process = self.process
        self.process = None
        if process:
            # Kill child FFmpeg processes too, even when the worker has already crashed.
            with suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
            await process.wait()
        for task in self.readers:
            task.cancel()
        await asyncio.gather(*self.readers, return_exceptions=True)
        self.readers.clear()
        self._fail_pending()

    async def restart(self, process: asyncio.subprocess.Process) -> None:
        async with self.lock:
            if self.process is process:
                self.restarts += 1
                await self._stop()
        # Lazy restart: the next call starts a clean owner; never replay a mutation.

    def _progress(self, event: dict[str, Any]) -> None:
        if event.get("kind") != 10:
            return
        value = event.get("value", {})
        if value.get("reset"):
            for item_id, (last, _previous) in self.downloads.items():
                if item_id not in (value.get("items") or {}):
                    self.downloads[item_id] = (last, {})
        for item_id in value.get("removed") or []:
            if item_id in self.downloads:
                self.downloads[item_id] = (self.downloads[item_id][0], {})
        for item_id, delta in (value.get("items") or {}).items():
            if item_id not in self.downloads or not isinstance(delta, dict):
                continue
            last, previous = self.downloads[item_id]
            # Deltas are merged per item. Heartbeats/speed alone are not forward progress.
            current = dict(delta) if value.get("reset") else previous | delta
            advanced = any(
                isinstance(current.get(key), (int, float)) and current[key] > previous.get(key, 0)
                for key in ("bytes_received", "progress")
            ) or any(current.get(key) != previous.get(key) for key in ("stage", "status"))
            self.downloads[item_id] = (time.monotonic() if advanced else last, current)

    async def _download_reply(self, future: asyncio.Future[Any], item_id: str) -> Any:
        deadline = time.monotonic() + self.config.download_timeout
        while True:
            last = self.downloads[item_id][0]
            remaining = min(deadline, last + self.config.download_stall_timeout) - time.monotonic()
            if remaining <= 0:
                raise TimeoutError
            try:
                return await asyncio.wait_for(asyncio.shield(future), remaining)
            except TimeoutError:
                if time.monotonic() >= deadline:
                    raise
                # A progress delta received while waiting may have moved the stall deadline.

    async def call(self, method: str, args: Any = None) -> Any:
        if self.active >= self.config.admission:
            raise BridgeError("worker_busy", 503, 3)
        self.active += 1
        ident = next(self.ids)
        future: asyncio.Future[Any] | None = None
        process: asyncio.subprocess.Process | None = None
        item_id = json.loads(args)["item_id"] if method == "downloadByStrategy" else ""
        deferred = False
        try:
            process = await self._ensure()
            assert process.stdin is not None
            future = asyncio.get_running_loop().create_future()
            self.pending[ident] = future
            if item_id:
                self.downloads[item_id] = (time.monotonic(), {})
            process.stdin.write(
                json.dumps({"id": ident, "method": method, "args": args}).encode() + b"\n"
            )
            await process.stdin.drain()
            if item_id:
                try:
                    return await self._download_reply(future, item_id)
                except TimeoutError:
                    await self._cancel(ident, future, process, item_id)
                    raise BridgeError(
                        "download_timeout", retry_after=int(self.config.download_backoff)
                    ) from None
            return await asyncio.wait_for(asyncio.shield(future), self.config.call_timeout)
        except TimeoutError:
            if process:
                await self.restart(process)
            raise BridgeError("worker_timeout") from None
        except asyncio.CancelledError:
            if future and process:
                if item_id:
                    # Do not remove staging until the native download replies or its owner is dead.
                    await self._cancel(ident, future, process, item_id)
                else:
                    deferred = True
                    task = asyncio.create_task(self._drain_cancel(ident, future, process))
                    self.drainers.add(task)
                    task.add_done_callback(self.drainers.discard)
            raise
        except (BrokenPipeError, ConnectionResetError):
            if process:
                await self.restart(process)
            raise BridgeError("worker_crashed") from None
        finally:
            self.downloads.pop(item_id, None)
            if not deferred:
                self.active -= 1
            if future and future.done() and not future.cancelled():
                future.exception()

    async def _cancel(
        self,
        ident: int,
        future: asyncio.Future[Any],
        process: asyncio.subprocess.Process,
        item_id: str = "",
    ) -> None:
        # Repeated caller cancellation cannot interrupt the bounded native cleanup.
        cleanup = asyncio.create_task(self._cancel_native(ident, future, process, item_id))
        interrupted = False
        while True:
            try:
                await asyncio.shield(cleanup)
                break
            except asyncio.CancelledError:
                if cleanup.cancelled():
                    await self.restart(process)
                    raise
                interrupted = True
        if interrupted:
            raise asyncio.CancelledError

    async def _cancel_native(
        self,
        ident: int,
        future: asyncio.Future[Any],
        process: asyncio.subprocess.Process,
        item_id: str = "",
    ) -> None:
        try:
            async with asyncio.timeout(self.config.cancel_timeout):
                if process.stdin and process.returncode is None:
                    process.stdin.write(json.dumps({"cancel": ident}).encode() + b"\n")
                    if item_id:
                        # Cancellation bypasses admission through a reserved IPC lane.
                        cancel_id = next(self.ids)
                        ack = asyncio.get_running_loop().create_future()
                        self.pending[cancel_id] = ack
                        process.stdin.write(
                            json.dumps(
                                {
                                    "id": cancel_id,
                                    "method": "cancelDownload",
                                    "args": {"item_id": item_id},
                                }
                            ).encode()
                            + b"\n"
                        )
                        await process.stdin.drain()
                        await asyncio.gather(
                            asyncio.shield(future), asyncio.shield(ack), return_exceptions=True
                        )
                    else:
                        await process.stdin.drain()
                        await asyncio.shield(future)
        except (TimeoutError, BrokenPipeError, ConnectionResetError):
            # Missing cancellation reply is evidence of an unresponsive native call.
            await self.restart(process)
        except BridgeError:
            pass

    async def _drain_cancel(
        self, ident: int, future: asyncio.Future[Any], process: asyncio.subprocess.Process
    ) -> None:
        try:
            await self._cancel(ident, future, process)
        finally:
            self.active -= 1
            if future.done() and not future.cancelled():
                future.exception()

    async def close(self) -> None:
        async with self.lock:
            self.closed = True
            await self._stop()
        await asyncio.gather(*self.drainers, return_exceptions=True)
