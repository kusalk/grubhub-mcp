from __future__ import annotations

import json
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import httpx

from src.grubhub_mcp.tools import payments as payment_tools

CREDITS = {"lines_of_credit": [{"id": "credit-1", "source_type": "SUBSCRIPTION_PREMIUM", "balance": 1000}]}
VAULT = {"gift_cards": [{"id": "gc-1", "amount_remaining": 2500}], "total_amount_remaining": 2500}


def error(status: int) -> httpx.HTTPStatusError:
    request = httpx.Request("GET", "https://api-gtm.grubhub.com/x")
    return httpx.HTTPStatusError("error", request=request, response=httpx.Response(status, request=request))


class FakePaymentsClient:
    def __init__(self, vault: dict | Exception = VAULT, redeem: dict | Exception | None = None) -> None:
        self.session = SimpleNamespace(is_authenticated=True, diner_udid="diner-1", auth_token="token")
        self.vault, self.redeem = vault, redeem if redeem is not None else {"id": "gc-1"}
        self.posts: list[tuple[str, dict]] = []

    async def get(self, path, params=None, auth_required=True):
        if path == "/grubcash/diner-1/list":
            return CREDITS
        if path == "/codes/vault/diner-1/giftcards":
            if isinstance(self.vault, Exception):
                raise self.vault
            return self.vault
        raise AssertionError(f"unexpected GET {path}")

    async def post(self, path, data=None, auth_required=True, params=None):
        self.posts.append((path, data))
        if isinstance(self.redeem, Exception):
            raise self.redeem
        return self.redeem


class GiftCardTests(unittest.IsolatedAsyncioTestCase):
    async def run_tool(self, client, name, **arguments) -> dict:
        tools: dict = {}
        payment_tools.register(SimpleNamespace(tool=lambda **_: lambda fn: tools.setdefault(fn.__name__, fn)))
        with patch("src.grubhub_mcp.tools.payments.get_client", return_value=client):
            return json.loads(await tools[name](**arguments))

    async def test_credits_include_gift_card_balance(self):
        out = await self.run_tool(FakePaymentsClient(), "get_grubhub_credits")
        self.assertEqual(out["lines_of_credit"][0]["id"], "credit-1")
        self.assertEqual(out["gift_cards"]["total_amount_remaining"], 2500)

    async def test_credits_still_listed_when_gift_cards_unavailable(self):
        out = await self.run_tool(FakePaymentsClient(vault=error(503)), "get_grubhub_credits")
        self.assertEqual(out["lines_of_credit"][0]["id"], "credit-1")
        self.assertNotIn("gift_cards", out)

    async def test_redeem_posts_the_code_and_reports_the_balance(self):
        client = FakePaymentsClient()
        out = await self.run_tool(client, "redeem_gift_card", code="  ABCD EFGH IJKL MNOP Q ")
        self.assertEqual(client.posts, [("/codes/vault/diner-1", {"code_text": "ABCD EFGH IJKL MNOP Q"})])
        self.assertEqual(out, {"redeemed": {"id": "gc-1"}, "gift_cards": VAULT})

    async def test_rejected_code_raises_grubhubs_reason(self):
        with self.assertRaises(httpx.HTTPStatusError):
            await self.run_tool(FakePaymentsClient(redeem=error(404)), "redeem_gift_card", code="0000")

    async def test_redeem_requires_login(self):
        client = FakePaymentsClient()
        client.session.is_authenticated = False
        out = await self.run_tool(client, "redeem_gift_card", code="ABCD")
        self.assertIn("logged in", out["error"])
        self.assertEqual(client.posts, [])
