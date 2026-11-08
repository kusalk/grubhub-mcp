from __future__ import annotations

from types import SimpleNamespace
import unittest

from src.grubhub_mcp.tools import account, auth, cart, order, payments, restaurant, search


def registered_tools() -> dict[str, dict]:
    tools: dict[str, dict] = {}

    def tool(**settings):
        def register(fn):
            tools[fn.__name__] = settings
            return fn
        return register

    for module in (account, auth, cart, order, payments, restaurant, search):
        module.register(SimpleNamespace(tool=tool))
    return tools


class ToolAnnotationTests(unittest.TestCase):
    def test_every_tool_is_annotated_and_open_world(self):
        for name, settings in registered_tools().items():
            with self.subTest(tool=name):
                self.assertIs(settings["annotations"].openWorldHint, True)

    def test_reads_are_read_only_and_writes_are_not(self):
        for name, settings in registered_tools().items():
            with self.subTest(tool=name):
                reads = name.startswith(("get_", "search_"))
                self.assertIs(settings["annotations"].readOnlyHint, reads)

    def test_place_order_is_destructive_and_says_it_charges(self):
        settings = registered_tools()["place_order"]
        self.assertIs(settings["annotations"].destructiveHint, True)
        self.assertIn("charges your card", settings["title"])
