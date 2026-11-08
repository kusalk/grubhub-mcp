from __future__ import annotations

import json
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import httpx

from src.grubhub_mcp import carts
from src.grubhub_mcp.tools import cart as cart_tools

CART = "cart1"
HOME = {
    "id": "home-1", "label": "Home", "phone": "(555) 010-0000", "special_instructions": "Leave at door 4",
    "handoff_options": ["CONTACTLESS"],
    "address": {
        "region_code": "US", "address_lines": ["1 Main St", "4"], "coordinates": {"latitude": 40.7, "longitude": -74.0},
        "administrative_area": "NY", "locality": "New York", "postal_code": "00000", "cross_streets": "",
    },
}
WORK = {**HOME, "id": "work-1", "label": "Work", "special_instructions": "Front desk"}


def rejected(path: str, status: int = 422) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", f"https://api-gtm.grubhub.com{path}")
    return httpx.HTTPStatusError("rejected", request=request, response=httpx.Response(status, request=request))


class FakeCartClient:
    """Answers the cart requests with the shapes Grubhub returned in live checks."""

    def __init__(self, subtotal: int = 6000, offers: list | None = None, applied: list | None = None,
                 refuse: set[str] = frozenset(), lines: list | None = None) -> None:
        self.session = SimpleNamespace(is_authenticated=True, diner_udid="diner-1", auth_token="token")
        self.calls: list[tuple[str, str, object]] = []
        self.subtotal, self.applied, self.refuse = subtotal, list(applied or []), refuse
        self.offers = offers if offers is not None else [
            {"entitlement_id": "five-off", "amount": {"value": 500, "type": "FLAT", "order_minimum": 3000}},
            {"entitlement_id": "ten-off", "amount": {"value": 1000, "type": "FLAT", "order_minimum": 5000}},
            {"entitlement_id": "big-spender", "amount": {"value": 2000, "type": "FLAT", "order_minimum": 9000}},
        ]
        self.lines = lines or []

    async def get(self, path, params=None, auth_required=True):
        self.calls.append(("GET", path, params))
        if path == "/diners/diner-1/addresses":
            return {"diner_addresses": [WORK, HOME]}
        if path == "/session":
            return {"credential": {"first_name": "Ada", "last_name": "Lovelace", "email": "ada@example.test"}}
        if path.startswith("/offers/availability/"):
            return {"available_offers": self.offers}
        if path == f"/carts/{CART}/bill":
            return {
                "id": CART, "restaurant_ids": ["rest-1"], "state": "INVALID",
                "fulfillment_info": {"delivery_info": {"latitude": "40.7", "longitude": "-74.0"}},
                "charges": {"diner_subtotal": self.subtotal, "lines": {"line_items": self.lines}},
                "payments": {"UNIFIED_REWARD": self.applied},
            }
        raise AssertionError(f"unexpected GET {path}")

    async def post(self, path, data=None, auth_required=True, params=None):
        self.calls.append(("POST", path, data))
        if path == "/carts":
            return {"id": CART, "uri": f"/carts/{CART}", "already_exists": False}
        if path.endswith("/payments") and (data or {}).get("payment_id") in self.refuse:
            raise rejected(path)
        if path.endswith("/payments") and data.get("type") == "UNIFIED_REWARD":
            self.applied = [{"payment_id": data["payment_id"], "encoded_id": "applied-1"}]
        return {"id": "new", "already_exists": False}

    async def put(self, path, data=None, auth_required=True):
        self.calls.append(("PUT", path, data))
        return {}

    async def delete(self, path, auth_required=True):
        self.calls.append(("DELETE", path, None))
        return {}

    def sent(self, method: str, suffix: str) -> list:
        return [body for verb, path, body in self.calls if verb == method and path.endswith(suffix)]


