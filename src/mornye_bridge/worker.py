"""Private newline JSON pipe protocol. Native stdout/stderr never enters the result pipe."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from typing import Any

from .native import NativeFailure, NativeHost

# Keep the callback and CDLL alive until OS process teardown (no native global destroy exists).
_keepalive: NativeHost | None = None


async def run() -> None:
    global _keepalive
    # Preserve stdout for IPC, then route all native/Python stdout to captured stderr.
    pipe = os.fdopen(os.dup(1), "w", buffering=1)
    os.dup2(2, 1)
    config = json.loads(await asyncio.to_thread(sys.stdin.readline))
    host = NativeHost(config)
    _keepalive = host
    mapping: dict[int, int] = {}
    tasks: set[asyncio.Task[None]] = set()

    def send(value: Any) -> None:
        pipe.write(json.dumps(value, separators=(",", ":")) + "\n")

    async def finish(ident: int, future: asyncio.Future[Any]) -> None:
        try:
            send({"id": ident, "result": await future})
        except NativeFailure as exc:
            send({"id": ident, "error": exc.payload})
        finally:
            mapping.pop(ident, None)

    async def events() -> None:
        while True:
            send({"event": await host.events.get()})

    await host.initialize()
    send({"ready": True})
    event_task = asyncio.create_task(events())
    try:
        while line := await asyncio.to_thread(sys.stdin.readline):
            message = json.loads(line)
            if "cancel" in message:
                native_id = mapping.get(message["cancel"])
                if native_id is not None:
                    host.cancel(native_id)
                continue
            ident = message["id"]
            native_id, future = host.start(message["method"], message.get("args"))
            mapping[ident] = native_id
            task = asyncio.create_task(finish(ident, future))
            tasks.add(task)
            task.add_done_callback(tasks.discard)
    finally:
        event_task.cancel()
        for native_id in mapping.values():
            host.cancel(native_id)


def main() -> None:
    try:
        asyncio.run(run())
    finally:
        # Native callbacks can still be active. Skip Python object teardown.
        os._exit(0)


if __name__ == "__main__":
    main()
