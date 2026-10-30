from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

from mornye_bridge.errors import BridgeError
from mornye_bridge.supervisor import Supervisor


def worker(config) -> Supervisor:
    config.initialize()
    return Supervisor(config, [sys.executable, str(Path(__file__).with_name("pipe_worker.py"))])


async def test_worker_crash_and_timeout_recover(config) -> None:
    config.call_timeout = 0.15
    host = worker(config)
    try:
        assert await host.call("echo", {"invented": True}) == {"invented": True}
        with pytest.raises(BridgeError):
            await host.call("crash")
        assert await host.call("echo", [1]) == [1]
        with pytest.raises(BridgeError, match="worker_timeout"):
            await host.call("hang")
        assert host.restarts >= 1
        assert await host.call("echo", [2]) == [2]
    finally:
        await host.close()


async def test_cancellation_drains_or_restarts_and_admission(config) -> None:
    config.call_timeout = 0.15
    config.admission = 1
    config.cancel_timeout = 0.15
    host = worker(config)
    try:
        await host.call("echo")
        task = asyncio.create_task(host.call("hang"))
        await asyncio.sleep(0.01)
        with pytest.raises(BridgeError, match="worker_busy"):
            await host.call("echo")
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert host.restarts == 0  # Callers return before a missing reply is drained.
        await asyncio.gather(*host.drainers)
        assert (config.state_dir / "cancel-observed").exists()
        assert host.restarts == 1
        assert await host.call("echo", 9) == 9
    finally:
        await host.close()


async def test_native_stderr_is_discarded(config, capsys) -> None:
    host = worker(config)
    try:
        await host.call("noisy", 7)
        assert host.stderr_bytes > 0
        captured = capsys.readouterr()
        assert "private-token" not in captured.err + captured.out
        assert not any("private-token" in repr(v) for v in vars(host).values())
    finally:
        await host.close()
