"""Cart management MCP tools."""

from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import FastMCP
from pydantic import BaseModel, Field

from .. import carts
from ..client import get_client, path_id
from . import ADDITIVE, DESTRUCTIVE, READ_ONLY
from ..response import respond
from ..summaries import cart_summary

# Bookkeeping in cart and bill responses that doesn't affect the order.
CART_EXCLUDE = frozenset({
    "FORTER_TOKEN",
    "action_messages_metadata",
    "active_payment_types",
    "affiliate",
    "allowed_payment_types",
    "anchor",
    "available_payment_types",
    "badges",
    "cart_attributes",
    "checkout_token",
    "diner_id",
    "experiments",
    "fee_items",  # Itemized in the summary; the raw fees totals remain.
    "group_admin",
    "group_payment_mode",
    "group_type",
    "images",
    "item_tags",
    "multi_part_shared_cart_enabled",
    "notification_preferences",
    "order_request_uuid",
    "packaging_options",
    "payment_selection_list",
    "pnp_substitution_rule",
    "promotion_infos",  # Reported as zero_fee_progress in the summary.
    "silo_id",
    "update_metadata",
})


class CartOption(BaseModel):
    """A chosen option for a menu item, such as a size or a topping."""

    id: str = Field(description="The option's id from get_menu_item's choice_option_list")
    quantity: int = Field(default=1, ge=1)


def option_payload(options: list[CartOption] | None) -> list[dict[str, Any]]:
    return [option.model_dump() for option in options or []]


def cart_view(bill: Any) -> str:
    """The cart's bill, led by a plain-language summary."""
    if isinstance(bill, dict):
        bill = {"summary": cart_summary(bill), **bill}
    return respond(bill, exclude=CART_EXCLUDE)


def register(mcp: FastMCP) -> None:
    @mcp.tool(structured_output=False, annotations=ADDITIVE)
    async def create_cart(
        restaurant_id: str,
        menu_item_id: str,
        quantity: int = 1,
        options: list[CartOption] | None = None,
        special_instructions: str = "",
        address_id: str | None = None,
        include_utensils: bool = True,
    ) -> str:
        """Start a delivery cart with its first item, and return the cart.

        The cart is delivered to a saved address (Home unless address_id is given;
        see get_addresses) with that address's delivery instructions. Grubhub+ and
        the best restaurant offer the cart qualifies for are applied after every
        cart change.

        Args:
            restaurant_id: The restaurant to order from
            menu_item_id: The first item to add
            quantity: How many of the item (default 1)
            options: Chosen options from get_menu_item, including every required
                choice such as size
            special_instructions: Preparation notes for this item
            address_id: A saved address id from get_addresses (default: Home)
            include_utensils: Ask for napkins and utensils (default true)
        """
        client = get_client()
        if error := carts.login_error(client):
            return respond({"error": error})
        if quantity < 1:
            return respond({"error": "quantity must be at least 1"})
        cart_id = await carts.create(client)
        try:
            await carts.set_delivery(client, cart_id, address_id, include_utensils)
            await carts.add_line(client, cart_id, carts.line_body(
                restaurant_id, menu_item_id, quantity, option_payload(options), special_instructions,
            ))
        except Exception as exc:
            raise ValueError(
                f"Created cart {cart_id}, but setting it up failed: {exc}. "
                "Fix the request and use add_to_cart on this cart, or start again."
            ) from exc
        return cart_view(await carts.apply_benefits(client, cart_id))

    @mcp.tool(structured_output=False, annotations=READ_ONLY)
    async def get_cart(cart_id: str) -> str:
        """Get a cart and its bill.

        The response starts with a summary: whether it's ready to order, any
        problems, items (with their line_item_id), subtotal, fees, tax, tip,
        total, discounts and credits, card_charge (what place_order will charge
        the card), progress toward $0 fees, and the delivery address.

        Args:
            cart_id: The cart ID
        """
        client = get_client()
        return cart_view(await carts.get_bill(client, cart_id))

    @mcp.tool(structured_output=False, annotations=ADDITIVE)
    async def add_to_cart(
        cart_id: str,
        menu_item_id: str,
        quantity: int = 1,
        options: list[CartOption] | None = None,
        special_instructions: str = "",
    ) -> str:
        """Add an item from the cart's restaurant, and return the cart.

        Args:
            cart_id: The cart ID
            menu_item_id: The menu item to add
            quantity: How many of the item (default 1)
            options: Chosen options from get_menu_item, including every required
                choice such as size
            special_instructions: Preparation notes for this item
        """
        client = get_client()
        if quantity < 1:
            return respond({"error": "quantity must be at least 1"})
        restaurant_id = carts.restaurant_of(await carts.get_bill(client, cart_id))
        if not restaurant_id:
            return respond({"error": "This cart has no restaurant yet; start one with create_cart."})
        await carts.add_line(client, cart_id, carts.line_body(
            restaurant_id, menu_item_id, quantity, option_payload(options), special_instructions,
        ))
        return cart_view(await carts.apply_benefits(client, cart_id))

    @mcp.tool(structured_output=False, annotations=DESTRUCTIVE)
    async def update_cart_item(cart_id: str, line_item_id: str, quantity: int) -> str:
        """Change how many of an item the cart has (0 removes it), and return the cart.

        Args:
            cart_id: The cart ID
            line_item_id: The item's line_item_id from the cart
            quantity: The new quantity
        """
        client = get_client()
        await carts.update_line(client, cart_id, line_item_id, quantity)
        return cart_view(await carts.apply_benefits(client, cart_id))

    @mcp.tool(structured_output=False, annotations=DESTRUCTIVE)
    async def remove_from_cart(cart_id: str, line_item_id: str) -> str:
        """Remove an item from the cart, and return the cart.

        Args:
            cart_id: The cart ID
            line_item_id: The item's line_item_id from the cart
        """
        client = get_client()
        await carts.remove_line(client, cart_id, line_item_id)
        return cart_view(await carts.apply_benefits(client, cart_id))

    @mcp.tool(structured_output=False, annotations=DESTRUCTIVE)
    async def delete_cart(cart_id: str) -> str:
        """Delete a cart that won't be ordered.

        Args:
            cart_id: The cart ID
        """
        client = get_client()
        await client.delete(f"/carts/{path_id(cart_id)}")
        return respond({"deleted": cart_id})

    @mcp.tool(structured_output=False, annotations=ADDITIVE)
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
        return respond(data)

    @mcp.tool(structured_output=False, annotations=DESTRUCTIVE)
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
        return respond(data)
