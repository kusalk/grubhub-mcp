from __future__ import annotations

import json
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import httpx

from src.grubhub_mcp import response
from src.grubhub_mcp.response import respond
from src.grubhub_mcp.summaries import restaurant_summary, search_result_summary
from src.grubhub_mcp.tools import restaurant as restaurant_tools
from src.grubhub_mcp.tools.restaurant import filter_menu
from src.grubhub_mcp.tools.search import add_summaries


def money(cents: int) -> dict:
    return {"amount": cents, "currency": "USD", "styled_text": {"text": f"${cents / 100:.2f}", "text_style": "BOLD"}}


def pricing_fee(kind: str, operand: str, amount: int, threshold: dict | None = None) -> dict:
    return {
        "fee_name": kind, "type": kind, "operand": operand, "amount": amount, "threshold": threshold,
        "non_adjusted_values": {"amount": amount}, "attribution": "GRUBHUB",
    }


SAMPLE_FEES = [
    pricing_fee("DELIVERY", "FLAT", 0),
    pricing_fee("DRIVER_BENEFITS_FEE", "FLAT", 0),
    pricing_fee("PRIORITY_DELIVERY", "FLAT", 299, {}),
    pricing_fee("SERVICE", "PERCENT", 1600, {"maximum_amount_for_percentage": 1200}),
    pricing_fee("SMALL", "FLAT", 200, {"threshold": 1000}),
    pricing_fee("TOLL", "FLAT", 200, {}),
]


class RespondTests(unittest.TestCase):
    def test_compact_with_dollar_strings_and_no_duplicate_prices(self):
        item = {
            "name": "Meat Lover", "price": money(1895),
            "delivery_price": money(1895), "pickup_price": money(1795),
        }
        text = respond(item)
        self.assertNotIn(" ", text.replace("Meat Lover", ""))
        self.assertEqual(json.loads(text), {"name": "Meat Lover", "price": "$18.95", "pickup_price": "$17.95"})

    def test_excluded_keys_dropped_at_any_depth(self):
        data = {"results": [{"name": "A", "media_image": {"url": "x"}, "nested": {"score": 1, "keep": 2}}]}
        shaped = json.loads(respond(data, exclude=frozenset({"score"})))
        self.assertEqual(shaped, {"results": [{"name": "A", "nested": {"keep": 2}}]})

    def test_empty_values_dropped_below_top_level_only(self):
        data = {"orders": [], "pager": {"total_pages": None, "current_page": 1, "tags": [], "note": ""}}
        self.assertEqual(json.loads(respond(data)), {"orders": [], "pager": {"current_page": 1}})

    def test_false_and_zero_are_kept(self):
        self.assertEqual(json.loads(respond({"x": {"open": False, "count": 0}})), {"x": {"open": False, "count": 0}})

    def test_unrecognized_money_shape_passes_through(self):
        data = {"price": {"amount": 1895, "currency": "USD", "new_field": True}}
        self.assertEqual(json.loads(respond(data)), data)

    def test_oversized_output_is_trimmed_then_refused(self):
        data = {"items": [{"name": "A", "description": "d" * 100}]}
        with patch.object(response, "MAX_CHARS", 80):
            trimmed = json.loads(respond(data, trim=frozenset({"description"}), trim_note="trimmed"))
            refused = json.loads(respond(data, too_large_hint="Narrow it."))
        self.assertEqual(trimmed, {"note": "trimmed", "items": [{"name": "A"}]})
        self.assertIn("Narrow it.", refused["error"])


