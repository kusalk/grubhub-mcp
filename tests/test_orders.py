from __future__ import annotations

import json
import struct
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from src.grubhub_mcp import tracking
from src.grubhub_mcp.tools import order as order_tools
from test_carts import CART, FakeCartClient, rejected


def varint(value: int) -> bytes:
    encoded = b""
    while True:
        byte, value = value & 0x7F, value >> 7
        encoded += bytes([byte | (0x80 if value else 0)])
        if not value:
            return encoded


def field(number: int, value) -> bytes:
    """Encode one protobuf field, as Grubhub's website code does."""
    if isinstance(value, bool):
        value = int(value)
    if isinstance(value, int):
        return varint(number << 3) + varint(value)
    if isinstance(value, float):
        return varint(number << 3 | 1) + struct.pack("<d", value)
    data = value.encode() if isinstance(value, str) else value
    return varint(number << 3 | 2) + varint(len(data)) + data


def message(*fields: bytes) -> bytes:
    return b"".join(fields)


def feed(state: int = 8, delivered_at: str | None = None) -> bytes:
    """A synthetic OrderStatusPageModel with the fields get_order reads, plus unknown ones."""
    merchant = message(field(1, "Pizza Place"), field(11, "America/New_York"), field(40, "future field"))
    static = message(field(1, merchant), field(3, message(field(2, "12345"))))
    location = message(field(1, 40.75), field(2, -74.0), field(4, 1794200000000))
    courier = message(field(1, "Sam"), field(4, location), field(10, True), field(11, "5550100"))
    eta = message(
        field(1, "2026-11-08T04:58:00Z"), field(2, "2026-11-08T05:13:00Z"),
        field(3, "2026-11-08T04:50:00Z"), field(4, "2026-11-08T05:00:00Z"), field(5, 1),
        *([field(6, delivered_at)] if delivered_at else []),
    )
    dynamic = message(field(1, courier), field(2, eta), field(3, message(field(1, state))), field(4, "74"), field(99, 7))
    return message(field(1, message(field(1, static), field(2, dynamic))), field(2, b"\x0a\x03abc"))


class TrackingDecodeTests(unittest.TestCase):
    def test_reads_the_status_screen_fields(self):
        self.assertEqual(tracking.decode_status(feed()), {
            "state": "Out for delivery",
            "arriving": "11:58 PM–12:13 AM",
            "originally": "11:50 PM–12:00 AM",
            "timing": "On time",
            "delivered_at": None,
            "delivery_code": "74",
            "restaurant": "Pizza Place",
            "order_number": "12345",
            "courier": {"name": "Sam", "phone": "5550100", "contactable": True,
                        "location": {"latitude": 40.75, "longitude": -74.0, "updated": "2026-11-09T04:53:20+00:00"}},
        })

    def test_delivered_and_unknown_states(self):
        self.assertEqual(tracking.decode_status(feed(11, "2026-11-08T05:04:00Z"))["delivered_at"], "12:04 AM")
        self.assertEqual(tracking.decode_status(feed(99))["state"], "Unknown (99)")

    def test_malformed_feed_decodes_to_nothing(self):
        for data in (b"\x0a\xff", b"\x0f", b""):
            with self.subTest(data=data):
                self.assertEqual(tracking.decode_status(data), {})

    def test_request_identifies_the_order_by_its_cart_id(self):
        self.assertEqual(tracking.cart_id_for("3f6c2a1e-5b7d-4c89-9e0a-1d2b3c4d5e6f"), "P2wqHlt9TImeCh0rPE1ebw")
        self.assertEqual(tracking.encode_request("diner-1", "cart-x"), field(1, "diner-1") + field(2, "cart-x"))


HISTORY_ORDER = {
    "id": "3f6c2a1e-5b7d-4c89-9e0a-1d2b3c4d5e6f", "group_id": "3f6c2a1f-5b7d-4c89-9e0a-1d2b3c4d5e6f",
    "order_number": "100200300400500", "order_status": "PICKUP_READY", "when_for": "2026-11-08T04:53:00.000Z",
    "restaurants": [{"id": "rest-1", "name": "Pizza Place"}],
    "charges": {"lines": {"line_items": [
        {"menu_item_id": "item-1", "quantity": 2, "special_instructions": "well done",
         "options": [{"id": "size-18", "name": "Large", "quantity": 1, "child_options": []}]},
        {"menu_item_id": "gone", "quantity": 1, "options": []},
    ]}},
}


class FakeOrderClient(FakeCartClient):
    def __init__(self, feed_bytes: bytes | Exception = b"", **kwargs) -> None:
        super().__init__(**kwargs)
        self.feed_bytes = feed_bytes

    async def get(self, path, params=None, auth_required=True):
        if path == "/diners/diner-1/search_listing":
            self.calls.append(("GET", path, params))
            return {"results": [HISTORY_ORDER], "pager": {"total_pages": 1}}
        return await super().get(path, params, auth_required)

    async def post(self, path, data=None, auth_required=True, params=None):
        if path.endswith("/lines") and data.get("menu_item_id") == "gone":
            self.calls.append(("POST", path, data))
            raise rejected(path)
        return await super().post(path, data, auth_required, params)

    async def post_protobuf(self, path, body):
        self.calls.append(("PROTOBUF", path, body))
        if isinstance(self.feed_bytes, Exception):
            raise self.feed_bytes
        return self.feed_bytes


class OrderToolTests(unittest.IsolatedAsyncioTestCase):
    async def run_tool(self, client, name, **arguments) -> dict:
        tools: dict = {}
        order_tools.register(SimpleNamespace(tool=lambda **_: lambda fn: tools.setdefault(fn.__name__, fn)))
        with patch("src.grubhub_mcp.tools.order.get_client", return_value=client):
            return json.loads(await tools[name](**arguments))

    async def test_get_order_by_order_number_with_live_tracking(self):
        client = FakeOrderClient(feed_bytes=feed())
        out = await self.run_tool(client, "get_order", order_id="100200300400500")
        self.assertEqual(list(out)[0], "tracking")
        self.assertEqual((out["tracking"]["state"], out["tracking"]["delivery_code"]), ("Out for delivery", "74"))
        sent = [body for verb, _, body in client.calls if verb == "PROTOBUF"]
        self.assertEqual(sent, [tracking.encode_request("diner-1", "P2wqHlt9TImeCh0rPE1ebw")])

    async def test_get_order_falls_back_to_history_status(self):
        client = FakeOrderClient(feed_bytes=RuntimeError("feed down"))
        out = await self.run_tool(client, "get_order", order_id=HISTORY_ORDER["group_id"])
        self.assertEqual(out["tracking"], {"state": "PICKUP_READY", "expected_by": "2026-11-08T04:53:00.000Z"})

    async def test_unknown_order(self):
        out = await self.run_tool(FakeOrderClient(), "get_order", order_id="nope")
        self.assertIn("No order 'nope'", out["error"])

    async def test_reorder_rebuilds_the_cart_and_reports_skipped_items(self):
        client = FakeOrderClient()
        out = await self.run_tool(client, "reorder", order_id="100200300400500")
        lines = client.sent("POST", f"/carts/{CART}/lines")
        self.assertEqual(lines[0], {
            "menu_item_id": "item-1", "restaurant_id": "rest-1", "brand": "GRUBHUB", "quantity": 2,
            "special_instructions": "well done", "options": [{"id": "size-18", "quantity": 1}],
        })
        self.assertEqual([s["menu_item_id"] for s in out["skipped_items"]], ["gone"])
        self.assertEqual(len(client.sent("PUT", "/delivery_info")), 1)
        self.assertIn("summary", out)
