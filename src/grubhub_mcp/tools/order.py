"""Order management MCP tools."""

from __future__ import annotations

from typing import Any

import httpx
from mcp.server.fastmcp import FastMCP

from .. import carts
from ..client import get_client, path_id
from . import ADDITIVE, DESTRUCTIVE, READ_ONLY
from ..response import dollars, respond
from ..summaries import cart_summary, order_confirmation
from .cart import CART_EXCLUDE, to_cents


async def _fetch_order_history_raw(
    client: Any, page_size: int = 20, page_num: int = 1
) -> dict[str, Any]:
    """Fetch one page of order history via the diner ``search_listing`` endpoint.

    The legacy ``/diners/{id}/orders`` endpoint only ever returns the most recent
    ~25 orders and carries no pagination metadata, so the full history is
    unreachable through it. ``search_listing`` is what the Grubhub web app uses:
    it honors ``pageNum``/``pageSize`` and returns a ``pager`` with
    ``total_pages``. Results are normalized back to ``{"orders": [...]}`` so the
    rest of the module is unchanged, with the ``pager`` passed through.
    """
    # Validate page_size and page_num BEFORE making API calls
    if page_size <= 0:
        raise ValueError("page_size must be a positive integer")
    if page_num <= 0:
        raise ValueError("page_num must be a positive integer")
    data = await client.get(
        f"/diners/{client.session.diner_udid}/search_listing",
        params=[
            ("pageNum", page_num),
            ("pageSize", page_size),
            ("facet", "scheduled:false"),
            ("facet", "orderType:ALL"),
            ("includePartnerOrders", "true"),
            ("sorts", "default"),
        ],
    )
    orders = (data.get("results") or []) + (data.get("partner_results") or [])
    return {"orders": orders, "pager": data.get("pager") or {}}


def _require_authenticated(client: Any, action: str) -> str | None:
    if not client.session.is_authenticated or not client.session.diner_udid:
        return respond({"error": f"Must be logged in to {action}"})
    return None


async def _find_order_in_history(client: Any, order_id: str) -> dict[str, Any] | None:
    page = 1
    total_pages = 1
    while page <= total_pages:
        history = await _fetch_order_history_raw(client, page_size=20, page_num=page)
        for order in history.get("orders", []):
            if order.get("id") == order_id or order.get("group_id") == order_id:
                return order
        total_pages = (history.get("pager") or {}).get("total_pages") or 1
        page += 1
    return None


def _build_cart_payload_from_order(order: dict[str, Any]) -> dict[str, Any]:
    restaurants = order.get("restaurants") or []
    if not restaurants:
        raise ValueError("Order does not include restaurant metadata")

    charges = order.get("charges") or {}
    line_items = (charges.get("lines") or {}).get("line_items") or []
    if not line_items:
        raise ValueError("Order does not include reorderable line items")

    payload_line_items: list[dict[str, Any]] = []
    for item in line_items:
        menu_item_id = item.get("menu_item_id") or item.get("id")
        if not menu_item_id:
            raise ValueError("Order line item is missing menu_item_id")
        payload_item: dict[str, Any] = {
            "menu_item_id": str(menu_item_id),
            "quantity": item.get("quantity", 1),
        }
        if item.get("special_instructions"):
            payload_item["special_instructions"] = item["special_instructions"]
        if item.get("options"):
            payload_item["options"] = item["options"]
        payload_line_items.append(payload_item)

    fulfillment_info = order.get("fulfillment_info") or {}
    order_type = fulfillment_info.get("type", "DELIVERY")
    payload: dict[str, Any] = {
        "brand": "GRUBHUB",
        "restaurant_id": str(restaurants[0]["id"]),
        "line_items": payload_line_items,
        "order_type": order_type,
    }

    delivery_info = fulfillment_info.get("delivery_info") or {}
    address = delivery_info.get("address") or {}
    coordinates = address.get("coordinates") or {}
    latitude = coordinates.get("latitude")
    longitude = coordinates.get("longitude")
    if latitude is not None and longitude is not None:
        payload["location"] = {
            "latitude": latitude,
            "longitude": longitude,
        }

    return payload


