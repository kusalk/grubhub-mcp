"""Restaurant and menu MCP tools."""

from __future__ import annotations

import json
from typing import Any

from mcp.server.fastmcp import FastMCP

from ..client import get_client, path_id
from .. import auth as auth_module


def register(mcp: FastMCP) -> None:
    @mcp.tool()
    async def get_restaurant(
        restaurant_id: str,
        latitude: float | None = None,
        longitude: float | None = None,
        order_type: str = "standard",
    ) -> str:
        """Get restaurant details including menu, hours, ratings, and delivery info.

        Args:
            restaurant_id: The Grubhub restaurant ID
            latitude: Optional latitude for delivery estimates
            longitude: Optional longitude for delivery estimates
            order_type: Order type - standard, catering (default standard)
        """
        client = get_client()
        if not client.session.auth_token:
            await auth_module.create_anonymous_session(client)

        params: dict[str, Any] = {
            "orderType": order_type,
            "hideUnavailableMenuItems": True,
            "hideChoiceCategories": True,
        }
        if latitude and longitude:
            params["location"] = f"POINT({longitude} {latitude})"

        data = await client.get(
            f"/restaurants/v4/{path_id(restaurant_id)}", params=params
        )
        return json.dumps(data, indent=2)

    @mcp.tool()
    async def get_menu(
        restaurant_id: str,
        latitude: float | None = None,
        longitude: float | None = None,
    ) -> str:
        """Get the full menu for a restaurant including categories, items, and prices.

        This is the same as get_restaurant — the v4 endpoint returns the full menu.

        Args:
            restaurant_id: The Grubhub restaurant ID
            latitude: Optional latitude for availability
            longitude: Optional longitude for availability
        """
        client = get_client()
        if not client.session.auth_token:
            await auth_module.create_anonymous_session(client)

        params: dict[str, Any] = {
            "orderType": "standard",
            "hideUnavailableMenuItems": True,
        }
        if latitude and longitude:
            params["location"] = f"POINT({longitude} {latitude})"

        data = await client.get(
            f"/restaurants/v4/{path_id(restaurant_id)}", params=params
        )
        return json.dumps(data, indent=2)

    @mcp.tool()
    async def get_menu_item(
        restaurant_id: str,
        item_id: str,
        order_type: str = "standard",
        latitude: float | None = None,
        longitude: float | None = None,
    ) -> str:
        """Get details for a specific menu item including options, add-ons, and pricing.

        Args:
            restaurant_id: The Grubhub restaurant ID
            item_id: The menu item ID
            order_type: Order type - standard, catering (default standard)
            latitude: Optional latitude for availability
            longitude: Optional longitude for availability
        """
        client = get_client()
        if not client.session.auth_token:
            await auth_module.create_anonymous_session(client)

        params: dict[str, Any] = {
            "orderType": order_type,
            "hideUnavailableMenuItems": True,
        }
        if latitude and longitude:
            params["location"] = f"POINT({longitude} {latitude})"

        data = await client.get(
            f"/restaurants/v4/{path_id(restaurant_id)}/menu_items/{path_id(item_id)}",
            params=params,
        )
        return json.dumps(data, indent=2)
