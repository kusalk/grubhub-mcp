from __future__ import annotations

from contextlib import asynccontextmanager

import json
from typing import Any
import unittest
from unittest.mock import patch

import httpx

from src.grubhub_mcp import auth as auth_module
from src.grubhub_mcp.tools import auth as auth_tools
from src.grubhub_mcp.tools import order as order_tools


class FakeSession:
    def __init__(self) -> None:
        self.auth_token = "token"
        self.refresh_token = "refresh"
        self.csrf_token = "csrf"
        self.diner_udid = None
        self.is_authenticated = True
        self.login_generation = None

    def set_authenticated(self, session_data: dict) -> None:
        self.update_tokens(session_data)
        self.is_authenticated = True

    def update_tokens(self, session_data: dict) -> None:
        handle = session_data.get("session_handle") or {}
        self.auth_token = handle.get("access_token") or self.auth_token
        self.refresh_token = handle.get("refresh_token") or self.refresh_token
        credential = session_data.get("credential") or {}
        self.diner_udid = credential.get("ud_id") or credential.get("udid") or self.diner_udid


class FakeClient:
    def __init__(self, history_orders: list[dict] | None = None, history_pages: dict[int, dict] | None = None) -> None:
        self.session = FakeSession()
        self.session.diner_udid = "diner-123"
        self.history_orders = history_orders or []
        self.history_pages = history_pages
        self.put_calls: list[tuple[str, dict, bool]] = []
        self.get_calls: list[tuple[str, Any, bool]] = []
        self.post_calls: list[tuple[str, dict | None, bool, Any]] = []

    @asynccontextmanager
    async def session_transition(self):
        yield

    async def put(self, path: str, data: dict | None = None, auth_required: bool = True):
        self.put_calls.append((path, data or {}, auth_required))
        return {"session_handle": {"access_token": "new-token"}}

    async def get(self, path: str, params=None, auth_required: bool = True):
        self.get_calls.append((path, params, auth_required))
        if path == "/session":
            return {
                "credential": {"ud_id": "otp-diner-456"},
                "session_handle": {"access_token": "new-token"},
            }
        if path.startswith("/orders/"):
            request = httpx.Request("GET", f"https://api-gtm.grubhub.com{path}")
            response = httpx.Response(404, request=request)
            raise httpx.HTTPStatusError("not found", request=request, response=response)
        if path == f"/diners/{self.session.diner_udid}/search_listing":
            query: dict[str, Any] = dict(params or [])
            page, size = query["pageNum"], query["pageSize"]
            if self.history_pages is not None:
                return self.history_pages[page]
            start = (page - 1) * size
            return {
                "results": self.history_orders[start:start + size],
                "partner_results": [],
                "pager": {
                    "total_pages": (len(self.history_orders) + size - 1) // size,
                    "current_page": page,
                },
            }
        raise AssertionError(f"unexpected GET path: {path}")

    async def post(self, path: str, data: dict | None = None, auth_required: bool = True, params=None):
        self.post_calls.append((path, data, auth_required, params))
        if path.startswith("/orders/") and path.endswith("/reorder"):
            request = httpx.Request("POST", f"https://api-gtm.grubhub.com{path}")
            response = httpx.Response(404, request=request)
            raise httpx.HTTPStatusError("not found", request=request, response=response)
        if path == "/carts":
            return {"id": "cart-123", "already_exists": False}
        raise AssertionError(f"unexpected POST path: {path}")


class FakeMCP:
    def __init__(self) -> None:
        self.tools: dict[str, Any] = {}

    def tool(self, **_settings):
        def decorator(func):
            self.tools[func.__name__] = func
            return func
        return decorator


