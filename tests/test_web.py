from __future__ import annotations

import asyncio
import copy
import json
import re
import time
from contextlib import asynccontextmanager

import httpx
import pytest
from conftest import EXT, FakeWorker

from mornye_bridge import config as host_config
from mornye_bridge.config import Config
from mornye_bridge.errors import BridgeError
from mornye_bridge.server import create_app
from mornye_bridge.service import Service
from mornye_bridge.web_auth import COOKIE, set_password
from mornye_bridge.web_forms import action_form, fields_view, parse_values

PASSWORD = "invented-management-password"
REGISTRY = "https://registry.example.test/index.json"
SETTINGS = [
    {"key": "market", "type": "string", "default": "xx", "required": True},
    {"key": "mode", "type": "select", "options": ["gentle", "fast"], "default": "gentle"},
    {"key": "enabled_feature", "type": "boolean", "default": True},
    {"key": "amount", "type": "number", "default": 1.5},
    {"key": "retries", "type": "integer", "default": 2},
    {"key": "private_value", "type": "string", "secret": True, "default": "private-default"},
    {"key": "mediaUserToken", "type": "string", "secret": False},
    {"key": "login", "type": "button", "label": "Log in", "action": "login"},
]


def form(fields, submit="submit_password"):
    return {
        "success": True,
        "action_form": {
            "version": 1,
            "title": "Account login",
            "submit_action": submit,
            "cancel_action": "cancel_login",
            "fields": fields,
        },
    }


class WebWorker(FakeWorker):
    def __init__(self):
        super().__init__()
        self.registry = REGISTRY
        self.installed = True
        self.pending = False
        self.action_needs_auth = False
        self.settings |= {"private_value": "saved-private", "mediaUserToken": "saved-media"}
        self.extension = copy.deepcopy(EXT) | {"settings": copy.deepcopy(SETTINGS)}
        self.extension["quality_options"][0]["settings"] = [
            {"key": "precision", "type": "number", "default": 4},
            {"key": "quality_secret", "type": "string", "secret": True},
        ]

    async def call(self, method, args=None):
        self.calls.append((method, args))
        if method == "getRepoRegistryUrl":
            return self.registry
        if method == "setRepoRegistryUrl":
            self.registry = args["registry_url"]
            return None
        if method == "clearRepoRegistryUrl":
            self.registry = ""
            return None
        if method == "getRepoExtensions":
            return [
                {
                    "id": "invented",
                    "version": "2.0",
                    "display_name": "Invented Music",
                    "is_installed": self.installed,
                    "has_update": self.installed,
                }
            ]
        if method == "getInstalledExtensions":
            return [self.extension | {"enabled": self.enabled}] if self.installed else []
        if method == "checkExtensionHealth":
            return {"status": "online"}
        if method == "getExtensionPendingAuth":
            return (
                {"auth_url": "https://provider.example.test/login?code=private-code"}
                if self.pending
                else ""
            )
        if method == "completeExtensionSessionGrant":
            assert args["grant"] == "private-grant"
            self.pending = False
            self.action_needs_auth = False
            return True
        if method == "queueExtensionCallbackUrl":
            assert "private-code" in args["url"]
            self.pending = False
            self.action_needs_auth = False
            return True
        if method == "invokeExtensionAction":
            action = args["action"]
            if action == "login":
                if self.action_needs_auth:
                    self.pending = True
                    raise BridgeError("needs_login_or_verification")
                return form(
                    [
                        {
                            "key": "password",
                            "type": "password",
                            "required": True,
                            "default": "private-action-default",
                        }
                    ]
                )
            if action == "submit_password":
                assert json.loads(args["arguments_json"])[0] == {"password": "private-password"}
                return {
                    "success": True,
                    "result": form(
                        [{"key": "otp", "type": "otp", "required": True}], "submit_code"
                    ),
                }
            if action == "submit_code":
                assert json.loads(args["arguments_json"])[0] == {"otp": "123456"}
                return {
                    "success": True,
                    "message": "private-password 123456",
                    "setting_updates": {"mediaUserToken": "new-private-media"},
                }
            return {"success": True}
        if method == "downloadRepoExtension":
            return str(self.package)
        if method in {"loadExtensionFromPath", "upgradeExtension"}:
            self.installed = True
            return self.extension
        if method == "removeExtension":
            self.installed = False
            return None
        return await super().call(method, args)


