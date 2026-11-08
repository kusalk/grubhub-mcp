"""Payment management MCP tools."""

from __future__ import annotations

from typing import Any

import httpx
from mcp.server.fastmcp import FastMCP

from ..client import get_client, path_id
from . import ADDITIVE, READ_ONLY
from .. import carts
from ..response import respond
from .cart import cart_view


async def _gift_cards(client: Any) -> Any:
    """The account's redeemed gift cards, or None if Grubhub won't say."""
    try:
        return await client.get(f"/codes/vault/{client.session.diner_udid}/giftcards")
    except (httpx.HTTPError, ValueError):
        return None


def register(mcp: FastMCP) -> None:
    @mcp.tool(structured_output=False, annotations=READ_ONLY)
    async def get_payment_methods() -> str:
        """Get saved payment methods. Requires authentication."""
        client = get_client()
        if not client.session.is_authenticated or not client.session.diner_udid:
            return respond({"error": "Must be logged in to view payment methods"})

        data = await client.get(
            f"/payments/{client.session.diner_udid}/payments"
        )
        return respond(data, exclude=frozenset({"nonce"}))  # A card payment token; the model never needs it.

    @mcp.tool(structured_output=False, annotations=READ_ONLY)
    async def get_grubhub_credits() -> str:
        """List the account's Grubhub credits and gift card balance. Requires authentication.

        Credit balances are in cents (1000 = $10.00). Each credit's restrictions
        say when it expires and whether it is limited to delivery orders.
        gift_cards holds redeemed gift cards; these tools can't spend them at
        checkout.
        """
        client = get_client()
        if not client.session.is_authenticated or not client.session.diner_udid:
            return respond({"error": "Must be logged in to view credits"})

        data = await client.get(f"/grubcash/{client.session.diner_udid}/list")
        gift_cards = await _gift_cards(client)
        if isinstance(data, dict) and gift_cards is not None:
            data = {**data, "gift_cards": gift_cards}
        return respond(data)

    @mcp.tool(structured_output=False, annotations=ADDITIVE)
    async def redeem_gift_card(code: str) -> str:
        """Redeem a Grubhub gift card code into the account. Requires authentication.

        Returns Grubhub's response and the account's gift cards afterwards.
        These tools can't spend gift card balance at checkout.

        Args:
            code: The gift card code, as printed (spaces are fine)
        """
        client = get_client()
        if not client.session.is_authenticated or not client.session.diner_udid:
            return respond({"error": "Must be logged in to redeem gift cards"})

        redeemed = await client.post(f"/codes/vault/{client.session.diner_udid}", data={"code_text": code.strip()})
        return respond({"redeemed": redeemed, "gift_cards": await _gift_cards(client)})

    @mcp.tool(structured_output=False, annotations=ADDITIVE)
    async def apply_grubhub_credit(cart_id: str, credit_id: str) -> str:
        """Apply a Grubhub credit to a cart, and return the cart. Requires authentication.

        Args:
            cart_id: The cart ID
            credit_id: The credit's id from get_grubhub_credits
        """
        client = get_client()
        if not client.session.is_authenticated or not client.session.diner_udid:
            return respond({"error": "Must be logged in to apply credits"})

        # The credit's source_type must accompany its id, so look it up rather
        # than trusting the caller; this also rejects ids from other accounts.
        credits = await client.get(f"/grubcash/{client.session.diner_udid}/list")
        credit = next(
            (c for c in credits.get("lines_of_credit") or [] if c.get("id") == credit_id),
            None,
        )
        if credit is None:
            return respond({"error": f"No credit with id {credit_id!r} on this account"})

        await client.post(
            f"/carts/{path_id(cart_id)}/payments",
            data={
                "payment_id": credit_id,
                "type": "GRUBCASH",
                "metadata": {"source_type": credit["source_type"]},
            },
        )
        return cart_view(await carts.get_bill(client, cart_id))
