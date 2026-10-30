"""Private server-rendered operator pages; native results never become generic HTML."""

from __future__ import annotations

import asyncio
import hmac
import secrets
import time
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, urlsplit

from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse, Response
from starlette.routing import Route
from starlette.templating import Jinja2Templates

from .config import CatalogConfig
from .errors import BridgeError
from .management import PASSING
from .service import Service, manifest, selected_format
from .web_auth import COOKIE, BrowserAuth, Session
from .web_forms import action_form, fields_view, parse_values, schema

HEADERS = {
    "Cache-Control": "no-store",
    "Referrer-Policy": "same-origin",
    "X-Content-Type-Options": "nosniff",
    "Content-Security-Policy": "default-src 'none'; style-src 'self'; script-src 'self'; "
    "connect-src 'self'; img-src 'self'; form-action 'self'; "
    "base-uri 'none'; frame-ancestors 'none'",
    "X-Frame-Options": "DENY",
}
NEEDS_AUTH = {
    "needs_login_or_verification",
    "needs_login",
    "unauthorized",
    "auth_required",
    "verification_required",
    "needs_verification",
}


def status_label(status: str) -> str:
    if status.lower() in PASSING:
        return "Ready"
    if status.lower() in NEEDS_AUTH:
        return "Needs login or verification"
    if status in {"unknown", "unchecked", ""}:
        return "Not checked"
    return "Unhealthy"


def browser_url(value: Any) -> str:
    if isinstance(value, str):
        if len(value) > 16384 or any(ord(character) < 32 for character in value):
            return ""
        parsed = urlsplit(value)
        if parsed.scheme in {"https", "http"} and parsed.hostname and not parsed.username:
            return value
    return ""


