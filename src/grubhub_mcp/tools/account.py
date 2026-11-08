"""Account management MCP tools."""

from __future__ import annotations

import json
from typing import Any

from mcp.server.fastmcp import FastMCP

from ..client import get_client, path_id
from .. import auth as auth_module


def register(mcp: FastMCP) -> None:
    @mcp.tool()
    async def get_profile() -> str:
        """Get the current user's profile information. Requires authentication."""
        client = get_client()
        if not client.session.is_authenticated or not client.session.diner_udid:
            return json.dumps({"error": "Must be logged in to view profile"})

        data = await client.get(
            f"/diners/{client.session.diner_udid}/details",
            params={
                "with_addresses": True,
                "with_favorites": True,
                "with_diner_identity": True,
                "with_phone_numbers": True,
            },
        )
        return json.dumps(data, indent=2)

    @mcp.tool()
    async def update_profile(
        first_name: str | None = None,
        last_name: str | None = None,
        phone: str | None = None,
    ) -> str:
        """Update user profile information. Requires authentication.

        Args:
            first_name: New first name
            last_name: New last name
            phone: New phone number
        """
        client = get_client()
        if not client.session.is_authenticated or not client.session.diner_udid:
            return json.dumps({"error": "Must be logged in to update profile"})

        payload: dict[str, Any] = {}
        if first_name is not None:
            payload["first_name"] = first_name
        if last_name is not None:
            payload["last_name"] = last_name
        if phone is not None:
            payload["phone"] = phone

        data = await client.put(
            f"/credentials/{client.session.diner_udid}/profile",
            data=payload,
        )
        return json.dumps(data if data else {"status": "updated"}, indent=2)

    @mcp.tool()
    async def get_addresses() -> str:
        """Get saved delivery addresses. Requires authentication."""
        client = get_client()
        if not client.session.is_authenticated or not client.session.diner_udid:
            return json.dumps({"error": "Must be logged in to view addresses"})

        data = await client.get(
            f"/diners/{client.session.diner_udid}/addresses"
        )
        return json.dumps(data, indent=2)

    @mcp.tool()
    async def add_address(
        street_address: str,
        city: str,
        state: str,
        zip_code: str,
        apt_suite: str = "",
        delivery_instructions: str = "",
        label: str = "",
    ) -> str:
        """Add a new delivery address. Requires authentication.

        Args:
            street_address: Street address line
            city: City name
            state: State abbreviation (e.g. NY, CA)
            zip_code: ZIP code
            apt_suite: Apartment/suite number
            delivery_instructions: Special delivery instructions
            label: Label for the address (e.g. Home, Work)
        """
        client = get_client()
        if not client.session.is_authenticated or not client.session.diner_udid:
            return json.dumps({"error": "Must be logged in to add addresses"})

        payload: dict[str, Any] = {
            "street_address": street_address,
            "city": city,
            "state": state,
            "zip_code": zip_code,
        }
        if apt_suite:
            payload["unit"] = apt_suite
        if delivery_instructions:
            payload["delivery_instructions"] = delivery_instructions
        if label:
            payload["label"] = label

        data = await client.post(
            f"/diners/{client.session.diner_udid}/addresses",
            data=payload,
        )
        return json.dumps(data, indent=2)

    @mcp.tool()
    async def get_favorites() -> str:
        """Get favorite/saved restaurants. Requires authentication."""
        client = get_client()
        if not client.session.is_authenticated or not client.session.diner_udid:
            return json.dumps({"error": "Must be logged in to view favorites"})

        data = await client.get(
            f"/diners/{client.session.diner_udid}/favorites/restaurants"
        )
        return json.dumps(data, indent=2)

    @mcp.tool()
    async def add_favorite(restaurant_id: str) -> str:
        """Add a restaurant to favorites. Requires authentication.

        Args:
            restaurant_id: The restaurant ID to favorite
        """
        client = get_client()
        if not client.session.is_authenticated or not client.session.diner_udid:
            return json.dumps({"error": "Must be logged in"})

        data = await client.post(
            f"/diners/{client.session.diner_udid}/favorites/restaurants",
            data={"restaurant_id": int(restaurant_id)},
        )
        return json.dumps(data if data else {"status": "added"}, indent=2)

    @mcp.tool()
    async def remove_favorite(restaurant_id: str) -> str:
        """Remove a restaurant from favorites. Requires authentication.

        Args:
            restaurant_id: The restaurant ID to unfavorite
        """
        client = get_client()
        if not client.session.is_authenticated or not client.session.diner_udid:
            return json.dumps({"error": "Must be logged in"})

        data = await client.delete(
            f"/diners/{client.session.diner_udid}/favorites/{path_id(restaurant_id)}"
        )
        return json.dumps(data if data else {"status": "removed"}, indent=2)
