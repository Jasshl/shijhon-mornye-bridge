from __future__ import annotations

import asyncio
import re
from contextlib import asynccontextmanager

import httpx
import pytest
from conftest import FakeWorker
from pydantic import ValidationError
from starlette.responses import JSONResponse
from starlette.routing import Route
from test_web import WebWorker

from mornye_bridge.config import Config
from mornye_bridge.server import create_app
from mornye_bridge.service import Service
from mornye_bridge.web_auth import COOKIE, set_password


def csrf(response):
    return re.search(r'name="csrf" value="([^"]+)"', response.text)[1]


@asynccontextmanager
async def running(config, *, password=None, peer="127.0.0.1", url="http://bridge.test"):
    if password is not None:
        set_password(config.state_dir, password)
    service = Service(config, WebWorker())
    app = create_app(service, surface="management")

    async def backend(scope, receive, send):
        # The browser sees `url`, while the proxy connects over HTTP to another Host.
        scope["scheme"] = "http"
        scope["headers"] = [
            (name, b"backend.internal:4760" if name == b"host" else value)
            for name, value in scope["headers"]
        ]
        await app(scope, receive, send)

    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=backend, client=(peer, 12345)), base_url=url
        ) as client:
            yield client, app.state.web.auth


async def test_first_run_claim_privacy_and_reset(config, capsys):
    # Leftovers from earlier private builds do not participate in setup.
    config.state_dir.mkdir(parents=True, exist_ok=True)
    code_file = config.state_dir / "setup.code"
    code_file.write_text("ignored-old-value")
    async with running(config) as (client, auth):
        assert capsys.readouterr().err == ""
        assert (await client.get("/")).headers["location"] == "/login"
        page = await client.get("/login")
        assert "Choose a password" in page.text and "whoever reaches this page first" in page.text
        assert "setup.code" not in page.text and 'name="setup_code"' not in page.text
        anonymous = client.cookies.get(COOKIE)
        data = {"csrf": csrf(page), "password": "x"}
        for changes, headers in (({"csrf": "bad"}, {}), ({}, {"Origin": "https://evil.test"})):
            response = await client.post("/login", data=data | changes, headers=headers)
            assert response.status_code == 403
        for password in ("", "x" * 1025):
            response = await client.post("/login", data=data | {"password": password})
            assert response.status_code == 400
            assert not auth.password_hash()
        response = await client.post("/login", data=data)
        assert response.status_code == 303 and response.headers["location"] == "/"
        assert anonymous not in auth.sessions and client.cookies.get(COOKIE) != anonymous
        assert auth.session(client.cookies.get(COOKIE)).authenticated
        assert (config.state_dir / "password.argon2").stat().st_mode & 0o777 == 0o600
        assert (await client.get("/login")).status_code == 303
        assert (await client.get("/links")).status_code == 200
        assert capsys.readouterr().err == ""
        session = auth.session(client.cookies.get(COOKIE))
        await client.post("/logout", data={"csrf": session.csrf})
        login = await client.get("/login")
        assert "Choose a password" not in login.text
        assert (await client.post("/login", data=data)).status_code == 403
        response = await client.post("/login", data={"csrf": csrf(login), "password": "x"})
        assert response.status_code == 303
        auth.attempts.clear()
        set_password(config.state_dir, "reset")
        assert (await client.get("/links")).headers["location"] == "/login"
        page = await client.get("/login")
        assert (
            await client.post("/login", data={"csrf": csrf(page), "password": "reset"})
        ).status_code == 303


async def test_setup_rate_limits_and_expiry(config, monkeypatch):
    async with running(config) as (client, auth):
        page = await client.get("/login")
        data = {"csrf": csrf(page), "password": ""}
        for _ in range(5):
            assert (await client.post("/login", data=data)).status_code == 400
        response = await client.post("/login", data=data | {"password": "x"})
        assert response.status_code == 429 and response.headers["retry-after"] == "60"
        now = auth.attempts[-1][0]
        monkeypatch.setattr("mornye_bridge.web_auth.time.monotonic", lambda: now + 61)
        assert (await client.post("/login", data=data | {"password": "x"})).status_code == 303


async def test_restart_invalidates_sessions_without_password(config, capsys):
    async with running(config) as (client, auth):
        page = await client.get("/login")
        old_cookie, old_csrf = client.cookies.get(COOKIE), csrf(page)
        assert not (config.state_dir / "setup.code").exists()
    async with running(config) as (client, auth):
        assert capsys.readouterr().err == ""
        client.cookies.set(COOKIE, old_cookie)
        response = await client.post("/login", data={"csrf": old_csrf, "password": "x"})
        assert response.status_code == 403
        client.cookies.clear()
        page = await client.get("/login")
        assert (
            await client.post("/login", data={"csrf": csrf(page), "password": "x"})
        ).status_code == 303
    async with running(config) as (client, auth):
        assert capsys.readouterr().err == ""
        assert "Choose a password" not in (await client.get("/login")).text
        assert not await auth.setup("replacement", "127.0.0.1")


