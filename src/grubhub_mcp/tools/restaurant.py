"""Restaurant and menu MCP tools."""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
from mcp.server.fastmcp import FastMCP

from ..client import get_client, path_id
from . import READ_ONLY
from .. import auth as auth_module
from ..response import FEE_NOISE, respond
from ..summaries import restaurant_summary

# Bookkeeping repeated on every menu item and option.
MENU_EXCLUDE = frozenset({
    "flexible_tax_fields",
    "flexible_tax_numeric_fields",
    "item_coupon",
    "lead_time_ms",
    "menu_category_id",
    "miscellaneous_taxes",
    "restaurant_id",
    "routing_tags",
    "sequence",
    "tax",
    "tax_category",
    "tax_rate",
    "uuid",
})

RESTAURANT_EXCLUDE = MENU_EXCLUDE | FEE_NOISE | {
    "aggregations",
    "allowable_order_types",
    "badge_list",
    "combinable_with_coupons",
    "data_bytes",
    "deleted",
    "gh_pos_only",
    "group_ids",
    "grouped_overrides_availability",
    "menu_category_name",  # Items are already nested under their category.
    "menu_data_bytes",
    "merchant_uuid",
    "order_tiers",
    "package_state_type_id",
    "restaurant_hash",
    "silo_id",
    "silo_name",
    "tax_engine_information",
    "template_type",
    "version_ids",
}

def filter_menu(data: Any, query: str) -> Any:
    """Keep only menu items whose name, description or category contains query."""
    restaurant = data.get("restaurant") if isinstance(data, dict) else None
    categories = restaurant.get("menu_category_list") if isinstance(restaurant, dict) else None
    if not query or not isinstance(categories, list):
        return data
    needle = query.casefold()
    kept = []
    for category in categories:
        items = category.get("menu_item_list") if isinstance(category, dict) else None
        if not isinstance(items, list):
            continue
        matches = [
            item for item in items
            if isinstance(item, dict) and needle in " ".join(
                str(text) for text in (item.get("name"), item.get("description"), category.get("name")) if text
            ).casefold()
        ]
        if matches:
            kept.append({**category, "menu_item_list": matches})
    restaurant["menu_category_list"] = kept
    return data


def with_summary(data: Any, offers: Any = None) -> Any:
    return {"summary": restaurant_summary(data, offers), **data} if isinstance(data, dict) else data


async def fetch_offers(client: Any, restaurant_id: str, latitude: float, longitude: float) -> Any:
    """The restaurant's offers, or None if they can't be fetched."""
    try:
        return await client.get(
            f"/offers/availability/{path_id(restaurant_id)}",
            params={
                "orderType": "STANDARD",
                "locationMode": "DELIVERY",
                "deliveryLatitude": latitude,
                "deliveryLongitude": longitude,
                "featureFlags": "includeLockedOffers",
            },
        )
    except (httpx.HTTPError, ValueError):
        return None


def register(mcp: FastMCP) -> None:
    @mcp.tool(structured_output=False, annotations=READ_ONLY)
    async def get_restaurant(
        restaurant_id: str,
        latitude: float | None = None,
        longitude: float | None = None,
        order_type: str = "standard",
        menu_query: str = "",
    ) -> str:
        """Get a restaurant's details, fees, offers and menu.

        The response starts with a summary: Grubhub+ eligibility, hours, delivery
        estimate, fees, promotions and, given latitude and longitude, the
        restaurant's offers. Offers discount the subtotal; first-order offers apply
        only if the account hasn't ordered from the restaurant before. Menu prices
        exclude tax and fees. An item priced $0.00 takes its price from a required
        option such as size, and minimum_price_variation is its starting price.
        Use get_menu_item for an item's sizes and options.

        Args:
            restaurant_id: The Grubhub restaurant ID
            latitude: Latitude of the delivery address, for fees, estimates and offers
            longitude: Longitude of the delivery address
            order_type: Order type - standard, catering (default standard)
            menu_query: Keep only menu items whose name, description or category
                contains this text (e.g. "pizza"); use it when the menu is too large
        """
        client = get_client()
        if not client.session.auth_token:
            await auth_module.create_anonymous_session(client)

        params: dict[str, Any] = {
            "orderType": order_type,
            "hideUnavailableMenuItems": True,
            "hideChoiceCategories": True,
        }
        path = f"/restaurants/v4/{path_id(restaurant_id)}"
        offers = None
        if latitude is not None and longitude is not None:
            params["location"] = f"POINT({longitude} {latitude})"
            data, offers = await asyncio.gather(
                client.get(path, params=params),
                fetch_offers(client, restaurant_id, latitude, longitude),
            )
        else:
            data = await client.get(path, params=params)
        return respond(
            with_summary(filter_menu(data, menu_query), offers), exclude=RESTAURANT_EXCLUDE,
            trim=frozenset({"description"}),
            trim_note="Item descriptions omitted to fit; pass menu_query to see them for matching items.",
            too_large_hint="Pass menu_query to return only matching menu items.",
        )

    @mcp.tool(structured_output=False, annotations=READ_ONLY)
    async def get_menu_item(
        restaurant_id: str,
        item_id: str,
        order_type: str = "standard",
        latitude: float | None = None,
        longitude: float | None = None,
    ) -> str:
        """Get a menu item's sizes, options, add-ons and prices.

        Option groups (choice_category_list) with min_choice_options of 1 or more
        are required. Pass the chosen option ids to create_cart or add_to_cart as
        options [{id, quantity}].

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
        if latitude is not None and longitude is not None:
            params["location"] = f"POINT({longitude} {latitude})"

        data = await client.get(
            f"/restaurants/v4/{path_id(restaurant_id)}/menu_items/{path_id(item_id)}",
            params=params,
        )
        return respond(data, exclude=MENU_EXCLUDE)