def csrf(response):
    return re.search(r'name="csrf" value="([^"]+)"', response.text)[1]


@asynccontextmanager
async def setup(tmp_path, *, https=False, login=True):
    path = tmp_path / "config.toml"
    path.write_text('library = "unused"\nstate_dir = "state"\n# preserve this comment\n')
    config = Config.load(path)
    set_password(config.state_dir, PASSWORD)
    worker = WebWorker()
    worker.package = config.state_dir / "repo" / "invented.package"
    service = Service(config, worker)
    app = create_app(service)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="https://bridge" if https else "http://bridge",
        ) as client:
            if login:
                response = await client.get("/login")
                response = await client.post(
                    "/login", data={"csrf": csrf(response), "password": PASSWORD}
                )
                assert response.status_code == 303
            yield client, service, worker, app


async def test_password_login_logout_csrf_and_cookie(tmp_path):
    async with setup(tmp_path, https=True, login=False) as (client, service, worker, app):
        assert (await client.get("/")).headers["location"] == "/login"
        page = await client.get("/login")
        anonymous = client.cookies.get(COOKIE)
        assert (await client.post("/login", data={"password": PASSWORD})).status_code == 403
        assert (
            await client.post("/login", data={"csrf": csrf(page), "password": "bad"})
        ).status_code == 401
        response = await client.post("/login", data={"csrf": csrf(page), "password": PASSWORD})
        assert response.status_code == 303 and client.cookies.get(COOKIE) != anonymous
        cookie = response.headers["set-cookie"].lower()
        assert all(flag in cookie for flag in ["secure", "httponly", "samesite=strict"])
        page = await client.get("/")
        assert (
            await client.post("/", data={"csrf": "wrong", "operation": "disable"})
        ).status_code == 403
        assert (
            await client.post(
                "/",
                headers={"Origin": "https://evil.test"},
                data={"csrf": csrf(page), "operation": "refresh"},
            )
        ).status_code == 403
        response = await client.post("/logout", data={"csrf": csrf(page)})
        assert response.status_code == 303 and (await client.get("/links")).status_code == 303
        assert not app.state.web.auth.sessions.get(anonymous)


async def test_login_rate_limit_and_password_revocation(tmp_path):
    async with setup(tmp_path, login=False) as (client, service, worker, app):
        page = await client.get("/login")
        for _ in range(5):
            assert (
                await client.post("/login", data={"csrf": csrf(page), "password": "bad"})
            ).status_code == 401
        response = await client.post("/login", data={"csrf": csrf(page), "password": PASSWORD})
        assert response.status_code == 429 and response.headers["retry-after"] == "60"
        app.state.web.auth.attempts.clear()
        await client.post("/login", data={"csrf": csrf(page), "password": PASSWORD})
        set_password(service.config.state_dir, PASSWORD + "new")
        assert (await client.get("/links")).headers["location"] == "/login"
        assert (service.config.state_dir / "password.argon2").stat().st_mode & 0o777 == 0o600
        assert PASSWORD not in (service.config.state_dir / "password.argon2").read_text()