class Web:
    def __init__(self, service: Service) -> None:
        self.service = service
        self.auth = BrowserAuth(service.config.state_dir)
        self.templates = Jinja2Templates(directory=Path(__file__).parent / "templates")
        self.health_cache: dict[str, tuple[float, str]] = {}
        self.registry_cache: dict[str, tuple[float, list[dict[str, Any]]]] = {}
        self.routes = [
            Route("/", self.handle, methods=["GET", "POST"]),
            Route("/login", self.handle, methods=["GET", "POST"]),
            Route("/logout", self.handle, methods=["POST"]),
            Route("/links", self.handle, methods=["GET"]),
            Route("/extensions/{extension}", self.handle, methods=["GET", "POST"]),
            Route("/extensions/{extension}/open-auth", self.handle, methods=["GET"]),
            Route("/assets/{asset}", self.asset, methods=["GET"]),
        ]

    async def asset(self, request: Request) -> Response:
        name = request.path_params["asset"]
        if name not in {"web.css", "web.js"}:
            return Response(status_code=404)
        return Response(
            (Path(__file__).parent / "static" / name).read_text(),
            media_type="text/css" if name.endswith("css") else "text/javascript",
            headers=HEADERS,
        )

    def render(self, request: Request, session: Session, template: str, **context: Any) -> Response:
        notice, session.notice = session.notice, ""
        return self.templates.TemplateResponse(
            request,
            template,
            {
                "session": session,
                "csrf": session.csrf,
                "notice": notice,
                "extension_url": lambda key: "/extensions/" + quote(key, safe=""),
                **context,
            },
            headers=HEADERS,
        )

    def redirect(self, path: str) -> Response:
        return RedirectResponse(
            self.service.config.management_public_url + path, status_code=303, headers=HEADERS
        )

    def secure_cookie(self, request: Request) -> bool:
        return (
            self.service.config.management_public_url.startswith("https://")
            or request.url.scheme == "https"
        )

    def cookie(self, response: Response, request: Request, token: str) -> None:
        response.set_cookie(
            COOKIE,
            token,
            httponly=True,
            samesite="strict",
            secure=self.secure_cookie(request),
            max_age=28800,
            path="/",
        )

    async def data(self, request: Request) -> dict[str, str]:
        if request.headers.get("content-type", "").split(";")[0] != (
            "application/x-www-form-urlencoded"
        ):
            raise BridgeError("invalid_form", 400)
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > 65536:
                raise BridgeError("form_too_large", 413)
        try:
            values = parse_qs(body.decode("utf-8"), keep_blank_values=True, max_num_fields=256)
        except (ValueError, UnicodeError):
            raise BridgeError("invalid_form", 400) from None
        if any(len(value) != 1 for value in values.values()):
            raise BridgeError("invalid_form", 400)
        return {key: value[0] for key, value in values.items()}

    async def handle(self, request: Request) -> Response:
        token = request.cookies.get(COOKIE, "")
        session = self.auth.session(token)
        is_login = request.url.path == "/login"
        if session is None:
            if request.method == "POST":
                return Response(
                    "Session expired. Reload the page.", status_code=403, headers=HEADERS
                )
            if not is_login:
                return self.redirect("/login")
            token, session = self.auth.create()
        try:
            data = await self.data(request) if request.method == "POST" else {}
            if request.method == "POST":
                if not hmac.compare_digest(data.get("csrf", "").encode(), session.csrf.encode()):
                    raise BridgeError("invalid_csrf", 403)
                origin = request.headers.get("origin")
                expected_origin = self.service.config.management_public_url or str(
                    request.base_url
                ).rstrip("/")
                if origin and origin != expected_origin:
                    raise BridgeError("invalid_origin", 403)
            if is_login:
                response = await self.login(request, session, token, data)
            elif not session.authenticated:
                response = self.redirect("/login")
            elif request.url.path == "/logout":
                self.auth.sessions.pop(token, None)
                response = self.redirect("/login")
                response.delete_cookie(
                    COOKIE,
                    path="/",
                    secure=self.secure_cookie(request),
                    httponly=True,
                    samesite="strict",
                )
                return response
            elif "extension" in request.path_params:
                response = await self.extension(request, session, data)
            elif request.method == "POST":
                response = await self.dashboard_post(session, data)
            else:
                response = await self.dashboard(request, session)
        except BridgeError as exc:
            # Only bridge-authored codes are rendered, never native payloads or request values.
            response = self.render(
                request, session, "error.html", code=exc.code, authenticated=session.authenticated
            )
            response.status_code = exc.status
            if exc.retry_after is not None:
                response.headers["Retry-After"] = str(exc.retry_after)
        except OSError:
            response = self.render(
                request,
                session,
                "error.html",
                code="save_failed_check_state_directory_permissions",
                authenticated=session.authenticated,
            )
            response.status_code = 503
        except (ValueError, TypeError, KeyError):
            response = self.render(
                request,
                session,
                "error.html",
                code="invalid_form_or_configuration",
                authenticated=session.authenticated,
            )
            response.status_code = 400
        if token in self.auth.sessions:
            self.cookie(response, request, token)
        return response

    async def login(
        self, request: Request, session: Session, token: str, data: dict[str, str]
    ) -> Response:
        if session.authenticated:
            return self.redirect("/")
        error = ""
        configured = bool(self.auth.password_hash())
        if request.method == "POST":
            address = request.client.host if request.client else "unknown"
            password = data.get("password", "")
            encoded = (
                await self.auth.verify(password, address)
                if configured
                else await self.auth.setup(password, address)
            )
            if encoded:
                self.auth.sessions.pop(token, None)
                new_token, _ = self.auth.create(encoded)
                response = self.redirect("/")
                self.cookie(response, request, new_token)
                return response
            error = "Password not accepted."
        response = self.render(
            request, session, "login.html", error=error, configured=bool(self.auth.password_hash())
        )
        if error:
            response.status_code = 401
        return response

    async def extension_status(self, extension: str, *, refresh: bool = False) -> str:
        state = self.service.management.states.get(extension, "")
        if state in NEEDS_AUTH and not refresh:
            return status_label(state)
        previous = self.health_cache.get(extension)
        if previous and previous[0] > time.monotonic() and not refresh:
            return previous[1]
        try:
            async with asyncio.timeout(8):
                result = await self.service.execute("health", {"extension": extension})
            native_status = str(result.get("status", "unknown"))
            if native_status == "unsupported":
                item = await self.service.management.extension(extension)
                native_status = str(item.get("status", "loaded"))
            status = status_label(self.service.management.states.get(extension) or native_status)
        except (BridgeError, TimeoutError):
            status = status_label(self.service.management.states.get(extension, "unhealthy"))
        if extension not in self.service.management.states:
            # A remembered failure is not cached: a login or passing check clears it.
            self.health_cache[extension] = (time.monotonic() + 60, status)
        return status

    async def offers(self, registry: str, *, refresh: bool = False) -> list[dict[str, Any]]:
        cached = self.registry_cache.get(registry)
        if cached and cached[0] > time.monotonic() and not refresh:
            return cached[1]
        offers: list[dict[str, Any]] = await self.service.execute(
            "registry.available", {"registry": registry, "refresh": refresh}
        )
        self.registry_cache[registry] = (time.monotonic() + 60, offers)
        return offers

    async def dashboard(self, request: Request, session: Session) -> Response:
        installed = await self.service.execute("list", {})
        statuses = await asyncio.gather(*(self.extension_status(item["id"]) for item in installed))
        links = await self.service.execute("links", {}, private=True)
        for item, status in zip(installed, statuses, strict=True):
            item["status"] = status
            item["link"] = links.get(item["id"], "")
            if item.get("has_download_provider"):
                native = await self.service.management.extension(item["id"])
                chosen = selected_format(native)
                item["effective_default"] = next(
                    (
                        x.get("label") or x["id"]
                        for x in native.get("quality_options", [])
                        if x["id"] == chosen
                    ),
                    chosen,
                )
            item["provides"] = " + ".join(
                kind
                for kind, key in (
                    ("Audio", "has_download_provider"),
                    ("Catalog", "has_metadata_provider"),
                )
                if item.get(key)
            )
            if item.get("has_metadata_provider"):
                try:
                    item["catalog"] = manifest(
                        await self.service.management.extension(item["id"]), self.service.config
                    )["fullCatalog"]
                except BridgeError:
                    item["catalog"] = None
        registries = await self.service.execute("registries.get", {})
        available: list[dict[str, Any]] = []
        errors = []
        for registry in registries:
            try:
                available.extend(
                    dict(item, registry=registry) for item in await self.offers(registry)
                )
            except BridgeError:
                errors.append(registry)
        return self.render(
            request,
            session,
            "dashboard.html",
            installed=installed,
            available=available,
            registries=registries,
            registry_errors=errors,
            links_only=request.url.path == "/links",
            reach="loopback"
            if urlsplit(
                self.service.config.public_url or f"http://127.0.0.1:{self.service.config.port}"
            ).hostname
            in {"127.0.0.1", "::1", "localhost"}
            else "private",
            budget_seconds=max(30, int(self.service.config.stream_wait + 2)),
            stream_wait=self.service.config.stream_wait,
        )

    async def dashboard_post(self, session: Session, data: dict[str, str]) -> Response:
        operation = data.get("operation", "")
        if operation in {"registry.add", "registry.remove"}:
            urls = await self.service.execute("registries.get", {})
            url = data.get("url", "").strip()
            if operation == "registry.add":
                urls = [*urls, url]
            else:
                urls = [value for value in urls if value != url]
            await self.service.execute("registries.save", {"urls": urls})
            self.registry_cache.clear()
        elif operation == "refresh":
            self.service.management.invalidate()
            self.registry_cache.clear()
            self.health_cache.clear()
        elif operation in {"install", "update", "remove", "enable", "disable"}:
            extension = data.get("extension", "")
            params = {"extension": extension}
            if operation in {"install", "update"}:
                params["registry"] = data.get("registry", "")
            await self.service.execute(operation, params)
            self.registry_cache.clear()
            self.health_cache.pop(extension, None)
        else:
            raise BridgeError("unknown_form_operation", 400)
        session.notice = "Saved."
        return self.redirect("/")

    async def pending(self, extension: str) -> dict[str, Any]:
        result = await self.service.execute(
            "pending-auth.show", {"extension": extension}, private=True
        )
        return result if isinstance(result, dict) else {}

    async def remember_result(
        self, session: Session, extension: str, result: Any, *, step: int = 0
    ) -> None:
        form = action_form(result)
        if form:
            if step >= 8:
                raise BridgeError("restart_login_after_eight_steps", 400)
            session.forms[extension] = dict(
                form, step=step, expires=time.monotonic() + 180, token=secrets.token_urlsafe(24)
            )
        else:
            session.forms.pop(extension, None)
        payload = result.get("result", result) if isinstance(result, dict) else {}
        if isinstance(payload, dict):
            urls = [payload.get(key) for key in ("auth_url", "login_url", "url")]
            updates = payload.get("setting_updates") or {}
            if isinstance(updates, dict):
                urls += [
                    value
                    for key, value in updates.items()
                    if "url" in key.lower()
                    and any(word in key.lower() for word in ("auth", "login"))
                ]
            for value in urls:
                if url := browser_url(value):
                    session.auth_urls[extension] = (time.monotonic() + 180, url)
                    break

    async def retry(self, session: Session, extension: str) -> None:
        self.health_cache.pop(extension, None)
        retry = session.retries.pop(extension, None)
        if retry and retry[0] > time.monotonic():
            _, operation, params = retry
            result = await self.service.execute(operation, params, private=True)
            if operation == "action":
                await self.remember_result(session, extension, result)
            session.notice = "Login completed. Retried the operation."
        else:
            status = await self.extension_status(extension, refresh=True)
            session.notice = "Return submitted. " + status + ". Retry playback in Shijhon."

    async def extension(self, request: Request, session: Session, data: dict[str, str]) -> Response:
        extension = request.path_params["extension"]
        item = await self.service.management.extension(extension)
        fields = schema(item)
        values = await self.service.management.call(
            "getExtensionSettings", {"extension_id": extension}
        )
        base = "/extensions/" + quote(extension, safe="")
        if request.url.path.endswith("/open-auth"):
            pending = await self.pending(extension)
            remembered = session.auth_urls.get(extension, (0, ""))
            url = browser_url(pending.get("auth_url"))
            if not url and remembered[0] > time.monotonic():
                url = remembered[1]
            if not url:
                # Some native actions save their browser link as a declared login URL setting.
                url = next(
                    (
                        browser_url(values.get(field["key"]))
                        for field in fields
                        if "url" in field["key"].lower()
                        and any(word in field["key"].lower() for word in ("auth", "login"))
                        and browser_url(values.get(field["key"]))
                    ),
                    "",
                )
            if not url:
                raise BridgeError("no_pending_browser_link", 404)
            return RedirectResponse(
                url, status_code=303, headers=HEADERS | {"Referrer-Policy": "no-referrer"}
            )
        operation = data.get("operation", "")
        form = session.forms.get(extension)
        if form and form["expires"] <= time.monotonic():
            session.forms.pop(extension, None)
            form = None
        if request.method == "POST":
            try:
                if operation == "settings":
                    changed = parse_values(fields, data, stored=values)
                    await self.service.execute(
                        "settings.set", {"extension": extension, "values": changed}
                    )
                    session.notice = "Settings saved."
                elif operation == "action":
                    action = data.get("action", "")
                    if not any(field.get("action") == action for field in fields):
                        raise BridgeError("unknown_declared_action", 400)
                    params = {"extension": extension, "action": action}
                    result = await self.service.execute("action", params, private=True)
                    await self.remember_result(session, extension, result)
                    session.notice = "Action completed. Continue below if login needs another step."
                elif operation in {"continue", "cancel"}:
                    if not form or not hmac.compare_digest(
                        data.get("step_token", "").encode(), form["token"].encode()
                    ):
                        raise BridgeError("action_form_expired", 400)
                    if operation == "cancel":
                        session.forms.pop(extension, None)
                        session.retries.pop(extension, None)
                        if form.get("cancel_action"):
                            await self.service.execute(
                                "action",
                                {"extension": extension, "action": form["cancel_action"]},
                                private=True,
                            )
                        session.notice = "Login cancelled."
                    else:
                        inputs = parse_values(form["fields"], data, transient=True)
                        params = {
                            "extension": extension,
                            "action": form["submit_action"],
                            "arguments": [inputs],
                        }
                        session.forms.pop(extension, None)
                        result = await self.service.execute("action", params, private=True)
                        await self.remember_result(
                            session, extension, result, step=form["step"] + 1
                        )
                        if extension not in session.forms:
                            await self.retry(session, extension)
                elif operation in {"grant", "callback"}:
                    value = data.get("return", "").strip()
                    if not value:
                        raise BridgeError("return_required", 400)
                    await self.service.execute(
                        "pending-auth." + operation,
                        {
                            "extension": extension,
                            "grant" if operation == "grant" else "url": value,
                        },
                        private=True,
                    )
                    if operation == "grant" or not await self.pending(extension):
                        await self.retry(session, extension)
                        session.auth_urls.pop(extension, None)
                    else:
                        session.notice = "Return submitted. Waiting for native login completion."
                elif operation in {"verify", "poll"}:
                    await self.service.management.call("processPendingExtensionCallbacks")
                    pending = await self.pending(extension)
                    if operation == "poll":
                        if not pending and extension in session.retries:
                            await self.retry(session, extension)
                        return JSONResponse({"pending": bool(pending)}, headers=HEADERS)
                    session.notice = "Open the provider page below, then return here."
                elif operation == "health":
                    session.notice = await self.extension_status(extension, refresh=True)
                elif operation == "catalog":
                    if data.get("confirm_identity") != "yes":
                        raise BridgeError("confirm_catalog_identity_change", 400)
                    await self.service.execute(
                        "catalog.save",
                        {
                            "extension": extension,
                            "catalog": {
                                "key": data.get("catalog_key", ""),
                                "region": data.get("catalog_region", ""),
                                "client_artwork": data.get("client_artwork") == "yes",
                            },
                        },
                    )
                    session.notice = "Catalog identity saved."
                else:
                    raise BridgeError("unknown_form_operation", 400)
            except BridgeError as exc:
                if exc.code != "needs_login_or_verification":
                    raise
                # Retain only the operation that needs retry; form credentials remain transient.
                if operation in {"action", "health"}:
                    retry_operation, retry_params = (
                        ("action", {"extension": extension, "action": data["action"]})
                        if operation == "action"
                        else ("health", {"extension": extension})
                    )
                    session.retries[extension] = (
                        time.monotonic() + 180,
                        retry_operation,
                        retry_params,
                    )
                session.notice = "Needs login or verification. Open the provider page below."
            return self.redirect(base)
        pending = await self.pending(extension)
        remembered = session.auth_urls.get(extension, (0, ""))
        has_link = bool(
            browser_url(pending.get("auth_url"))
            or remembered[0] > time.monotonic()
            or any(
                browser_url(values.get(field["key"]))
                for field in fields
                if "url" in field["key"].lower()
                and any(word in field["key"].lower() for word in ("auth", "login"))
            )
        )
        catalog = self.service.config.catalogs.get(extension, CatalogConfig())
        catalog_key = catalog.key
        if item.get("has_metadata_provider") and catalog_key is None:
            try:
                catalog_key = manifest(item, self.service.config)["fullCatalog"]["key"]
            except BridgeError:
                catalog_key = ""
        return self.render(
            request,
            session,
            "extension.html",
            item=item,
            fields=fields_view(fields, values),
            action_form=form,
            action_fields=fields_view(form["fields"], {}, transient=True) if form else [],
            pending=bool(pending),
            has_link=has_link,
            status="Needs login or verification"
            if pending
            else await self.extension_status(extension),
            catalog=catalog,
            catalog_key=catalog_key,
        )