async def test_cli_password_invalidates_unclaimed_setup(config, monkeypatch, capsys):
    from mornye_bridge.cli import main

    path = config.state_dir.parent / "config.toml"
    path.write_text('library = "unused"\nstate_dir = "state"\n')
    async with running(config) as (client, auth):
        page = await client.get("/login")
        monkeypatch.setattr("sys.argv", ["mornye-bridge", "--config", str(path), "set-password"])
        monkeypatch.setattr("getpass.getpass", lambda prompt: "cli-reset")
        main()
        assert "password saved" in capsys.readouterr().out
        assert not (config.state_dir / "setup.code").exists()
        assert not await auth.setup("wrong", "127.0.0.1")
        assert "Choose a password" not in (await client.get("/login")).text
        response = await client.post("/login", data={"csrf": csrf(page), "password": "cli-reset"})
        assert response.status_code == 303


async def test_concurrent_claim_does_not_overwrite_cli_reset(config, monkeypatch):
    import threading

    import mornye_bridge.web_auth as module

    async with running(config) as (client, auth):
        original = module.set_password
        entered, release = threading.Event(), threading.Event()

        def delayed(state_dir, password, *, replace=True):
            entered.set()
            assert release.wait(5)
            return original(state_dir, password, replace=replace)

        monkeypatch.setattr(module, "set_password", delayed)
        task = asyncio.create_task(auth.setup("browser", "127.0.0.1"))
        assert await asyncio.to_thread(entered.wait, 5)
        try:
            original(config.state_dir, "cli")
        finally:
            release.set()
        assert await task == ""
        assert await auth.verify("cli", "127.0.0.1")
        assert not await auth.verify("browser", "127.0.0.1")


@pytest.mark.parametrize("password", ["é", "x" * 1024])
async def test_web_setup_password_boundaries(config, password):
    async with running(config) as (client, auth):
        page = await client.get("/login")
        response = await client.post("/login", data={"csrf": csrf(page), "password": password})
        assert response.status_code == 303
        assert await auth.verify(password, "127.0.0.1")


@pytest.mark.parametrize(
    "public", ["", "http://manage.example.test", "https://manage.example.test"]
)
@pytest.mark.parametrize("trusted", [False, True])
async def test_proxy_origin_secure_cookie_and_csrf(config, public, trusted):
    config.management_public_url = public
    config.management_trusted_proxies = ["127.0.0.1"] if trusted else []
    # HTTPS on the outside; test transport emulates plain HTTP proxy-to-backend traffic.
    async with running(config, password="x", url="https://manage.example.test") as (client, auth):
        client.headers.update({"X-Forwarded-Proto": "https", "X-Forwarded-Host": "evil.test"})
        redirect = await client.get("/")
        assert redirect.headers["location"] == public + "/login"
        page = await client.get("/login")
        assert ("; Secure" in page.headers["set-cookie"]) == (
            trusted or public.startswith("https:")
        )
        expected = public or ("https" if trusted else "http") + "://backend.internal:4760"
        data = {"csrf": csrf(page), "password": "x"}
        for invalid in {"https://evil.test", "https://evil.test, " + expected, "null"}:
            assert (
                await client.post("/login", data=data, headers={"Origin": invalid})
            ).status_code == 403
        if public:
            assert (
                await client.post(
                    "/login", data=data, headers={"Origin": "http://backend.internal:4760"}
                )
            ).status_code == 403
        response = await client.post("/login", data=data, headers={"Origin": expected})
        assert response.status_code == 303 and response.headers["location"] == public + "/"
        page = await client.get("/links")
        assert page.status_code == 200 and 'href="/assets/web.css"' in page.text
        assert 'action="/logout"' in page.text
        token = auth.session(client.cookies.get(COOKIE)).csrf
        response = await client.post("/logout", data={"csrf": token}, headers={"Origin": expected})
        assert response.headers["location"] == public + "/login"
        assert ("; Secure" in response.headers["set-cookie"]) == (
            trusted or public.startswith("https:")
        )


@pytest.mark.parametrize("trusted", [False, True])
async def test_forwarded_client_limits_are_only_trusted_from_configured_proxy(config, trusted):
    config.management_trusted_proxies = ["127.0.0.1"]
    peer = "127.0.0.1" if trusted else "192.0.2.20"
    async with running(config, password="correct", peer=peer, url="https://bridge.test") as (
        client,
        auth,
    ):
        page = await client.get("/login", headers={"X-Forwarded-Proto": "https"})
        assert ("; Secure" in page.headers["set-cookie"]) == trusted
        data = {"csrf": csrf(page), "password": "x"}
        for index in range(30 if trusted else 5):
            response = await client.post(
                "/login", data=data, headers={"X-Forwarded-For": f"192.0.2.{index + 30}"}
            )
            assert response.status_code == 401
        response = await client.post(
            "/login", data=data, headers={"X-Forwarded-For": "198.51.100.1"}
        )
        assert response.status_code == 429
        assert len(auth.attempts) == (30 if trusted else 5)
        assert len({address for _, address in auth.attempts}) == (30 if trusted else 1)


