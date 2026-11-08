"""Order management MCP tools."""

from __future__ import annotations

from typing import Any

import httpx
from mcp.server.fastmcp import FastMCP

from .. import carts, tracking
from ..client import get_client, path_id
from . import ADDITIVE, DESTRUCTIVE, READ_ONLY
from ..response import dollars, respond
from ..summaries import cart_summary, order_confirmation
from .cart import CART_EXCLUDE, cart_view, to_cents


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
            if order_id in (order.get("id"), order.get("group_id"), str(order.get("order_number"))):
                return order
        total_pages = (history.get("pager") or {}).get("total_pages") or 1
        page += 1
    return None


def reorder_lines(order: dict[str, Any]) -> tuple[str, list[dict[str, Any]]]:
    """The restaurant and cart lines to rebuild a past order."""
    restaurants = order.get("restaurants") or []
    if not restaurants or not isinstance(restaurants[0], dict) or not restaurants[0].get("id"):
        raise ValueError("The order doesn't say which restaurant it came from")
    restaurant_id = str(restaurants[0]["id"])
    lines = []
    for item in carts.line_items(order):
        if item.get("menu_item_id"):
            lines.append(carts.line_body(
                restaurant_id, str(item["menu_item_id"]), item.get("quantity") or 1,
                carts.line_options(item.get("options")), item.get("special_instructions") or "",
            ))
    if not lines:
        raise ValueError("The order has no items to reorder")
    return restaurant_id, lines


def history_status(order: dict[str, Any]) -> dict[str, Any]:
    """Status from the order-history entry, used when the live feed is unavailable."""
    return {"state": order.get("order_status"), "expected_by": order.get("when_for")}


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
        """Get an order and its live delivery status.

        The response starts with tracking: the status (e.g. "Out for delivery",
        "Delivered"), the arrival window, whether it's on time, the delivery code
        the courier may ask for, and the courier's name, phone and location. If
        live tracking is unavailable, tracking holds the order-history status.

        Args:
            order_id: The order number, or the order's id or group_id from get_order_history
        """
        client = get_client()
        auth_error = _require_authenticated(client, "view order details")
        if auth_error:
            return auth_error
        order = await _find_order_in_history(client, order_id.strip())
        if order is None:
            return respond({"error": f"No order {order_id!r} in your order history."})
        tracking_status = await tracking.live_status(client, order.get("id")) or history_status(order)
        return respond({"tracking": tracking_status, **order}, exclude=CART_EXCLUDE)

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

    @mcp.tool(structured_output=False, annotations=ADDITIVE)
    async def reorder(order_id: str, address_id: str | None = None, include_utensils: bool = True) -> str:
        """Start a new cart with a past order's items, and return the cart.

        Items the restaurant no longer offers are skipped and listed under
        skipped_items. The cart is delivered like create_cart's.

        Args:
            order_id: The order number, or the order's id or group_id from get_order_history
            address_id: A saved address id from get_addresses (default: Home)
            include_utensils: Ask for napkins and utensils (default true)
        """
        client = get_client()
        auth_error = _require_authenticated(client, "reorder")
        if auth_error:
            return auth_error
        order = await _find_order_in_history(client, order_id.strip())
        if order is None:
            return respond({"error": f"No order {order_id!r} in your order history."})
        _, lines = reorder_lines(order)
        cart_id = await carts.create(client)
        try:
            await carts.set_delivery(client, cart_id, address_id, include_utensils)
        except Exception as exc:
            raise ValueError(f"Created cart {cart_id}, but setting its delivery address failed: {exc}") from exc
        skipped = []
        for line in lines:
            try:
                await carts.add_line(client, cart_id, line)
            except httpx.HTTPStatusError as exc:
                skipped.append({"menu_item_id": line["menu_item_id"], "reason": str(exc)})
        if len(skipped) == len(lines):
            return respond({"error": f"None of the order's items could be added to cart {cart_id}.", "skipped_items": skipped})
        bill = await carts.apply_benefits(client, cart_id)
        return cart_view({"skipped_items": skipped, **bill} if skipped else bill)
