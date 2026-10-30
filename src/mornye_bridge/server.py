from __future__ import annotations

import hmac
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response
from starlette.routing import Route
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from .errors import BridgeError
from .ids import RECORDING_PREFIX, SINGLE_PREFIX, decode_id, internal_id, single_track_id
from .service import Service, manifest, selected_format
from .web import Web


def create_app(
    service: Service, *, surface: str = "all", manage_lifespan: bool = True
) -> Starlette:
    @asynccontextmanager
    async def lifespan(app: Starlette) -> AsyncIterator[None]:
        if manage_lifespan:
            await service.start()
        try:
            if surface in {"all", "management"}:
                web.auth.start()
            yield
        finally:
            if manage_lifespan:
                await service.close()

    def answer(value: Any) -> Response:
        if len(json.dumps(value).encode()) > 1024**2:
            raise BridgeError("answer_exceeds_protocol_limit", 502)
        return JSONResponse(value, headers={"Cache-Control": "no-store"})

    def descriptor(entry: Any) -> dict[str, Any]:
        # Configured origin avoids reflecting Host headers into privileged fetch URLs.
        origin = service.config.public_url.rstrip("/") or f"http://127.0.0.1:{service.config.port}"
        return {"url": f"{origin}/audio/{entry.selector}", **entry.audio}

    async def addon(request: Request) -> Response:
        if not hmac.compare_digest(
            request.path_params["access"].encode(), service.access_token.encode()
        ):
            raise BridgeError("not_found", 404)
        ext = await service.extension(request.path_params["extension"])
        path = request.path_params.get("path", "")
        query = request.query_params
        if path == "manifest.json":
            return answer(manifest(ext, service.config))
        if path == "search":
            try:
                limit = int(query.get("limit", "20"))
            except ValueError:
                raise BridgeError("invalid_limit", 400) from None
            return answer(await service.catalog.search(ext, query.get("q", ""), limit))
        if path.startswith("catalog/isrc/") and ext.get("has_metadata_provider"):
            return answer(await service.catalog.isrc(ext, path[len("catalog/isrc/") :]))
        for prefix, kind in (
            ("album/", "album"),
            ("artist/", "artist"),
            ("catalog/track/", "track"),
        ):
            if path.startswith(prefix) and ext.get("has_metadata_provider"):
                if kind == "album" and path[len(prefix) :].startswith(SINGLE_PREFIX):
                    return answer(
                        await service.catalog.single(
                            ext["id"], single_track_id(path[len(prefix) :])
                        )
                    )
                return answer(
                    await service.catalog.lookup(ext["id"], kind, decode_id(path[len(prefix) :]))
                )
        if (
            path.startswith("catalog/artist/")
            and path.endswith("/releases")
            and ext.get("has_metadata_provider")
        ):
            decode_id(path[len("catalog/artist/") : -len("/releases")])
            return answer(await service.catalog.releases(ext["id"], query.get("cursor", "")))
        if not ext.get("has_download_provider"):
            raise BridgeError("not_found", 404)
        if path == "resolve":
            return answer(await service.resolve(ext, query))
        quality = (
            selected_format(ext, query.get("format"))
            if path == "availability" or path.startswith("stream/")
            else ""
        )
        if path.startswith("stream/"):
            return answer(descriptor(await service.stream(ext, path[len("stream/") :], quality)))
        assert service.cache
        if path.startswith("cached-stream/"):
            entry = service.cache.selector(path[len("cached-stream/") :])
            if not entry or entry.extension != ext["id"]:
                raise BridgeError("cache_expired", 410)
            return answer(descriptor(entry))
        if path in ("availability", "cached"):
            wanted = dict(query)
            entry = service.cached_match(
                ext["id"], wanted, quality=quality if path == "availability" else None
            )
            if path == "cached":
                return answer(
                    {"available": bool(entry), **({"selector": entry.selector} if entry else {})}
                )
            ident = query.get("id")
            recording = service.known_recording(ident) if ident else None
            if ident and recording and not entry:
                entry = service.ready(ext["id"], internal_id(ident), quality, recording)
            preparing = service.preparing(ext["id"], wanted, quality)
            missed = service.unavailable(ext["id"], wanted, quality) if not entry else False
            if missed:
                preparing = False
            elif ident and not entry:
                key = (ext["id"], internal_id(ident), quality)
                preparing = key in service.jobs
                forgotten = ident.startswith(RECORDING_PREFIX) and recording is None
                prepare = query.get("prepare", "").lower() in ("true", "1")
                if prepare and not preparing and not forgotten:
                    try:
                        service.begin(ext, key[1], quality, recording)
                        preparing = True
                    except BridgeError as exc:
                        if exc.code not in {"preparation_busy", "download_backoff"}:
                            raise
                        preparing = False
            elif not entry and not preparing and query.get("prepare", "").lower() in ("true", "1"):
                preparing = service.prepare_wanted(ext, wanted, quality)
            return answer(
                {
                    "available": bool(entry),
                    "preparing": preparing,
                    **({"id": entry.metadata["id"]} if entry else {}),
                }
            )
        raise BridgeError("not_found", 404)

    async def audio(request: Request) -> Response:
        assert service.cache
        selector = request.path_params["selector"]
        entry = service.cache.acquire(selector)
        if not entry:
            raise BridgeError("cache_expired", 404)
        try:
            # Starlette FileResponse implements GET/HEAD/ranges and closes the file on disconnect.
            return LeasedFile(entry.path, service=service, selector=selector)
        except BaseException:
            service.cache.release(selector)
            raise

    async def management(request: Request) -> Response:
        expected = "Bearer " + service.admin_token
        if not hmac.compare_digest(
            request.headers.get("authorization", "").encode(), expected.encode()
        ):
            raise BridgeError("not_found", 404)
        chunks = bytearray()
        async for chunk in request.stream():
            chunks.extend(chunk)
            if len(chunks) > 1024**2:
                raise BridgeError("request_too_large", 413)
        try:
            data = json.loads(chunks)
            if not isinstance(data, dict) or not isinstance(data.get("params", {}), dict):
                raise ValueError
            result = await service.execute(
                data["operation"], data.get("params", {}), private=data.get("private") is True
            )
        except (KeyError, ValueError, TypeError):
            raise BridgeError("invalid_management_request", 400) from None
        return answer(result)

    async def error(request: Request, exc: Exception) -> Response:
        if isinstance(exc, (ValueError, UnicodeError)):
            exc = BridgeError("invalid_id_or_argument", 400)
        if not isinstance(exc, BridgeError):
            exc = BridgeError("internal_error", 500)
        status = exc.status
        if request.url.path.startswith("/audio/") and status == 429:
            status = 503
        headers = {"Cache-Control": "no-store"}
        if exc.retry_after is not None:
            headers["Retry-After"] = str(exc.retry_after)
        return JSONResponse({"error": exc.code}, status_code=status, headers=headers)

    web = Web(service)
    app = Starlette(
        lifespan=lifespan,
        routes=[
            *(web.routes if surface in {"all", "management"} else []),
            *(
                [
                    Route("/addons/{access}/{extension}/{path:path}", addon),
                    Route("/audio/{selector}", audio, methods=["GET", "HEAD"]),
                ]
                if surface in {"all", "api"}
                else []
            ),
            *(
                [Route("/management", management, methods=["POST"])]
                if surface in {"all", "management"}
                else []
            ),
        ],
        exception_handlers={BridgeError: error, ValueError: error},
    )

    if surface in {"all", "management"}:
        app.add_middleware(
            ProxyHeadersMiddleware, trusted_hosts=service.config.management_trusted_proxies
        )
    app.state.web = web
    return app


class LeasedFile(FileResponse):
    def __init__(self, path: Any, *, service: Service, selector: str) -> None:
        # Platform MIME databases may call .m4a audio/mp4a-latm (raw LATM AAC).
        # These files contain MP4, including when their audio codec is AAC.
        media_type = "audio/mp4" if path.suffix.lower() in (".m4a", ".mp4") else None
        super().__init__(
            path, media_type=media_type, headers={"Cache-Control": "private, no-store"}
        )
        self.service = service
        self.selector = selector

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            assert self.service.cache
            self.service.cache.release(self.selector)