async def test_separate_api_does_not_apply_management_proxy_headers(config, capsys):
    config.management_trusted_proxies = ["127.0.0.1"]
    service = Service(config, FakeWorker())
    await service.start()
    try:
        api = create_app(service, surface="api", manage_lifespan=False)

        async def inspect(request):
            return JSONResponse({"scheme": request.url.scheme, "client": request.client.host})

        api.router.routes.append(Route("/inspect", inspect))
        async with api.router.lifespan_context(api):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=api), base_url="http://bridge"
            ) as client:
                response = await client.get(
                    "/inspect",
                    headers={"X-Forwarded-Proto": "https", "X-Forwarded-For": "192.0.2.1"},
                )
                assert response.json() == {"scheme": "http", "client": "127.0.0.1"}
                assert (await client.get("/login")).status_code == 404
                assert (
                    await client.get(f"/addons/{service.access_token}/invented/manifest.json")
                ).status_code == 200
        assert not (config.state_dir / "setup.code").exists() and capsys.readouterr().err == ""
    finally:
        await service.close()


@pytest.mark.parametrize(
    "url",
    [
        "https://example.test/prefix",
        "https://u:p@example.test",
        "ftp://example.test",
        "https://example.test/?",
        "https://example.test/#",
        "https://example.test:99999",
        "https://example.test:0",
        " https://example.test",
        "https://example.test/\n",
        "https://example.test/\\x",
    ],
)
def test_public_management_origin_rejects_non_origins(url):
    with pytest.raises(ValidationError):
        Config(library="unused", management_public_url=url)


def test_proxy_config_validation():
    config = Config(
        library="unused",
        management_public_url="https://MANAGE.example.test:443/",
        management_trusted_proxies=["127.0.0.1", "::1"],
    )
    assert config.management_public_url == "https://manage.example.test"
    assert config.host == "127.0.0.1" and config.management_trusted_proxies == ["127.0.0.1", "::1"]
    for address in ("*", "proxy.test", "127.0.0.0/8", "not-an-ip"):
        with pytest.raises(ValidationError):
            Config(library="unused", management_trusted_proxies=[address])


async def test_trusted_proxy_chain_uses_nearest_untrusted_client(config):
    config.management_trusted_proxies = ["127.0.0.1", "::1"]
    async with running(config, password="correct") as (client, auth):
        page = await client.get("/login")
        for index in range(5):
            response = await client.post(
                "/login",
                data={"csrf": csrf(page), "password": "x"},
                headers={"X-Forwarded-For": f"192.0.2.{index}, 198.51.100.1, ::1"},
            )
            assert response.status_code == 401
        assert {address for _, address in auth.attempts} == {"198.51.100.1"}
        response = await client.post(
            "/login",
            data={"csrf": csrf(page), "password": "x"},
            headers={"X-Forwarded-For": "192.0.2.100, 198.51.100.1, ::1"},
        )
        assert response.status_code == 429


async def test_reset_during_login_does_not_authorize_replacement_hash(config, monkeypatch):
    async with running(config, password="before") as (client, auth):
        page = await client.get("/login")
        original = auth.verify

        async def reset_after_verify(password, address):
            encoded = await original(password, address)
            set_password(config.state_dir, "after")
            return encoded

        monkeypatch.setattr(auth, "verify", reset_after_verify)
        response = await client.post("/login", data={"csrf": csrf(page), "password": "before"})
        assert response.status_code == 303
        assert (await client.get("/links")).headers["location"] == "/login"
        monkeypatch.setattr(auth, "verify", original)
        page = await client.get("/login")
        response = await client.post("/login", data={"csrf": csrf(page), "password": "after"})
        assert response.status_code == 303 and (await client.get("/links")).status_code == 200


def test_serve_disables_implicit_uvicorn_proxy_trust(tmp_path, monkeypatch):
    from mornye_bridge.cli import main

    path = tmp_path / "config.toml"
    path.write_text('library = "unused"\nstate_dir = "state"\n')
    monkeypatch.setattr("sys.argv", ["mornye-bridge", "--config", str(path), "serve"])
    monkeypatch.setenv("FORWARDED_ALLOW_IPS", "*")
    options = {}

    def serve(app, **kwargs):
        options.update(kwargs)

    monkeypatch.setattr("mornye_bridge.cli.uvicorn.run", serve)
    main()
    assert options["proxy_headers"] is False
    assert options["host"] == "127.0.0.1" and options["access_log"] is False


async def test_two_first_visitors_cannot_replace_password(config):
    async with running(config) as (client, auth):
        page = await client.get("/login")
        csrf_token = csrf(page)
        first, second = await asyncio.gather(
            auth.setup("first", "192.0.2.1"), auth.setup("second", "192.0.2.2")
        )
        assert bool(first) != bool(second)
        assert await auth.verify("first" if first else "second", "127.0.0.1")
        # The losing visitor's pre-claim CSRF/session is revoked too.
        assert (
            await client.post("/login", data={"csrf": csrf_token, "password": "other"})
        ).status_code == 403
