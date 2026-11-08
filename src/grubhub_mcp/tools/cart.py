"""Cart management MCP tools."""

from __future__ import annotations

import json
from typing import Any

from mcp.server.fastmcp import FastMCP

from ..client import get_client, path_id
from . import ADDITIVE, DESTRUCTIVE, READ_ONLY


def register(mcp: FastMCP) -> None:
    @mcp.tool(annotations=ADDITIVE)
    async def create_cart(
        restaurant_id: str,
        menu_item_id: str,
        quantity: int = 1,
        special_instructions: str = "",
        options: list[dict[str, Any]] | None = None,
        latitude: float | None = None,
        longitude: float | None = None,
        is_delivery: bool = True,
    ) -> str:
        """Create a new cart with the first item.

        Args:
            restaurant_id: The restaurant ID to order from
            menu_item_id: The menu item ID to add
            quantity: Number of this item (default 1)
            special_instructions: Special preparation instructions
            options: List of selected options/add-ons, each with {id, quantity}
            latitude: Delivery latitude
            longitude: Delivery longitude
            is_delivery: True for delivery, False for pickup
        """
        client = get_client()

        line_item: dict[str, Any] = {
            "menu_item_id": menu_item_id,
            "quantity": quantity,
        }
        if special_instructions:
            line_item["special_instructions"] = special_instructions
        if options:
            line_item["options"] = options

        payload: dict[str, Any] = {
            "brand": "GRUBHUB",
            "restaurant_id": restaurant_id,
            "line_items": [line_item],
            "order_type": "DELIVERY" if is_delivery else "PICKUP",
        }
        if latitude is not None and longitude is not None:
            payload["location"] = {
                "latitude": latitude,
                "longitude": longitude,
            }

        data = await client.post("/carts", data=payload)
        return json.dumps(data, indent=2)

    @mcp.tool(annotations=READ_ONLY)
    async def get_cart(cart_id: str) -> str:
        """Get the current state of a cart including items, totals, and fees.

        Args:
            cart_id: The cart ID
        """
        client = get_client()
        data = await client.get(f"/carts/{path_id(cart_id)}")
        return json.dumps(data, indent=2)

    @mcp.tool(annotations=ADDITIVE)
    async def add_to_cart(
        cart_id: str,
        menu_item_id: str,
        quantity: int = 1,
        special_instructions: str = "",
        options: list[dict[str, Any]] | None = None,
    ) -> str:
        """Add an item to an existing cart.

        Args:
            cart_id: The cart ID to add to
            menu_item_id: The menu item ID to add
            quantity: Number of this item (default 1)
            special_instructions: Special preparation instructions
            options: List of selected options/add-ons, each with {id, quantity}
        """
        client = get_client()

        line_item: dict[str, Any] = {
            "menu_item_id": menu_item_id,
            "quantity": quantity,
        }
        if special_instructions:
            line_item["special_instructions"] = special_instructions
        if options:
            line_item["options"] = options

        data = await client.post(
            f"/carts/{path_id(cart_id)}/line_items", data=line_item
        )
        return json.dumps(data, indent=2)

    @mcp.tool(annotations=DESTRUCTIVE)
    async def update_cart_item(
        cart_id: str, line_item_id: str, quantity: int
    ) -> str:
        """Update the quantity of an item in the cart.

        Args:
            cart_id: The cart ID
            line_item_id: The line item ID to update
            quantity: New quantity (0 to remove)
        """
        client = get_client()
        data = await client.put(
            f"/carts/{path_id(cart_id)}/line_items/{path_id(line_item_id)}",
            data={"quantity": quantity},
        )
        return json.dumps(data, indent=2)

    @mcp.tool(annotations=DESTRUCTIVE)
    async def remove_from_cart(cart_id: str, line_item_id: str) -> str:
        """Remove an item from the cart.

        Args:
            cart_id: The cart ID
            line_item_id: The line item ID to remove
        """
        client = get_client()
        data = await client.delete(
            f"/carts/{path_id(cart_id)}/line_items/{path_id(line_item_id)}"
        )
        return json.dumps(data, indent=2)

    @mcp.tool(annotations=ADDITIVE)
    async def apply_promo_code(cart_id: str, promo_code: str) -> str:
        """Apply a promotion code to the cart.

        Args:
            cart_id: The cart ID
            promo_code: The promotion code to apply
        """
        client = get_client()
        data = await client.post(
            f"/carts/{path_id(cart_id)}/promotions",
            data={"promo_code": promo_code},
        )
        return json.dumps(data, indent=2)

    @mcp.tool(annotations=DESTRUCTIVE)
    async def set_tip(cart_id: str, tip_amount: float) -> str:
        """Set the tip amount on the cart.

        Args:
            cart_id: The cart ID
            tip_amount: Tip amount in dollars
        """
        client = get_client()
        data = await client.put(
            f"/carts/{path_id(cart_id)}/tip",
            data={"tip_amount": int(tip_amount * 100)},  # cents
        )
        return json.dumps(data, indent=2)
