from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import logging
import os
import signal
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import httpx
import uvicorn

from .config import Config
from .errors import BridgeError
from .server import create_app
from .service import Service


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="mornye-bridge")
    p.add_argument("--config", type=Path, default=Path("config.toml"))
    p.add_argument(
        "--private-output", type=Path, help="Write private operator results to a new 0600 file"
    )
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("serve")
    sub.add_parser(
        "set-password",
        help="Set or replace the management page password (non-empty, at most 1024 characters)",
    )
    sub.add_parser("rotate-tokens", help="Replace add-on and CLI tokens; recopy all add-on links")
    sub.add_parser("check")
    sub.add_parser("links", help="Write secret add-on links (requires --private-output)")
    reg = sub.add_parser("registry").add_subparsers(dest="operation", required=True)
    reg.add_parser("get")
    set_reg = reg.add_parser("set")
    set_reg.add_argument("url", help="HTTPS registry address you trust")
    available = sub.add_parser("available")
    available.add_argument("--refresh", action="store_true")
    sub.add_parser("list")
    for command in (
        "install",
        "update",
        "remove",
        "enable",
        "disable",
        "actions",
        "health",
        "describe",
    ):
        sub.add_parser(command).add_argument("extension")
    settings = sub.add_parser("settings").add_subparsers(dest="operation", required=True)
    for command in ("show", "set"):
        item = settings.add_parser(command)
        item.add_argument("extension")
        if command == "set":
            item.add_argument(
                "--input", type=Path, required=True, help="JSON object file; - reads stdin"
            )
    action = sub.add_parser("action")
    action.add_argument("extension")
    action.add_argument("action")
    action.add_argument("--input", type=Path, help="JSON argument array file; - reads stdin")
    auth = sub.add_parser("pending-auth").add_subparsers(dest="operation", required=True)
    for command in ("show", "grant", "callback"):
        item = auth.add_parser(command)
        item.add_argument("extension")
        if command != "show":
            item.add_argument(
                "--input", type=Path, required=True, help="Private text file; - reads stdin"
            )
    cancel = sub.add_parser("cancel")
    cancel.add_argument("extension")
    cancel.add_argument("id", help="Encoded add-on track ID")
    cancel.add_argument("--format")
    return p


def read_input(path: Path) -> str:
    return sys.stdin.read() if str(path) == "-" else path.read_text()


async def execute(args: argparse.Namespace, config: Config) -> Any:
    params = {
        k: v
        for k, v in vars(args).items()
        if k in ("extension", "action", "refresh", "id", "format") and v is not None
    }
    operation = args.command
    if operation in ("registry", "settings", "pending-auth"):
        operation += "." + args.operation
    if operation == "registry.set" and args.url:
        params["url"] = args.url
    if getattr(args, "input", None):
        value = read_input(args.input)
        if operation == "settings.set":
            params["values"] = json.loads(value)
        elif operation == "action":
            params["arguments"] = json.loads(value)
            if not isinstance(params["arguments"], list):
                raise BridgeError("action_arguments_must_be_array", 400)
        else:
            params["grant" if operation.endswith("grant") else "url"] = value.strip()
    if args.command == "links" and not args.private_output:
        raise BridgeError("links_require_private_output", 400)
    admin_file = config.state_dir / "admin.token"
    # Reuse the running owner. Connection failures alone fall back to an exclusive offline owner.
    if admin_file.exists():
        try:
            async with httpx.AsyncClient(
                trust_env=False, timeout=config.call_timeout + 10
            ) as client:
                response = await client.post(
                    (
                        "http://127.0.0.1:"
                        f"{config.management_port if config.management_host else config.port}"
                        "/management"
                    ),
                    headers={"Authorization": "Bearer " + admin_file.read_text().strip()},
                    json={
                        "operation": operation,
                        "params": params,
                        "private": bool(args.private_output),
                    },
                )
                if response.is_error:
                    raise BridgeError("management_request_failed", response.status_code)
                return response.json()
        except httpx.ConnectError:
            pass
    service = Service(config)
    await service.start()
    try:
        return await service.execute(operation, params, private=bool(args.private_output))
    finally:
        await service.close()


async def serve_separate(config: Config) -> None:
    # Both listeners share one service owner, job queue, cache and native worker.
    class Listener(uvicorn.Server):
        @contextmanager
        def capture_signals(self) -> Iterator[None]:
            yield

    service = Service(config)
    await service.start()
    servers = [
        Listener(
            uvicorn.Config(
                create_app(service, surface=surface, manage_lifespan=False),
                host=host or "127.0.0.1",
                port=port,
                access_log=False,
                proxy_headers=False,
                log_level="critical",
                timeout_graceful_shutdown=int(config.shutdown_timeout),
            )
        )
        for surface, host, port in (
            ("api", config.host, config.port),
            ("management", config.management_host, config.management_port),
        )
    ]
    loop = asyncio.get_running_loop()

    def stop() -> None:
        for server in servers:
            server.should_exit = True

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop)
    tasks = [asyncio.create_task(server.serve()) for server in servers]
    try:
        # If either listener exits (including a bind failure), stop the other too.
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    finally:
        stop()
        await asyncio.gather(*tasks, return_exceptions=True)
        await service.close()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.remove_signal_handler(sig)


def main() -> None:
    args = parser().parse_args()
    try:
        config = Config.load(args.config)
        if args.command == "set-password":
            from .web_auth import set_password

            password = getpass.getpass(
                "New management password (non-empty, at most 1024 characters): "
            )
            if password != getpass.getpass("Repeat password: "):
                raise BridgeError("passwords_do_not_match", 400)
            set_password(config.state_dir, password)
            print("Management password saved; existing browser sessions revoked.")
            return
        if args.command == "serve":
            # Preparation and resolve lines only (codes and timings) on stderr.
            handler = logging.StreamHandler()
            handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
            bridge_log = logging.getLogger("mornye_bridge")
            bridge_log.addHandler(handler)
            bridge_log.setLevel(logging.INFO)
            bridge_log.propagate = False
            if config.management_host:
                asyncio.run(serve_separate(config))
                return
            # No access logs: path prefixes, selectors and query strings are credentials.
            uvicorn.run(
                create_app(Service(config)),
                host=config.host,
                port=config.port,
                access_log=False,
                proxy_headers=False,
                log_level="critical",
                timeout_graceful_shutdown=int(config.shutdown_timeout),
            )
            return
        result = asyncio.run(execute(args, config))
        if args.private_output:
            fd = os.open(args.private_output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w") as stream:
                json.dump(result, stream, indent=2)
                stream.write("\n")
            print("Private result written.")
        else:
            print(json.dumps(result, indent=2))
    except (BridgeError, ValueError, OSError, httpx.HTTPError) as exc:
        # Never print third-party exception text (it can contain a full address or credential).
        print(exc.code if isinstance(exc, BridgeError) else "operation_failed", file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