def register(mcp: FastMCP) -> None:
    @mcp.tool(structured_output=False, annotations=DESTRUCTIVE, title="Place order (charges your card)")
    async def place_order(
        cart_id: str,
        expected_charge: float,
        payment_method_id: str | None = None,
    ) -> str:
        """Place the order and charge the card. Only call once the user has confirmed.

        First show the user get_cart's summary and ask about a tip. Pass the
        card_charge they confirmed as expected_charge. The order is placed only if
        Grubhub reports the cart ready and the card charge equals expected_charge
        to the cent; otherwise nothing is charged and the reason is returned.

        Args:
            cart_id: The cart ID
            expected_charge: The card_charge the user confirmed, in dollars
            payment_method_id: A saved card id from get_payment_methods
                (default: the most recently used card)
        """
        client = get_client()
        auth_error = _require_authenticated(client, "place an order")
        if auth_error:
            return auth_error
        card_id = payment_method_id or await carts.default_card(client)
        if not card_id:
            return respond({"error": "No usable saved card; add one in the Grubhub app."})
        await carts.ensure_card(client, cart_id, await carts.get_bill(client, cart_id), card_id)
        bill = await carts.get_bill(client, cart_id)
        summary = cart_summary(bill)
        charge = carts.card_charge(bill)
        if bill.get("state") != "READY_FOR_CHECKOUT" or bill.get("validation_errors") or not bill.get("checkout_token"):
            return respond({"error": "Not placed: Grubhub says the cart isn't ready to order.", "summary": summary})
        if charge != to_cents(expected_charge):
            shown = dollars(charge) if charge is not None else "unknown"
            return respond({
                "error": f"Not placed: the card charge is {shown}, not the confirmed {dollars(to_cents(expected_charge))}. "
                         "Confirm the current amount with the user.",
                "summary": summary,
            })
        order = await client.post(f"/carts/{path_id(cart_id)}/checkout", data={"checkout_token": bill["checkout_token"]})
        if isinstance(order, dict):
            order = {"summary": order_confirmation(order), **order}
        return respond(order, exclude=CART_EXCLUDE)

    @mcp.tool(structured_output=False, annotations=READ_ONLY)
    async def get_order(order_id: str) -> str:
        """Get details for a specific order.

        Args:
            order_id: The order ID
        """
        client = get_client()
        auth_error = _require_authenticated(client, "view order details")
        if auth_error:
            return auth_error
        try:
            data = await client.get(f"/orders/{path_id(order_id)}")
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code != 404:
                raise

            match = await _find_order_in_history(client, order_id)
            if match is None:
                raise
            data = match
        return respond(data)

    @mcp.tool(structured_output=False, annotations=READ_ONLY)
    async def get_order_history(page_size: int = 20, page_num: int = 1) -> str:
        """Get past order history (paginated). Requires authentication.

        Pagination is server-side via the ``search_listing`` endpoint, so the
        full history is reachable -- iterate ``page_num`` from 1 up to
        ``pagination.total_pages`` in the response.

        Args:
            page_size: Orders per page (default 20)
            page_num: 1-based page number (default 1)
        """
        client = get_client()
        auth_error = _require_authenticated(client, "view order history")
        if auth_error:
            return auth_error

        data = await _fetch_order_history_raw(
            client, page_size=page_size, page_num=page_num
        )
        pager = data.get("pager") or {}
        return respond(
            {
                "orders": data["orders"],
                "pagination": {
                    "page_size": page_size,
                    "page_num": page_num,
                    "returned": len(data["orders"]),
                    "total_pages": pager.get("total_pages"),
                    "current_page": pager.get("current_page"),
                },
            },
            too_large_hint="Use a smaller page_size.",
        )

    @mcp.tool(structured_output=False, annotations=READ_ONLY)
    async def track_order(order_id: str) -> str:
        """Get real-time tracking info for an active order.

        Args:
            order_id: The order ID to track
        """
        client = get_client()
        auth_error = _require_authenticated(client, "track an order")
        if auth_error:
            return auth_error
        data = await client.get(f"/orders/{path_id(order_id)}/tracking")
        return respond(data)

    @mcp.tool(structured_output=False, annotations=ADDITIVE)
    async def reorder(order_id: str) -> str:
        """Create a new cart from a previous order for easy reordering.

        Args:
            order_id: The order ID to reorder
        """
        client = get_client()
        auth_error = _require_authenticated(client, "reorder")
        if auth_error:
            return auth_error

        try:
            data = await client.post(f"/orders/{path_id(order_id)}/reorder")
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code != 404:
                raise

            match = await _find_order_in_history(client, order_id)
            if match is None:
                raise
            payload = _build_cart_payload_from_order(match)
            data = await client.post("/carts", data=payload)
        return respond(data)