@pytest.mark.parametrize(
    "path", ["/", "/links", "/extensions/invented", "/assets/web.css", "/assets/web.js"]
)
async def test_pages_render_without_stored_secrets(tmp_path, path):
    async with setup(tmp_path) as (client, service, worker, app):
        response = await client.get(path)
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
        assert response.headers["referrer-policy"] == "same-origin"
        for private in (
            "saved-private",
            "saved-media",
            "private-default",
            "private-action-default",
        ):
            assert private not in response.text
        if path == "/links":
            assert service.access_token in response.text
            assert "budget_seconds" in response.text and "loopback" in response.text
        if path == "/extensions/invented":
            assert service.access_token not in response.text
            assert all(
                'name="value:' + key + '"' in response.text
                for key in (
                    "market",
                    "mode",
                    "enabled_feature",
                    "amount",
                    "retries",
                    "precision",
                    "quality_secret",
                )
            )
            assert 'value="xx"' in response.text and ">gentle</option>" in response.text


async def test_settings_keep_clear_and_validation(tmp_path):
    async with setup(tmp_path) as (client, service, worker, app):
        page = await client.get("/extensions/invented")
        data = {
            "csrf": csrf(page),
            "operation": "settings",
            "value:market": "zz",
            "value:mode": "fast",
            "value:amount": "2.5",
            "value:retries": "3",
            "value:enabled_feature": "true",
            "value:private_value": "",
            "value:mediaUserToken": "",
            "value:precision": "8",
        }
        response = await client.post("/extensions/invented", data=data)
        assert response.status_code == 303
        assert worker.settings["private_value"] == "saved-private"
        assert worker.settings["mediaUserToken"] == "saved-media"
        assert worker.settings["amount"] == 2.5 and worker.settings["enabled_feature"] is True
        data |= {"value:private_value": "replacement-private", "clear:mediaUserToken": "yes"}
        await client.post("/extensions/invented", data=data)
        assert worker.settings["private_value"] == "replacement-private"
        assert worker.settings["mediaUserToken"] == ""
        assert "replacement-private" not in (await client.get("/extensions/invented")).text
        for invalid in ({"value:amount": "nan"}, {"value:mode": "invalid"}, {"value:market": ""}):
            response = await client.post("/extensions/invented", data=data | invalid)
            assert response.status_code == 400 and "replacement-private" not in response.text


async def test_registry_lifecycle_and_catalog_persistence(tmp_path):
    async with setup(tmp_path) as (client, service, worker, app):
        token = csrf(await client.get("/"))

        async def post(operation, **values):
            response = await client.post(
                "/", data={"csrf": token, "operation": operation, "extension": "invented", **values}
            )
            assert response.status_code == 303

        second = "https://second.example.test/registry.json"
        await post("registry.add", url=second)
        assert Config.load(tmp_path / "config.toml").registries == [REGISTRY, second]
        assert "# preserve this comment" in (tmp_path / "config.toml").read_text()
        await client.get("/")
        assert worker.registry == REGISTRY
        for operation in ("update", "disable", "enable", "remove", "install"):
            await post(operation, registry=second)
        assert worker.installed and worker.enabled
        await post("registry.remove", url=REGISTRY)
        await post("registry.remove", url=second)
        assert await service.execute("registries.get", {}) == []
        page = await client.get("/extensions/invented")
        assert "Give apps direct catalog artwork addresses" in page.text
        assert "Shijhon always loads album covers itself" in page.text
        assert "region or storefront setting, use the same value here" in page.text
        data = {
            "csrf": csrf(page),
            "operation": "catalog",
            "catalog_key": "inventedmusic",
            "catalog_region": "xx",
            "client_artwork": "yes",
        }
        assert (await client.post("/extensions/invented", data=data)).status_code == 400
        data["confirm_identity"] = "yes"
        assert (await client.post("/extensions/invented", data=data)).status_code == 303
        assert Config.load(tmp_path / "config.toml").catalogs["invented"].key == "inventedmusic"