class SummaryTests(unittest.TestCase):
    def test_search_summary_reports_all_fees(self):
        result = {
            "restaurant_tags": ["SUBSCRIPTION_ELIGIBLE_FOR_STANDARD_ORDER"],
            "open": True, "next_closed_at": "2026-11-08T09:45:00.000Z", "time_zone": "America/New_York",
            "delivery_time_estimate_lower_bound": 23, "delivery_time_estimate_upper_bound": 38,
            "delivery_minimum": {"price": 0, "currency": "USD"},
            "delivery_fee": {"price": 0, "currency": "USD"},
            "delivery_fee_without_discounts": {"price": 199, "currency": "USD"},
            "price_response": {
                "delivery_response": {"pricing_fees": SAMPLE_FEES},
                "promotions": [{"promotion_type": "SPEND_BASED_DISCOUNT", "spend_based_threshold": 5000}],
            },
        }
        self.assertEqual(search_result_summary(result), {
            "grubhub_plus": True,
            "open": True,
            "closes_at": "Sun 4:45 AM",
            "opens_at": None,
            "delivery_estimate": "23-38 min",
            "delivery_minimum": None,
            "fees": {
                "delivery": "$0.00 (normally $1.99)",
                "priority_delivery_optional": "$2.99",
                "service": "16% of subtotal, max $12.00, before Grubhub+ savings (see get_cart)",
                "small_order": "$2.00 on subtotals under $10.00",
                "toll": "$2.00",
            },
            "promotions": ["$0 fees on orders of $50.00+"],
        })

    def test_unknown_fee_and_promotion_types_still_reported(self):
        result = {"price_response": {
            "delivery_response": {"pricing_fees": [pricing_fee("NEW_FEE", "FLAT", 150)]},
            "promotions": [{"promotion_type": "SOMETHING_NEW"}],
        }}
        summary = search_result_summary(result)
        self.assertEqual(summary["fees"], {"new_fee": "$1.50"})
        self.assertEqual(summary["promotions"], ["SOMETHING_NEW"])

    def test_malformed_fee_entries_are_skipped(self):
        result = {"price_response": {
            "delivery_response": {"pricing_fees": [1, None, pricing_fee("TOLL", "FLAT", 200, {})]},
            "promotions": ["unexpected"],
        }}
        summary = search_result_summary(result)
        self.assertEqual(summary["fees"], {"toll": "$2.00"})
        self.assertEqual(summary["promotions"], [])

    def test_unreadable_response_yields_empty_summary(self):
        self.assertEqual(search_result_summary({"price_response": "unexpected"}), {})
        self.assertEqual(restaurant_summary({"restaurant_availability": "closed"}), {})

    def test_restaurant_summary(self):
        data = {
            "restaurant": {"restaurant_tags": ["ALL_ACCESS"]},
            "restaurant_availability": {
                "open": True, "time_zone_id": "America/New_York",
                "next_open_closed_info": {"next_cutoff_order_send_time_delivery": "2026-11-08T07:15:00.000Z"},
                "delivery_estimate_range_v2": {"minimum": 30, "maximum": 30},
                "order_minimum": money(1200), "sales_tax": 8.875,
                "delivery_fee": money(0), "delivery_fees": SAMPLE_FEES[3:4],
            },
        }
        self.assertEqual(restaurant_summary(data), {
            "grubhub_plus": False,
            "open": True,
            "delivery_orders_until": "Sun 2:15 AM",
            "delivery_estimate": "30 min",
            "order_minimum": "$12.00",
            "sales_tax": "8.875%",
            "fees": {"delivery": "$0.00", "service": "16% of subtotal, max $12.00"},
            "promotions": [],
            "offers": [],
            "rewards": [],
        })

    def test_restaurant_summary_lists_offers_and_rewards(self):
        data = {"restaurant_availability": {"time_zone_id": "America/New_York"}}
        offers = {
            "available_offers": [
                {"title": "$10 off", "description": "Offer valid on qualifying orders of $50 or more.",
                 "end_date": "2026-12-01T04:59:59.000Z", "amount": {"value": 1000}},
                {"title": "$5 off", "description": "Offer valid on first orders of $30 or more."},
                "unexpected", {"title": None},
            ],
            "available_campaigns": [{"title": "Spend $50, get $5", "description": "$50 to go", "progress": 0.0}],
        }
        summary = restaurant_summary(data, offers)
        self.assertEqual(summary["offers"], [
            "$10 off: Offer valid on qualifying orders of $50 or more. (ends Nov 30)",
            "$5 off: Offer valid on first orders of $30 or more.",
        ])
        self.assertEqual(summary["rewards"], ["Spend $50, get $5: $50 to go"])

    def test_unreadable_offers_leave_rest_of_summary(self):
        data = {"restaurant_availability": {"sales_tax": 8.875}}
        for offers in ("unexpected", {"available_offers": "x"}, [1, 2]):
            with self.subTest(offers=offers):
                summary = restaurant_summary(data, offers)
                self.assertEqual((summary["sales_tax"], summary["offers"]), ("8.875%", []))

    def test_search_summary_comes_first(self):
        data = add_summaries({"results": [{"name": "A", "open": False}, "not-a-dict"]})
        self.assertEqual(list(data["results"][0])[0], "summary")
        self.assertEqual(data["results"][1], "not-a-dict")


