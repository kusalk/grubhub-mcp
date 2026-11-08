from __future__ import annotations

import json
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from src.grubhub_mcp.tools import order as order_tools

CART = "cart1"
CARDS = {"credit_cards": [
    {"id": "old-card", "expired": False, "last_used_date": 100},
    {"id": "expired-card", "expired": True, "last_used_date": 300},
    {"id": "recent-card", "expired": False, "last_used_date": 200},
]}


class FakeCheckoutClient:
    """A cart that becomes ready once a card is on it, charging card_cents."""

    def __init__(self, card_cents: int = 2387, cards_on_cart: list | None = None, problems: list | None = None):
        self.session = SimpleNamespace(is_authenticated=True, diner_udid="diner-1", auth_token="token")
        self.calls: list[tuple[str, str, object]] = []
        self.card_cents, self.problems = card_cents, problems or []
        self.cards_on_cart = list(cards_on_cart or [])

    async def get(self, path, params=None, auth_required=True):
        self.calls.append(("GET", path, params))
        if path == "/payments/diner-1/payments":
            return CARDS
        if path == f"/carts/{CART}/bill":
            ready = bool(self.cards_on_cart) and not self.problems
            return {
                "state": "READY_FOR_CHECKOUT" if ready else "INVALID",
                "validation_errors": self.problems if self.cards_on_cart else [{"property": "payments", "message_key": "invalid-amountnotmet"}],
                "checkout_token": "token-123" if ready else None,
                "charges": {"diner_grand_total": self.card_cents},
                "payments": {"CREDIT_CARD": [{"payment_id": card, "encoded_id": f"enc-{card}", "amount": self.card_cents}
                                             for card in self.cards_on_cart]},
            }
        raise AssertionError(f"unexpected GET {path}")

    async def post(self, path, data=None, auth_required=True, params=None):
        self.calls.append(("POST", path, data))
        if path.endswith("/payments"):
            self.cards_on_cart.append(data["payment_id"])
            return {"id": "new"}
        if path.endswith("/checkout"):
            return {"state": "CHECKOUT_COMPLETE", "status": "ASAP_PROCESSED", "order_number": "100200300400500",
                    "charges": {"diner_grand_total": 3997},
                    "payments": {"CREDIT_CARD": [{"amount": self.card_cents}]},
                    "ordering_info": {"real_time_eta": {"estimate_range": {
                        "start_time": "2026-11-08T04:44:00.000Z", "end_time": "2026-11-08T04:59:00.000Z",
                        "start_time_minutes": 30, "end_time_minutes": 45}}}}
        raise AssertionError(f"unexpected POST {path}")

    async def delete(self, path, auth_required=True):
        self.calls.append(("DELETE", path, None))
        self.cards_on_cart = [c for c in self.cards_on_cart if not path.endswith(f"enc-{c}")]
        return {}

    def checkouts(self) -> list:
        return [body for verb, path, body in self.calls if path.endswith("/checkout")]


class PlaceOrderTests(unittest.IsolatedAsyncioTestCase):
    async def place(self, client, **arguments) -> dict:
        tools: dict = {}
        order_tools.register(SimpleNamespace(tool=lambda **_: lambda fn: tools.setdefault(fn.__name__, fn)))
        with patch("src.grubhub_mcp.tools.order.get_client", return_value=client):
            return json.loads(await tools["place_order"](cart_id=CART, **arguments))

    async def test_confirmed_charge_checks_out_with_the_bill_token(self):
        client = FakeCheckoutClient()
        out = await self.place(client, expected_charge=23.87)
        self.assertEqual(client.checkouts(), [{"checkout_token": "token-123"}])
        self.assertEqual(out["summary"]["placed"], True)
        self.assertEqual(out["summary"]["order_number"], "100200300400500")
        self.assertEqual(out["summary"]["arriving_in"], "30-45 min")
        self.assertEqual(out["summary"]["charged_to_card"], "$23.87")

    async def test_default_card_is_most_recent_unexpired(self):
        client = FakeCheckoutClient()
        await self.place(client, expected_charge=23.87)
        added = [body["payment_id"] for verb, path, body in client.calls if path.endswith("/payments") and verb == "POST"]
        self.assertEqual(added, ["recent-card"])

    async def test_different_charge_is_refused_without_checkout(self):
        client = FakeCheckoutClient(card_cents=2500)
        out = await self.place(client, expected_charge=23.87)
        self.assertIn("card charge is $25.00, not the confirmed $23.87", out["error"])
        self.assertEqual(client.checkouts(), [])

    async def test_cart_with_problems_is_refused_without_checkout(self):
        client = FakeCheckoutClient(problems=[{"property": "fulfillment_info", "message_key": "invalid-fulfillmentinforequired"}])
        out = await self.place(client, expected_charge=23.87)
        self.assertIn("isn't ready", out["error"])
        self.assertEqual(out["summary"]["problems"], ["No delivery address."])
        self.assertEqual(client.checkouts(), [])

    async def test_requested_card_replaces_another_and_is_not_added_twice(self):
        client = FakeCheckoutClient(cards_on_cart=["old-card"])
        await self.place(client, expected_charge=23.87, payment_method_id="recent-card")
        self.assertIn(("DELETE", f"/carts/{CART}/payments/enc-old-card", None), client.calls)
        client = FakeCheckoutClient(cards_on_cart=["recent-card"])
        await self.place(client, expected_charge=23.87, payment_method_id="recent-card")
        self.assertFalse([c for c in client.calls if c[0] == "POST" and c[1].endswith("/payments")])

    async def test_requires_login(self):
        client = FakeCheckoutClient()
        client.session.is_authenticated = False
        out = await self.place(client, expected_charge=23.87)
        self.assertIn("logged in", out["error"])
        self.assertEqual(client.calls, [])


class PaymentMethodTests(unittest.IsolatedAsyncioTestCase):
    async def test_card_nonce_is_not_shown(self):
        from src.grubhub_mcp.tools import payments as payment_tools
        client = FakeCheckoutClient()
        tools: dict = {}
        payment_tools.register(SimpleNamespace(tool=lambda **_: lambda fn: tools.setdefault(fn.__name__, fn)))
        CARDS["credit_cards"][0]["nonce"] = "secret-nonce"
        try:
            with patch("src.grubhub_mcp.tools.payments.get_client", return_value=client):
                raw = await tools["get_payment_methods"]()
        finally:
            del CARDS["credit_cards"][0]["nonce"]
        self.assertNotIn("secret-nonce", raw)
        self.assertIn("recent-card", raw)