@pytest.mark.parametrize(
    "operation,value",
    [
        ("grant", "private-grant"),
        ("callback", "mornye://callback?code=private-code&state=private-state"),
    ],
)
async def test_pending_auth_submission_and_retry(tmp_path, operation, value):
    async with setup(tmp_path) as (client, service, worker, app):
        worker.action_needs_auth = True
        page = await client.get("/extensions/invented")
        token = csrf(page)
        await client.post(
            "/extensions/invented", data={"csrf": token, "operation": "action", "action": "login"}
        )
        page = await client.get("/extensions/invented")
        assert "data-pending" in page.text and "Open provider login" in page.text
        assert "private-code" not in page.text
        opened = await client.get("/extensions/invented/open-auth")
        assert opened.status_code == 303 and "provider.example.test" in opened.headers["location"]
        response = await client.post(
            "/extensions/invented", data={"csrf": token, "operation": operation, "return": value}
        )
        assert response.status_code == 303 and value not in response.text
        page = await client.get("/extensions/invented")
        assert "Account login" in page.text and "private-code" not in page.text
        assert not app.state.web.auth.session(client.cookies.get(COOKIE)).retries


async def test_multistep_action_form_and_cancel(tmp_path):
    async with setup(tmp_path) as (client, service, worker, app):
        page = await client.get("/extensions/invented")
        token = csrf(page)

        async def submit(operation, **values):
            if operation in {"continue", "cancel"}:
                page = await client.get("/extensions/invented")
                values["step_token"] = re.search(r'name="step_token" value="([^"]+)"', page.text)[1]
            response = await client.post(
                "/extensions/invented", data={"csrf": token, "operation": operation, **values}
            )
            assert response.status_code == 303
            return await client.get("/extensions/invented")

        page = await submit("action", action="login")
        assert 'name="value:password"' in page.text
        assert "private-action-default" not in page.text
        previous_step = re.search(r'name="step_token" value="([^"]+)"', page.text)[1]
        page = await submit("continue", **{"value:password": "private-password"})
        rejected = await client.post(
            "/extensions/invented",
            data={
                "csrf": token,
                "operation": "continue",
                "step_token": previous_step,
                "value:otp": "123456",
            },
        )
        assert rejected.status_code == 400 and "123456" not in rejected.text
        assert 'name="value:otp"' in page.text and "private-password" not in page.text
        page = await submit("continue", **{"value:otp": "123456"})
        assert "123456" not in page.text and "new-private-media" not in page.text
        assert worker.settings["mediaUserToken"] == "new-private-media"
        assert "password" not in worker.settings and "otp" not in worker.settings
        await submit("action", action="login")
        await submit("cancel")
        assert any(
            args.get("action") == "cancel_login"
            for method, args in worker.calls
            if method == "invokeExtensionAction"
        )
        assert (
            await client.post("/extensions/invented", data={"csrf": token, "operation": "continue"})
        ).status_code == 400


def test_action_form_contract_and_type_validation():
    fields = [
        {"key": "choice", "type": "select", "options": ["one", "two"], "default": "two"},
        {"key": "amount", "type": "number"},
        {"key": "name", "type": "string"},
    ]
    assert action_form(form(fields))["fields"] == fields
    assert fields_view(fields, {}, transient=True)[0]["value"] == "two"
    assert parse_values(
        fields, {"value:choice": "one", "value:amount": "-1.5", "value:name": "x"}, transient=True
    ) == {"choice": "one", "amount": -1.5, "name": "x"}
    invalid = [
        form([]),
        form(fields, "bad-action"),
        form([fields[0], fields[0]]),
        form([{"key": "x", "type": "select", "options": ["same", "same"]}]),
    ]
    for value in invalid:
        with pytest.raises(BridgeError):
            action_form(value)


async def test_defaults_host_version_and_session_expiry(tmp_path, monkeypatch):
    async with setup(tmp_path) as (client, service, worker, app):
        assert service.config.stream_wait == 8
        monkeypatch.setattr(host_config, "HOST_VERSION", "test-host")
        assert (await service.execute("check", {}))["host_version"] == "test-host"
        session = app.state.web.auth.session(client.cookies.get(COOKIE))
        session.expires = time.monotonic() - 1
        assert (await client.get("/")).status_code == 303