class AuthAndOrdersTests(unittest.IsolatedAsyncioTestCase):
    async def test_verify_otp_fetches_session_when_udid_missing(self):
        client: Any = FakeClient()
        client.session.diner_udid = None
        await auth_module.verify_otp(client, "user@example.com", "123456")
        self.assertEqual(client.session.diner_udid, "otp-diner-456")
        self.assertEqual(client.session.refresh_token, "refresh")
        self.assertEqual(client.get_calls[0][0], "/session")

    async def test_verify_login_otp_maps_401_to_friendly_error(self):
        client = FakeClient()
        mcp = FakeMCP()
        auth_tools.register(mcp)
        request = httpx.Request("PUT", "https://api-gtm.grubhub.com/auth/confirmation_code")
        response = httpx.Response(401, request=request)
        with (
            patch("src.grubhub_mcp.tools.auth.get_client", return_value=client),
            patch("src.grubhub_mcp.tools.auth.auth_module.verify_otp",
                  side_effect=httpx.HTTPStatusError("unauthorized", request=request, response=response)),
        ):
            raw = await mcp.tools["verify_login_otp"]("user@example.com", "000000")
        self.assertEqual(json.loads(raw), {"error": "OTP expired or invalid — request a new code with send_login_otp"})


    async def test_get_order_history_paginates_server_side(self):
        orders = [{"id": f"order-{i}"} for i in range(5)]
        client = FakeClient(history_orders=orders)
        mcp = FakeMCP()
        order_tools.register(mcp)
        with patch("src.grubhub_mcp.tools.order.get_client", return_value=client):
            first = json.loads(await mcp.tools["get_order_history"](page_size=2, page_num=1))
            second = json.loads(await mcp.tools["get_order_history"](page_size=2, page_num=2))
        self.assertEqual(first["orders"], orders[:2])
        self.assertEqual(second["orders"], orders[2:4])
        self.assertEqual(second["pagination"], {
            "page_size": 2, "page_num": 2, "returned": 2, "total_pages": 3, "current_page": 2,
        })
        path, params, auth_required = client.get_calls[1]
        self.assertEqual(path, "/diners/diner-123/search_listing")
        self.assertTrue(auth_required)
        self.assertIn(("pageNum", 2), params)
        self.assertIn(("pageSize", 2), params)
        self.assertEqual([value for key, value in params if key == "facet"], ["scheduled:false", "orderType:ALL"])
        self.assertIn(("includePartnerOrders", "true"), params)

    async def test_order_history_defaults_to_first_page_and_twenty_orders(self):
        client = FakeClient()
        mcp = FakeMCP()
        order_tools.register(mcp)
        with patch("src.grubhub_mcp.tools.order.get_client", return_value=client):
            data = json.loads(await mcp.tools["get_order_history"]())
        self.assertEqual(dict(client.get_calls[0][1])["pageNum"], 1)
        self.assertEqual(dict(client.get_calls[0][1])["pageSize"], 20)
        self.assertEqual(data["orders"], [])
        self.assertEqual(data["pagination"]["total_pages"], 0)

    async def test_history_normalizes_partner_results_and_missing_pager(self):
        response = {"results": [{"id": "normal"}], "partner_results": [{"id": "partner"}]}
        client = FakeClient(history_pages={1: response})
        mcp = FakeMCP()
        order_tools.register(mcp)
        with patch("src.grubhub_mcp.tools.order.get_client", return_value=client):
            data = json.loads(await mcp.tools["get_order_history"]())
        self.assertEqual(data["orders"], response["results"] + response["partner_results"])
        self.assertEqual(data["pagination"]["returned"], 2)
        self.assertNotIn("total_pages", data["pagination"])
        self.assertNotIn("current_page", data["pagination"])

    async def test_history_handles_null_results_and_pager(self):
        client = FakeClient(history_pages={1: {"results": None, "partner_results": None, "pager": None}})
        data = await order_tools._fetch_order_history_raw(client)
        self.assertEqual(data, {"orders": [], "pager": {}})
        self.assertIsNone(await order_tools._find_order_in_history(client, "missing"))

    async def test_history_rejects_non_positive_page_parameters_before_request(self):
        client = FakeClient()
        mcp = FakeMCP()
        order_tools.register(mcp)
        with patch("src.grubhub_mcp.tools.order.get_client", return_value=client):
            for args in ({"page_size": 0}, {"page_size": -1}, {"page_num": 0}, {"page_num": -1}):
                with self.subTest(args=args), self.assertRaises(ValueError):
                    await mcp.tools["get_order_history"](**args)
        self.assertEqual(client.get_calls, [])

    async def test_get_order_returns_friendly_auth_error_when_logged_out(self):
        client = FakeClient()
        client.session.is_authenticated = False
        client.session.diner_udid = None
        mcp = FakeMCP()
        order_tools.register(mcp)
        with patch("src.grubhub_mcp.tools.order.get_client", return_value=client):
            data = json.loads(await mcp.tools["get_order"]("order-1"))
        self.assertEqual(data, {"error": "Must be logged in to view order details"})
        self.assertEqual(client.get_calls, [])

    async def test_get_order_fallback_finds_order_id_or_group_id_on_later_page(self):
        orders = [{"id": f"order-{i}", "group_id": f"group-{i}"} for i in range(45)]
        for target in ("order-44", "group-44"):
            with self.subTest(target=target):
                client = FakeClient(history_orders=orders)
                mcp = FakeMCP()
                order_tools.register(mcp)
                with patch("src.grubhub_mcp.tools.order.get_client", return_value=client):
                    data = json.loads(await mcp.tools["get_order"](target))
                self.assertEqual(data["id"], "order-44")
                self.assertEqual([dict(params)["pageNum"] for path, params, _ in client.get_calls if path.endswith("search_listing")], [1, 2, 3])

    async def test_get_order_not_found_stops_at_last_page(self):
        client = FakeClient(history_orders=[{"id": f"order-{i}"} for i in range(25)])
        mcp = FakeMCP()
        order_tools.register(mcp)
        with patch("src.grubhub_mcp.tools.order.get_client", return_value=client):
            out = json.loads(await mcp.tools["get_order"]("absent"))
        self.assertIn("No order 'absent'", out["error"])
        self.assertEqual([dict(params)["pageNum"] for path, params, _ in client.get_calls if path.endswith("search_listing")], [1, 2])