class CartToolTests(unittest.IsolatedAsyncioTestCase):
    async def run_tool(self, client: FakeCartClient, name: str, **arguments) -> str:
        tools: dict = {}
        cart_tools.register(SimpleNamespace(tool=lambda **_: lambda fn: tools.setdefault(fn.__name__, fn)))
        with patch("src.grubhub_mcp.tools.cart.get_client", return_value=client):
            return await tools[name](**arguments)

    async def test_create_cart_follows_the_website_sequence(self):
        client = FakeCartClient()
        options = [cart_tools.CartOption(id="size-18", quantity=1)]
        out = json.loads(await self.run_tool(client, "create_cart", restaurant_id="rest-1", menu_item_id="item-1", options=options))
        self.assertEqual(out["id"], CART)
        writes = [(verb, path) for verb, path, _ in self.client_writes(client)]
        self.assertEqual(writes, [
            ("POST", "/carts"),
            ("PUT", f"/carts/{CART}/delivery_info"),
            ("POST", f"/carts/{CART}/lines"),
            ("POST", f"/carts/{CART}/payments"),  # Grubhub+
            ("POST", f"/carts/{CART}/payments"),  # best offer
        ])
        self.assertEqual(client.sent("POST", "/carts")[0]["experiments"], carts.CART_EXPERIMENTS)
        delivery = client.sent("PUT", "/delivery_info")[0]
        self.assertEqual(delivery["delivery_instructions"], "Leave at door 4")  # Home, not the first address
        self.assertEqual(delivery["name"], "Ada Lovelace")
        self.assertEqual(delivery["address"]["coordinates"], {"latitude": "40.7", "longitude": "-74.0"})
        self.assertIs(delivery["green_indicated"], False)  # utensils included
        line = client.sent("POST", "/lines")[0]
        self.assertEqual((line["restaurant_id"], line["options"]), ("rest-1", [{"id": "size-18", "quantity": 1}]))
        grubhub_plus, offer = client.sent("POST", "/payments")
        self.assertEqual((grubhub_plus["type"], grubhub_plus["payment_id"]), ("PROMO_CODE", "diner-1"))
        self.assertEqual((offer["type"], offer["payment_id"]), ("UNIFIED_REWARD", "ten-off"))

    def client_writes(self, client):
        return [call for call in client.calls if call[0] != "GET"]

    async def test_address_and_utensil_choices(self):
        client = FakeCartClient()
        await self.run_tool(client, "create_cart", restaurant_id="rest-1", menu_item_id="item-1",
                            address_id="work-1", include_utensils=False)
        delivery = client.sent("PUT", "/delivery_info")[0]
        self.assertEqual((delivery["delivery_instructions"], delivery["green_indicated"]), ("Front desk", True))

    async def test_failure_after_creating_names_the_cart(self):
        client = FakeCartClient()
        with self.assertRaisesRegex(ValueError, f"Created cart {CART}.*No saved address 'missing'.*home-1 \\(Home\\)"):
            await self.run_tool(client, "create_cart", restaurant_id="rest-1", menu_item_id="item-1", address_id="missing")

    async def test_requires_login(self):
        client = FakeCartClient()
        client.session.is_authenticated = False
        out = json.loads(await self.run_tool(client, "create_cart", restaurant_id="rest-1", menu_item_id="item-1"))
        self.assertIn("Log in first", out["error"])
        self.assertEqual(client.calls, [])

    async def test_refused_offer_falls_back_to_the_next(self):
        client = FakeCartClient(refuse={"ten-off"})
        await self.run_tool(client, "add_to_cart", cart_id=CART, menu_item_id="item-2")
        offers = [body["payment_id"] for body in client.sent("POST", "/payments") if body["type"] == "UNIFIED_REWARD"]
        self.assertEqual(offers, ["ten-off", "five-off"])

    async def test_offer_already_applied_is_left_alone(self):
        client = FakeCartClient(applied=[{"payment_id": "ten-off", "encoded_id": "applied-1"}])
        await self.run_tool(client, "add_to_cart", cart_id=CART, menu_item_id="item-2")
        self.assertEqual([b["type"] for b in client.sent("POST", "/payments")], ["PROMO_CODE"])
        self.assertEqual(client.sent("DELETE", "/payments/applied-1"), [])

    async def test_offer_no_longer_eligible_is_removed(self):
        client = FakeCartClient(subtotal=2000, applied=[{"payment_id": "ten-off", "encoded_id": "applied-1"}])
        await self.run_tool(client, "remove_from_cart", cart_id=CART, line_item_id="line-2")
        self.assertEqual(len(client.sent("DELETE", f"/carts/{CART}/payments/applied-1")), 1)
        self.assertEqual(len(client.sent("DELETE", f"/carts/{CART}/lines/line-2")), 1)

    async def test_update_resends_the_whole_line(self):
        line = {"id": "line-1", "menu_item_id": "item-1", "restaurant_id": "rest-1", "special_instructions": "crispy",
                "options": [{"id": "size-18", "name": "Large", "quantity": 1,
                             "child_options": [{"id": "extra", "quantity": 2, "child_options": []}]}]}
        client = FakeCartClient(lines=[line])
        await self.run_tool(client, "update_cart_item", cart_id=CART, line_item_id="line-1", quantity=3)
        self.assertEqual(client.sent("PUT", "/lines/line-1"), [{
            "menu_item_id": "item-1", "restaurant_id": "rest-1", "brand": "GRUBHUB", "quantity": 3,
            "special_instructions": "crispy",
            "options": [{"id": "size-18", "quantity": 1, "child_options": [{"id": "extra", "quantity": 2}]}],
        }])

    async def test_update_to_zero_removes(self):
        client = FakeCartClient()
        await self.run_tool(client, "update_cart_item", cart_id=CART, line_item_id="line-1", quantity=0)
        self.assertEqual(len(client.sent("DELETE", "/lines/line-1")), 1)
        self.assertEqual(client.sent("PUT", "/lines/line-1"), [])


class RankedOfferTests(unittest.TestCase):
    def test_minimum_met_largest_flat_discount_first(self):
        offers = {"available_offers": [
            {"entitlement_id": "a", "amount": {"value": 500, "type": "FLAT", "order_minimum": 3000}},
            {"entitlement_id": "b", "amount": {"value": 1000, "type": "FLAT", "order_minimum": 5000}},
            {"entitlement_id": "c", "amount": {"value": 900, "type": "PERCENT"}},
            {"entitlement_id": "d", "amount": {"value": 2000, "type": "FLAT", "order_minimum": 9000}},
            "unexpected", {"amount": {"value": 100}},
        ]}
        self.assertEqual(carts.ranked_offers(offers, 6000), ["b", "a", "c"])

    def test_unreadable_offers_rank_nothing(self):
        for offers in (None, "x", {"available_offers": "x"}):
            with self.subTest(offers=offers):
                self.assertEqual(carts.ranked_offers(offers, 6000), [])


class DeleteCartTests(unittest.IsolatedAsyncioTestCase):
    async def test_deletes_the_cart(self):
        client = FakeCartClient()
        tools: dict = {}
        cart_tools.register(SimpleNamespace(tool=lambda **_: lambda fn: tools.setdefault(fn.__name__, fn)))
        with patch("src.grubhub_mcp.tools.cart.get_client", return_value=client):
            out = json.loads(await tools["delete_cart"](cart_id=CART))
        self.assertEqual(out, {"deleted": CART})
        self.assertEqual(client.calls, [("DELETE", f"/carts/{CART}", None)])