async def test_no_registry_is_added_automatically(tmp_path):
    async with setup(tmp_path) as (client, service, worker, app):
        worker.registry = ""
        page = await client.get("/")
        registries = page.text.split('<section id="registries">', 1)[1].split("</section>", 1)[0]
        assert "Registry address" in registries
        assert "Install only from addresses and publishers you trust." in registries
        assert "suggestion" not in registries.lower()
        assert "official" not in registries.lower()
        assert "registry.json" not in registries
        assert "Mornye-Extension" not in registries
        assert not any(method == "setRepoRegistryUrl" for method, args in worker.calls)
        assert (
            await client.post(
                "/",
                data={
                    "csrf": csrf(page),
                    "operation": "registry.add",
                    "url": "http://insecure.test/registry.json",
                },
            )
        ).status_code == 400


async def test_unauthenticated_pages_never_expose_links(tmp_path):
    async with setup(tmp_path, login=False) as (client, service, worker, app):
        for path in ("/", "/links", "/extensions/invented", "/extensions/invented/open-auth"):
            response = await client.get(path)
            assert response.status_code == 303 and service.access_token not in response.text
        assert (await client.post("/logout", data={"operation": "logout"})).status_code == 403


async def test_pending_poll_expiry_and_rejected_callback(tmp_path):
    async with setup(tmp_path) as (client, service, worker, app):
        worker.pending = True
        page = await client.get("/extensions/invented")
        token = csrf(page)
        response = await client.post(
            "/extensions/invented", data={"csrf": token, "operation": "poll"}
        )
        assert response.json() == {"pending": True}
        assert "private-code" not in response.text
        worker.pending = False
        response = await client.post(
            "/extensions/invented", data={"csrf": token, "operation": "poll"}
        )
        assert response.json() == {"pending": False}
        session = app.state.web.auth.session(client.cookies.get(COOKIE))
        await app.state.web.remember_result(
            session, "invented", form([{"key": "password", "type": "password"}])
        )
        session.forms["invented"]["expires"] = time.monotonic() - 1
        response = await client.post(
            "/extensions/invented",
            data={"csrf": token, "operation": "continue", "value:password": "private-password"},
        )
        assert response.status_code == 400 and "private-password" not in response.text
        with pytest.raises(BridgeError, match="restart_login_after_eight_steps"):
            await app.state.web.remember_result(
                session, "invented", form([{"key": "password", "type": "password"}]), step=8
            )

        async def reject(method, args=None):
            return False

        worker.call = reject
        with pytest.raises(BridgeError, match="callback_not_accepted"):
            await service.execute(
                "pending-auth.callback", {"url": "mornye://callback?code=private"}
            )


