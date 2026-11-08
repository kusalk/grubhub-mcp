"""Payment management MCP tools."""

from __future__ import annotations

import json
from typing import Any

from mcp.server.fastmcp import FastMCP

from ..client import get_client, path_id


def register(mcp: FastMCP) -> None:
    @mcp.tool()
    async def get_payment_methods() -> str:
        """Get saved payment methods. Requires authentication."""
        client = get_client()
        if not client.session.is_authenticated or not client.session.diner_udid:
            return json.dumps({"error": "Must be logged in to view payment methods"})

        data = await client.get(
            f"/payments/{client.session.diner_udid}/payments"
        )
        return json.dumps(data, indent=2)

    @mcp.tool()
    async def get_gift_card_balance(card_number: str, pin: str) -> str:
        """Check the balance of a Grubhub gift card.

        Args:
            card_number: Gift card number
            pin: Gift card PIN
        """
        client = get_client()
        data = await client.post(
            "/gift_cards/balance",
            data={"card_number": card_number, "pin": pin},
        )
        return json.dumps(data, indent=2)

    @mcp.tool()
    async def apply_gift_card(cart_id: str, card_number: str, pin: str) -> str:
        """Apply a gift card to a cart.

        Args:
            cart_id: The cart ID
            card_number: Gift card number
            pin: Gift card PIN
        """
        client = get_client()
        data = await client.post(
            f"/carts/{path_id(cart_id)}/payments/gift_card",
            data={"card_number": card_number, "pin": pin},
        )
        return json.dumps(data, indent=2)
