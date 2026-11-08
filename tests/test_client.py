from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch

import httpx

from src.grubhub_mcp import client as module


def session_data(access="old-access", refresh="old-refresh", diner="diner-123"):
    return {
        "session_handle": {"access_token": access, "refresh_token": refresh},
        "credential": {"ud_id": diner},
    }


class SessionTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        for name, value in (("_SESSION_DIR", self.root), ("_SESSION_FILE", self.root / "session.json")):
            patcher = patch.object(module, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.session = module.GrubhubSession()
        self.session.set_authenticated(session_data())

    def test_partial_refresh_preserves_identity_and_refresh_token_after_restart(self):
        self.session.update_tokens({"session_handle": {"access_token": "new-access"}})
        reloaded = module.GrubhubSession()
        self.assertEqual(reloaded.auth_token, "new-access")
        self.assertEqual(reloaded.refresh_token, "old-refresh")
        self.assertEqual(reloaded.diner_udid, "diner-123")
        self.assertTrue(reloaded.is_authenticated)

    def test_rotation_is_persisted_and_null_refresh_does_not_erase_it(self):
        self.session.update_tokens({"session_handle": {"access_token": "new-access", "refresh_token": "new-refresh"}})
        self.session.update_tokens({"credential": {"ud_id": "diner-123"}, "session_handle": {"refresh_token": None}})
        reloaded = module.GrubhubSession()
        self.assertEqual(reloaded.refresh_token, "new-refresh")
        assert reloaded.session_handle is not None
        self.assertEqual(reloaded.session_handle["refresh_token"], "new-refresh")
        self.assertEqual(reloaded.auth_token, "new-access")

    def test_explicit_login_does_not_inherit_old_account(self):
        self.session.set_authenticated({"session_handle": {"access_token": "other-user"}})
        self.assertIsNone(self.session.diner_udid)
        self.assertIsNone(self.session.refresh_token)

    def test_anonymous_session_clears_account_and_challenge(self):
        self.session.csrf_token = "challenge"
        self.session.set_anonymous(session_data(access="anon"))
        self.assertFalse(self.session.is_authenticated)
        self.assertIsNone(self.session.diner_udid)
        self.assertIsNone(self.session.csrf_token)

    def test_expiry_is_in_minutes_not_seconds(self):
        now = datetime.now(timezone.utc)
        assert self.session.session_handle is not None
        self.session.session_handle.update({"expire_in": 60, "token_created": (now - timedelta(minutes=30)).isoformat()})
        self.assertFalse(self.session.access_token_expiring())
        self.session.session_handle["token_created"] = (now - timedelta(minutes=61)).isoformat()
        self.assertTrue(self.session.access_token_expiring())

    def test_epoch_millis_expiry_and_early_refresh_margin(self):
        now = datetime.now(timezone.utc)
        assert self.session.session_handle is not None
        self.session.session_handle["token_expire_time"] = (now + timedelta(seconds=20)).timestamp() * 1000
        self.assertTrue(self.session.access_token_expiring())
        self.session.session_handle["token_expire_time"] = (now + timedelta(minutes=10)).timestamp() * 1000
        self.assertFalse(self.session.access_token_expiring())

    def test_creation_epoch_millis_with_minute_lifetime(self):
        old = datetime.now(timezone.utc) - timedelta(hours=2)
        self.session.session_handle = {"token_created_time": old.timestamp() * 1000, "expire_in": 60}
        self.assertTrue(self.session.access_token_expiring())

    def test_missing_or_malformed_expiry_uses_401_fallback(self):
        for handle in ({}, {"token_created": "invalid", "expire_in": 60}, {"token_created": None, "expire_in": "bad"}):
            with self.subTest(handle=handle):
                self.session.session_handle = handle
                self.assertFalse(self.session.access_token_expiring())

    def test_rotated_token_does_not_inherit_old_expiry(self):
        assert self.session.session_handle is not None
        self.session.session_handle.update({"token_created": "old", "token_expire_time": 1})
        self.session.update_tokens({"auth_token": "new-access"})
        self.assertNotIn("token_created", self.session.session_handle)
        self.assertNotIn("token_expire_time", self.session.session_handle)
        self.assertFalse(self.session.access_token_expiring())

    def test_atomic_persistence_has_private_permissions(self):
        self.assertEqual(stat.S_IMODE(module._SESSION_FILE.stat().st_mode), 0o600)
        self.assertEqual(list(self.root.glob(".session-*")), [])
        before = module._SESSION_FILE.read_text()
        with patch.object(module.os, "replace", side_effect=OSError("disk failure")), self.assertLogs(module.logger, level="WARNING"):
            self.session.update_tokens({"auth_token": "unsaved"})
        self.assertEqual(module._SESSION_FILE.read_text(), before)
        self.assertEqual(list(self.root.glob(".session-*")), [])

    def test_clear_does_not_restore_session_after_restart(self):
        self.session.clear()
        restored = module.GrubhubSession()
        self.assertFalse(restored.is_authenticated)
        self.assertIsNone(restored.refresh_token)
        self.assertFalse(module._SESSION_FILE.exists())


class ClientRefreshTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        for name, value in (("_SESSION_DIR", self.root), ("_SESSION_FILE", self.root / "session.json")):
            patcher = patch.object(module, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.client = module.GrubhubClient()
        await self.client.close()
        self.client.session.set_authenticated(session_data())
        self.addAsyncCleanup(self.client.close)

    def transport(self, handler):
        self.client._http = httpx.AsyncClient(base_url=module.BASE_URL, transport=httpx.MockTransport(handler))

    async def test_errors_carry_grubhub_details(self):
        self.transport(lambda request: httpx.Response(
            422, json=[{"property": "restaurant_id", "message_key": "required", "message": None}]))
        with self.assertRaises(httpx.HTTPStatusError) as caught:
            await self.client.post("/carts/abc/lines", data={})
        self.assertIsInstance(caught.exception, module.GrubhubAPIError)
        self.assertEqual(caught.exception.response.status_code, 422)
        self.assertEqual(
            str(caught.exception),
            'Grubhub returned 422 for POST /carts/abc/lines: '
            '[{"property":"restaurant_id","message_key":"required","message":null}]',
        )

    async def test_long_and_empty_error_bodies(self):
        responses = iter([httpx.Response(500, text="x" * 2000), httpx.Response(404)])
        self.transport(lambda request: next(responses))
        with self.assertRaises(module.GrubhubAPIError) as long_error:
            await self.client.get("/a", params={"q": "1"})
        self.assertTrue(str(long_error.exception).endswith(": " + "x" * 1000 + "…"))
        with self.assertRaises(module.GrubhubAPIError) as empty_error:
            await self.client.get("/b")
        self.assertEqual(str(empty_error.exception), "Grubhub returned 404 for GET /b")

    async def test_authenticated_401_retries_all_verbs_with_rotated_token(self):
        for method in ("get", "post", "put", "delete"):
            with self.subTest(method=method):
                self.client.session.set_authenticated(session_data())
                calls = []
                def handler(request):
                    calls.append(request)
                    if request.url.path == "/auth/refresh":
                        self.assertEqual(json.loads(request.content)["refresh_token"], "old-refresh")
                        return httpx.Response(200, json={"session_handle": {"access_token": "new-access", "refresh_token": "new-refresh"}})
                    if request.headers.get("authorization") == "Bearer old-access":
                        return httpx.Response(401)
                    self.assertEqual(request.headers["authorization"], "Bearer new-access")
                    return httpx.Response(200, json={"ok": True})
                self.transport(handler)
                result = await getattr(self.client, method)("/resource")
                self.assertEqual(result, {"ok": True})
                self.assertEqual([r.url.path for r in calls], ["/resource", "/auth/refresh", "/resource"])
                reloaded = module.GrubhubSession()
                self.assertEqual(reloaded.refresh_token, "new-refresh")
                self.assertEqual(reloaded.diner_udid, "diner-123")
                await self.client.close()

    async def test_expiring_token_is_refreshed_before_request(self):
        old = datetime.now(timezone.utc) - timedelta(hours=2)
        assert self.client.session.session_handle is not None
        self.client.session.session_handle.update({"token_created": old.isoformat(), "expire_in": 60})
        self.client.session._save()
        calls = []
        def handler(request):
            calls.append(request.url.path)
            if request.url.path == "/auth/refresh":
                return httpx.Response(200, json={"auth_token": "new-access"})
            self.assertEqual(request.headers["authorization"], "Bearer new-access")
            return httpx.Response(200, json={"ok": True})
        self.transport(handler)
        await self.client.get("/resource")
        self.assertEqual(calls, ["/auth/refresh", "/resource"])

    async def test_concurrent_401s_refresh_only_once(self):
        refreshes = 0
        async def handler(request):
            nonlocal refreshes
            if request.url.path == "/auth/refresh":
                refreshes += 1
                await asyncio.sleep(0)
                return httpx.Response(200, json={"auth_token": "new-access"})
            if request.headers.get("authorization") == "Bearer old-access":
                await asyncio.sleep(0)
                return httpx.Response(401)
            return httpx.Response(200, json={"ok": True})
        self.transport(handler)
        results = await asyncio.gather(*(self.client.get("/resource") for _ in range(5)))
        self.assertEqual(results, [{"ok": True}] * 5)
        self.assertEqual(refreshes, 1)

    @unittest.skipIf(module.fcntl is None, "Cross-invocation lock is POSIX-only")
    async def test_two_clients_share_rotated_session_without_double_refresh(self):
        other = module.GrubhubClient()
        await other.close()
        refreshes = 0
        async def handler(request):
            nonlocal refreshes
            if request.url.path == "/auth/refresh":
                refreshes += 1
                await asyncio.sleep(0.02)
                return httpx.Response(200, json={"session_handle": {"access_token": "new-access", "refresh_token": "new-refresh"}})
            if request.headers.get("authorization") == "Bearer old-access":
                return httpx.Response(401)
            return httpx.Response(200, json={"ok": True})
        self.transport(handler)
        other._http = httpx.AsyncClient(base_url=module.BASE_URL, transport=httpx.MockTransport(handler))
        self.addAsyncCleanup(other.close)
        results = await asyncio.gather(self.client.get("/resource"), other.get("/resource"))
        self.assertEqual(results, [{"ok": True}] * 2)
        self.assertEqual(refreshes, 1)
        self.assertEqual(other.session.refresh_token, "new-refresh")
        self.assertEqual(other.session.diner_udid, "diner-123")

    async def test_login_401_does_not_refresh_or_retry_credentials(self):
        calls = []
        def handler(request):
            calls.append(request)
            return httpx.Response(401)
        self.transport(handler)
        with self.assertRaises(httpx.HTTPStatusError):
            await self.client.post("/auth/login", data={"email": "test@example.com"}, auth_required=False)
        self.assertEqual(len(calls), 1)
        self.assertNotIn("authorization", calls[0].headers)

    async def test_refresh_failure_preserves_session_and_does_not_loop(self):
        for response in (httpx.Response(401), httpx.Response(503), httpx.Response(200, json={})):
            with self.subTest(status=response.status_code):
                self.client.session.set_authenticated(session_data())
                calls = []
                def handler(request):
                    calls.append(request.url.path)
                    return response if request.url.path == "/auth/refresh" else httpx.Response(401)
                self.transport(handler)
                with self.assertRaises(httpx.HTTPStatusError), self.assertLogs(module.logger, level="WARNING"):
                    await self.client.get("/resource")
                self.assertEqual(calls, ["/resource", "/auth/refresh"])
                reloaded = module.GrubhubSession()
                self.assertEqual(reloaded.refresh_token, "old-refresh")
                self.assertEqual(reloaded.diner_udid, "diner-123")
                await self.client.close()

    async def test_preflight_failure_does_not_refresh_again_on_same_request(self):
        assert self.client.session.session_handle is not None
        self.client.session.session_handle["token_expire_time"] = 1
        self.client.session._save()
        calls = []
        def handler(request):
            calls.append(request.url.path)
            return httpx.Response(503 if request.url.path == "/auth/refresh" else 401)
        self.transport(handler)
        with self.assertRaises(httpx.HTTPStatusError), self.assertLogs(module.logger, level="WARNING"):
            await self.client.get("/resource")
        self.assertEqual(calls, ["/auth/refresh", "/resource"])

    async def test_second_401_stops_after_one_retry(self):
        calls = []
        def handler(request):
            calls.append(request.url.path)
            if request.url.path == "/auth/refresh":
                return httpx.Response(200, json={"auth_token": "new-access"})
            return httpx.Response(401)
        self.transport(handler)
        with self.assertRaises(httpx.HTTPStatusError):
            await self.client.get("/resource")
        self.assertEqual(calls, ["/resource", "/auth/refresh", "/resource"])

    async def test_retries_preserve_query_pairs_and_json_payload(self):
        calls = []
        def handler(request):
            calls.append(request)
            if request.url.path == "/auth/refresh":
                return httpx.Response(200, json={"auth_token": "new-access"})
            return httpx.Response(401 if request.headers.get("authorization") == "Bearer old-access" else 200, json={})
        self.transport(handler)
        await self.client.post("/resource", data={"quantity": 2}, params=[("facet", "a"), ("facet", "b")])
        self.assertEqual(calls[0].url.params.multi_items(), calls[2].url.params.multi_items())
        self.assertEqual(calls[0].content, calls[2].content)

    async def test_otp_session_metadata_does_not_discard_issued_refresh_token(self):
        from src.grubhub_mcp.auth import verify_otp
        self.client.session.csrf_token = "challenge"
        def handler(request):
            if request.url.path == "/auth/confirmation_code":
                return httpx.Response(200, json={"session_handle": {"access_token": "otp-access", "refresh_token": "otp-refresh"}})
            self.assertEqual(request.url.path, "/session")
            self.assertEqual(request.headers["authorization"], "Bearer otp-access")
            return httpx.Response(200, json={"credential": {"ud_id": "otp-diner"}})
        self.transport(handler)
        await verify_otp(self.client, "user@example.com", "123456")
        reloaded = module.GrubhubSession()
        self.assertEqual(reloaded.auth_token, "otp-access")
        self.assertEqual(reloaded.refresh_token, "otp-refresh")
        self.assertEqual(reloaded.diner_udid, "otp-diner")

    async def test_deleted_session_is_not_resurrected_by_stale_client(self):
        module._SESSION_FILE.unlink()
        calls = []
        def handler(request):
            calls.append(request.url.path)
            return httpx.Response(401)
        self.transport(handler)
        with self.assertRaises(httpx.HTTPStatusError):
            await self.client.get("/resource")
        self.assertEqual(calls, ["/resource"])
        self.assertFalse(module._SESSION_FILE.exists())

    async def test_other_account_login_is_not_used_to_retry_old_request(self):
        newer = module.GrubhubSession()
        newer.set_authenticated(session_data("account-b", "refresh-b", "diner-b"))
        calls = []
        def handler(request):
            calls.append(request)
            return httpx.Response(401)
        self.transport(handler)
        with self.assertRaisesRegex(ValueError, "Login changed"):
            await self.client.get("/diners/diner-123/details")
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0].headers["authorization"], "Bearer old-access")
        self.assertEqual(module.GrubhubSession().diner_udid, "diner-b")

    async def test_same_client_login_during_request_cannot_replay_with_new_account(self):
        from src.grubhub_mcp.auth import login
        calls = []
        async def handler(request):
            calls.append(request.url.path)
            if request.url.path == "/auth/login":
                return httpx.Response(200, json=session_data("account-b", "refresh-b", "diner-b"))
            await login(self.client, "b@example.com", "password")
            return httpx.Response(401)
        self.transport(handler)
        with self.assertRaisesRegex(ValueError, "Login changed"):
            await self.client.get("/resource-for-a")
        self.assertEqual(calls, ["/resource-for-a", "/auth/login"])
        self.assertEqual(module.GrubhubSession().diner_udid, "diner-b")

    async def test_logout_during_refresh_does_not_resurrect_session(self):
        from src.grubhub_mcp.auth import logout
        other = module.GrubhubClient()
        await other.close()
        refreshing, logging_out, finish_refresh = asyncio.Event(), asyncio.Event(), asyncio.Event()
        async def handler(request):
            if request.url.path == "/auth/refresh":
                refreshing.set()
                await finish_refresh.wait()
                return httpx.Response(200, json={"auth_token": "new-access"})
            if request.url.path == "/auth/logout":
                logging_out.set()
                return httpx.Response(200, json={})
            return httpx.Response(401 if request.headers.get("authorization") == "Bearer old-access" else 200, json={})
        self.transport(handler)
        other._http = httpx.AsyncClient(base_url=module.BASE_URL, transport=httpx.MockTransport(handler))
        self.addAsyncCleanup(other.close)
        get_task = asyncio.create_task(self.client.get("/resource"))
        await asyncio.wait_for(refreshing.wait(), 2)
        logout_task = asyncio.create_task(logout(other))
        await asyncio.wait_for(logging_out.wait(), 2)
        finish_refresh.set()
        await asyncio.wait_for(asyncio.gather(get_task, logout_task), 2)
        logged_out = module.GrubhubSession()
        self.assertFalse(logged_out.is_authenticated)
        self.assertIsNone(logged_out.auth_token)
        self.assertIsNone(logged_out.refresh_token)

    async def test_new_login_waits_for_refresh_then_wins_persistence(self):
        from src.grubhub_mcp.auth import login
        other = module.GrubhubClient()
        await other.close()
        refreshing, logging_in, finish_refresh = asyncio.Event(), asyncio.Event(), asyncio.Event()
        async def handler(request):
            if request.url.path == "/auth/refresh":
                refreshing.set()
                await finish_refresh.wait()
                return httpx.Response(200, json={"auth_token": "refreshed-a"})
            if request.url.path == "/auth/login":
                logging_in.set()
                return httpx.Response(200, json=session_data("account-b", "refresh-b", "diner-b"))
            return httpx.Response(401 if request.headers.get("authorization") == "Bearer old-access" else 200, json={})
        self.transport(handler)
        other._http = httpx.AsyncClient(base_url=module.BASE_URL, transport=httpx.MockTransport(handler))
        self.addAsyncCleanup(other.close)
        get_task = asyncio.create_task(self.client.get("/resource"))
        await asyncio.wait_for(refreshing.wait(), 2)
        login_task = asyncio.create_task(login(other, "b@example.com", "password"))
        await asyncio.wait_for(logging_in.wait(), 2)
        finish_refresh.set()
        await asyncio.wait_for(asyncio.gather(get_task, login_task), 2)
        persisted = module.GrubhubSession()
        self.assertEqual(persisted.auth_token, "account-b")
        self.assertEqual(persisted.diner_udid, "diner-b")

    async def test_stale_anonymous_response_does_not_replace_new_login(self):
        from src.grubhub_mcp.auth import create_anonymous_session
        self.client.session.auth_token = None
        self.client.session.is_authenticated = False
        newer = module.GrubhubSession()
        newer.set_authenticated(session_data("account-b", "refresh-b", "diner-b"))
        self.transport(lambda _: httpx.Response(200, json={"auth_token": "anonymous"}))
        await create_anonymous_session(self.client)
        self.assertEqual(self.client.session.auth_token, "account-b")
        self.assertTrue(self.client.session.is_authenticated)
        self.assertEqual(module.GrubhubSession().diner_udid, "diner-b")

    async def test_logout_does_not_clear_new_login_completed_during_request(self):
        from src.grubhub_mcp.auth import login, logout
        async def handler(request):
            if request.url.path == "/auth/login":
                return httpx.Response(200, json=session_data("account-b", "refresh-b", "diner-b"))
            await login(self.client, "b@example.com", "password")
            return httpx.Response(200, json={})
        self.transport(handler)
        await logout(self.client)
        self.assertEqual(module.GrubhubSession().diner_udid, "diner-b")

    async def test_logout_cancels_delayed_login_otp_and_account_responses(self):
        from src.grubhub_mcp.auth import create_account, login, logout, verify_otp
        for operation in ("login", "otp", "create_account"):
            with self.subTest(operation=operation):
                self.client.session.set_authenticated(session_data())
                self.client.session.csrf_token = "challenge"
                async def handler(request):
                    if request.url.path == "/auth/logout":
                        return httpx.Response(200, json={})
                    await logout(self.client)
                    return httpx.Response(200, json=session_data("account-b", "refresh-b", "diner-b"))
                self.transport(handler)
                with self.assertRaisesRegex(ValueError, "Session changed"):
                    if operation == "login":
                        await login(self.client, "b@example.com", "password")
                    elif operation == "otp":
                        await verify_otp(self.client, "b@example.com", "123456")
                    else:
                        await create_account(self.client, "b@example.com", "password", "B", "User")
                saved = module.GrubhubSession()
                self.assertFalse(saved.is_authenticated)
                self.assertIsNone(saved.auth_token)
                self.assertIsNone(saved.refresh_token)
                self.assertIsNotNone(saved.login_generation)
                await self.client.close()

    async def test_logout_marker_cancels_login_started_without_existing_session(self):
        from src.grubhub_mcp.auth import login, logout
        self.client.session.clear()
        async def handler(request):
            if request.url.path == "/auth/logout":
                return httpx.Response(200, json={})
            await logout(self.client)
            return httpx.Response(200, json=session_data("account-b", "refresh-b", "diner-b"))
        self.transport(handler)
        with self.assertRaisesRegex(ValueError, "Session changed"):
            await login(self.client, "b@example.com", "password")
        self.assertFalse(module.GrubhubSession().is_authenticated)

    async def test_delayed_login_does_not_replace_login_completed_after_it_started(self):
        from src.grubhub_mcp.auth import login
        async def handler(request):
            email = json.loads(request.content)["email"]
            if email == "c@example.com":
                return httpx.Response(200, json=session_data("account-c", "refresh-c", "diner-c"))
            await login(self.client, "c@example.com", "password")
            return httpx.Response(200, json=session_data("account-b", "refresh-b", "diner-b"))
        self.transport(handler)
        with self.assertRaisesRegex(ValueError, "Session changed"):
            await login(self.client, "b@example.com", "password")
        self.assertEqual(module.GrubhubSession().diner_udid, "diner-c")

    async def test_empty_response_is_supported(self):
        self.transport(lambda _: httpx.Response(204))
        self.assertEqual(await self.client.delete("/resource"), {})


if __name__ == "__main__":
    unittest.main()