async def test_form_errors_and_new_password_command(tmp_path, monkeypatch, capsys):
    import sys

    from mornye_bridge.cli import main

    path = tmp_path / "config.toml"
    path.write_text('library = "unused"\nstate_dir = "state"\n')
    monkeypatch.setattr(sys, "argv", ["mornye-bridge", "--config", str(path), "set-password"])
    monkeypatch.setattr("getpass.getpass", lambda prompt: PASSWORD)
    main()
    assert "password saved" in capsys.readouterr().out
    assert (tmp_path / "state/password.argon2").exists()
    async with setup(tmp_path) as (client, service, worker, app):
        await client.get("/")
        for path in ("/", "/logout", "/extensions/invented"):
            assert (await client.post(path, data={"csrf": "wrong"})).status_code == 403
        response = await client.post(
            "/",
            content="csrf=x&csrf=y",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        assert response.status_code == 400
        response = await client.post(
            "/",
            content="x=" + "x" * 65537,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        assert response.status_code == 413
        assert (await client.get("/assets/not-allowed")).status_code == 404


def test_write_only_zero_and_form_legacy_alias():
    fields = [{"key": "private_count", "type": "number", "secret": True, "required": True}]
    assert fields_view(fields, {"private_count": 0})[0]["value"] == 0
    assert fields_view(fields, {"private_count": 0})[0]["secret"] is False
    assert parse_values(fields, {"value:private_count": "0"}, stored={"private_count": 0}) == {
        "private_count": 0
    }
    raw = form([{"key": "name", "type": "string"}])["action_form"]
    assert action_form({"success": True, "byoa_form": raw})["submit_action"] == "submit_password"


async def test_unsupported_native_health_and_empty_settings(tmp_path):
    async with setup(tmp_path) as (client, service, worker, app):
        original = worker.call

        async def unsupported(method, args=None):
            if method == "checkExtensionHealth":
                return {"status": "unsupported", "checks": []}
            return await original(method, args)

        worker.call = unsupported
        worker.extension["settings"] = []
        worker.extension["quality_options"] = []
        page = await client.get("/extensions/invented")
        assert "Ready" in page.text and "Unhealthy" not in page.text
        assert "no editable settings" in page.text and "Save settings" not in page.text


async def test_registry_reads_serialize_and_auth_inspection_preserves_failure(tmp_path):
    async with setup(tmp_path) as (client, service, worker, app):
        second = "https://second.example.test/registry.json"
        service.config.persist(registries=[REGISTRY, second])
        entered, release = asyncio.Event(), asyncio.Event()
        original = worker.call

        async def blocked(method, args=None):
            if method == "getRepoExtensions":
                if worker.registry == second:
                    entered.set()
                    await release.wait()
                return [{"id": "invented", "display_name": worker.registry}]
            return await original(method, args)

        worker.call = blocked
        web_read = asyncio.create_task(service.execute("registry.available", {"registry": second}))
        await asyncio.wait_for(entered.wait(), 1)
        cli_read = asyncio.create_task(service.execute("available", {}))
        await asyncio.sleep(0)
        assert not cli_read.done()
        release.set()
        assert (await web_read)[0]["display_name"] == second
        assert (await cli_read)[0]["display_name"] == REGISTRY
        service.management.states["invented"] = "needs_login_or_verification"
        await service.execute("pending-auth.show", {"extension": "invented"})
        assert service.management.states["invented"] == "needs_login_or_verification"
        assert await app.state.web.extension_status("invented") == "Needs login or verification"
        # A passing health check clears the remembered failure.
        assert await app.state.web.extension_status("invented", refresh=True) == "Ready"
        assert "invented" not in service.management.states


@pytest.mark.parametrize(
    "password, accepted",
    [("", False), ("x", True), ("é", True), ("x" * 1024, True), ("x" * 1025, False)],
)
async def test_cli_password_bounds_and_login(tmp_path, monkeypatch, capsys, password, accepted):
    import sys

    from mornye_bridge.cli import main
    from mornye_bridge.web_auth import BrowserAuth

    path = tmp_path / "config.toml"
    path.write_text('library = "unused"\nstate_dir = "state"\n')
    monkeypatch.setattr(sys, "argv", ["mornye-bridge", "--config", str(path), "set-password"])
    monkeypatch.setattr("getpass.getpass", lambda prompt: password)
    stored = tmp_path / "state/password.argon2"
    if accepted:
        main()
        assert "password saved" in capsys.readouterr().out
        assert stored.stat().st_mode & 0o777 == 0o600
        auth = BrowserAuth(stored.parent)
        assert await auth.verify(password, "127.0.0.1")
        assert not await auth.verify(password + "wrong", "127.0.0.1")
    else:
        with pytest.raises(SystemExit) as failure:
            main()
        assert failure.value.code == 1
        assert "password_must_be_nonempty_and_at_most_1024_characters" in capsys.readouterr().err
        assert not stored.exists()
