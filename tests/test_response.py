from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from src.grubhub_mcp import response
from src.grubhub_mcp.response import respond
from src.grubhub_mcp.tools.restaurant import filter_menu


def money(cents: int) -> dict:
    return {"amount": cents, "currency": "USD", "styled_text": {"text": f"${cents / 100:.2f}", "text_style": "BOLD"}}


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