class MenuFilterTests(unittest.TestCase):
    def menu(self) -> dict:
        return {"restaurant": {"menu_category_list": [
            {"name": "Pizza Pies 18\"", "menu_item_list": [
                {"name": "Meat Lover Pizza Pie", "description": "sausage, pepperoni"},
                {"name": "Margherita", "description": "basil"},
            ]},
            {"name": "Pasta", "menu_item_list": [{"name": "Ziti", "description": "with Sausage"}]},
            {"name": "Drinks", "menu_item_list": [{"name": "Soda"}]},
        ]}}

    def names(self, data: dict) -> list[str]:
        return [i["name"] for c in data["restaurant"]["menu_category_list"] for i in c["menu_item_list"]]

    def test_matches_name_description_or_category_case_insensitively(self):
        self.assertEqual(self.names(filter_menu(self.menu(), "SAUSAGE")), ["Meat Lover Pizza Pie", "Ziti"])
        self.assertEqual(self.names(filter_menu(self.menu(), "pies 18")), ["Meat Lover Pizza Pie", "Margherita"])

    def test_empty_query_or_unexpected_shape_is_left_alone(self):
        self.assertEqual(filter_menu(self.menu(), ""), self.menu())
        for data in ({}, {"restaurant": {"menu_category_list": "x"}}, ["not", "a", "dict"]):
            with self.subTest(data=data):
                self.assertEqual(filter_menu(data, "meat"), data)


class FakeRestaurantClient:
    def __init__(self, offers: dict | Exception) -> None:
        self.session = SimpleNamespace(auth_token="token")
        self.offers = offers
        self.paths: list[str] = []

    async def get(self, path: str, params=None, auth_required: bool = True):
        self.paths.append(path)
        if path.startswith("/offers/"):
            if isinstance(self.offers, Exception):
                raise self.offers
            return self.offers
        return {"restaurant": {"name": "Pizza Place"}, "restaurant_availability": {"sales_tax": 8.875}}


class GetRestaurantTests(unittest.IsolatedAsyncioTestCase):
    async def call(self, client: FakeRestaurantClient, **coordinates) -> dict:
        tools: dict = {}
        mcp = SimpleNamespace(tool=lambda **_settings: lambda fn: tools.setdefault(fn.__name__, fn))
        restaurant_tools.register(mcp)
        with patch("src.grubhub_mcp.tools.restaurant.get_client", return_value=client):
            return json.loads(await tools["get_restaurant"]("1000001", **coordinates))

    async def test_offers_join_the_summary(self):
        client = FakeRestaurantClient({"available_offers": [{"title": "$10 off", "description": "On $50+."}]})
        data = await self.call(client, latitude=40.75, longitude=-74.0)
        self.assertEqual(data["summary"]["offers"], ["$10 off: On $50+."])
        self.assertEqual(data["restaurant"], {"name": "Pizza Place"})

    async def test_failed_offers_request_still_returns_restaurant(self):
        request = httpx.Request("GET", "https://api-gtm.grubhub.com/offers/availability/1000001")
        client = FakeRestaurantClient(httpx.ConnectError("down", request=request))
        data = await self.call(client, latitude=40.75, longitude=-74.0)
        self.assertNotIn("offers", data["summary"])
        self.assertEqual(data["summary"]["sales_tax"], "8.875%")

    async def test_offers_need_coordinates(self):
        client = FakeRestaurantClient({"available_offers": []})
        await self.call(client)
        self.assertEqual(client.paths, ["/restaurants/v4/1000001"])
