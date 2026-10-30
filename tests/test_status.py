from __future__ import annotations

from conftest import FakeWorker

from mornye_bridge.service import Service


async def test_completed_login_clears_the_remembered_status(config):
    class Login(FakeWorker):
        pending = True

        async def call(self, method, args=None):
            if method == "getExtensionPendingAuth":
                self.calls.append((method, args))
                return {"auth_url": "https://login.example.test/x"} if self.pending else {}
            if method == "checkExtensionHealth":
                return {"status": "degraded"}
            return await super().call(method, args)

    worker = Login()
    service = Service(config, worker)
    await service.start()
    try:
        states = service.management.states
        await service.execute("pending-auth.show", {"extension": "invented"})
        assert states["invented"] == "needs_login_or_verification"
        # A failing health check keeps it; an answered challenge clears it.
        await service.execute("health", {"extension": "invented"})
        assert states["invented"] == "needs_login_or_verification"
        await service.execute("pending-auth.show", {"extension": "invented"})
        assert states["invented"] == "needs_login_or_verification"
        worker.pending = False
        await service.execute("pending-auth.show", {"extension": "invented"})
        assert "invented" not in states
        # No challenge seen: a missing challenge proves nothing.
        states["invented"] = "needs_login_or_verification"
        await service.execute("pending-auth.show", {"extension": "invented"})
        assert states["invented"] == "needs_login_or_verification"
    finally:
        await service.close()
