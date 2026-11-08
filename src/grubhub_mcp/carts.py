"""Cart operations, performed the way Grubhub's website performs them.

Each request shape here matches what the website sends and was checked against
Grubhub's API. Responses are read defensively: an unexpected shape skips an
optional step (like applying an offer) rather than failing the whole action.
"""

from __future__ import annotations

import time
from typing import Any

import httpx

from .client import GrubhubClient, path_id

# Sent by the website when it creates a cart. IGNORE_MINIMUM_TIP_REQUIREMENT
# allows tips below Grubhub's suggested minimum.
CART_EXPERIMENTS = [
    "IGNORE_MINIMUM_TIP_REQUIREMENT",
    "LINEOPTION_ENHANCEMENTS",
    "ENABLE_BUNDLED_ORDER_PAYMENTS",
    "ROBOT_DELIVERY",
    "DRONE_DELIVERY",
]


def login_error(client: GrubhubClient) -> str | None:
    if not client.session.is_authenticated or not client.session.diner_udid:
        return "Log in first (send_login_otp, then verify_login_otp)."
    return None


async def create(client: GrubhubClient) -> str:
    created = await client.post(
        "/carts", data={"brand": "GRUBHUB", "experiments": CART_EXPERIMENTS, "cart_attributes": []},
    )
    cart_id = created.get("id") if isinstance(created, dict) else None
    if not cart_id:
        raise ValueError(f"Grubhub didn't return a cart id: {created!r}")
    return cart_id


async def set_delivery(
    client: GrubhubClient, cart_id: str, address_id: str | None = None, include_utensils: bool = True,
) -> None:
    """Deliver to a saved address, with the account's name and email."""
    addresses = (await client.get(f"/diners/{client.session.diner_udid}/addresses")).get("diner_addresses")
    address = pick_address(addresses if isinstance(addresses, list) else [], address_id)
    credential = (await client.get("/session")).get("credential") or {}
    location = address.get("address") or {}
    coordinates = location.get("coordinates") or {}
    await client.put(f"/carts/{path_id(cart_id)}/delivery_info", data={
        "address": {
            "region_code": location.get("region_code"),
            "address_lines": location.get("address_lines"),
            "coordinates": {
                "latitude": str(coordinates.get("latitude")),
                "longitude": str(coordinates.get("longitude")),
            },
            "administrative_area": location.get("administrative_area"),
            "locality": location.get("locality"),
            "postal_code": location.get("postal_code"),
            "cross_streets": location.get("cross_streets") or "",
        },
        "green_indicated": not include_utensils,  # The website's "No utensils" checkbox.
        "handoff_options": address.get("handoff_options") or [],
        "delivery_instructions": address.get("special_instructions") or "",
        "name": " ".join(filter(None, (credential.get("first_name"), credential.get("last_name")))),
        "email": credential.get("email"),
        "phone": address.get("phone") or credential.get("phone"),
    })


def pick_address(addresses: list[Any], address_id: str | None) -> dict[str, Any]:
    """The address with address_id, else the one labelled Home, else the first."""
    saved = [a for a in addresses if isinstance(a, dict)]
    if not saved:
        raise ValueError("No saved delivery addresses; add one with add_address.")
    if address_id:
        for address in saved:
            if address.get("id") == address_id:
                return address
        choices = ", ".join(f"{a.get('id')} ({a.get('label') or 'no label'})" for a in saved)
        raise ValueError(f"No saved address {address_id!r}; saved addresses: {choices}")
    return next((a for a in saved if str(a.get("label", "")).casefold() == "home"), saved[0])


def line_body(
    restaurant_id: str, menu_item_id: str, quantity: int,
    options: list[dict[str, Any]], special_instructions: str = "",
) -> dict[str, Any]:
    return {
        "menu_item_id": menu_item_id,
        "restaurant_id": restaurant_id,
        "brand": "GRUBHUB",
        "quantity": quantity,
        "special_instructions": special_instructions,
        "options": options,
    }


async def add_line(client: GrubhubClient, cart_id: str, body: dict[str, Any]) -> None:
    await client.post(f"/carts/{path_id(cart_id)}/lines", data=body)


async def get_bill(client: GrubhubClient, cart_id: str) -> dict[str, Any]:
    return await client.get(f"/carts/{path_id(cart_id)}/bill")


def line_items(bill: Any) -> list[dict[str, Any]]:
    charges = bill.get("charges") if isinstance(bill, dict) else None
    lines = (charges or {}).get("lines") or {}
    items = lines.get("line_items") if isinstance(lines, dict) else None
    return [item for item in items or [] if isinstance(item, dict)]


def restaurant_of(bill: Any) -> str | None:
    ids = bill.get("restaurant_ids") if isinstance(bill, dict) else None
    return str(ids[0]) if isinstance(ids, list) and ids else None


def line_options(options: Any) -> list[dict[str, Any]]:
    """A cart line's chosen options, in the shape Grubhub accepts when re-sending the line."""
    chosen = []
    for option in options if isinstance(options, list) else []:
        if isinstance(option, dict) and option.get("id"):
            entry = {"id": str(option["id"]), "quantity": option.get("quantity") or 1}
            if option.get("child_options"):
                entry["child_options"] = line_options(option["child_options"])
            chosen.append(entry)
    return chosen


async def update_line(client: GrubhubClient, cart_id: str, line_id: str, quantity: int) -> None:
    """Change a line's quantity; Grubhub needs the whole line re-sent. 0 removes it."""
    if quantity <= 0:
        await remove_line(client, cart_id, line_id)
        return
    line = next((item for item in line_items(await get_bill(client, cart_id)) if item.get("id") == line_id), None)
    if line is None:
        raise ValueError(f"No item {line_id!r} in cart {cart_id!r}; get_cart lists each item's line_item_id.")
    await client.put(
        f"/carts/{path_id(cart_id)}/lines/{path_id(line_id)}",
        data=line_body(
            str(line.get("restaurant_id")), str(line.get("menu_item_id")), quantity,
            line_options(line.get("options")), line.get("special_instructions") or "",
        ),
    )


async def remove_line(client: GrubhubClient, cart_id: str, line_id: str) -> None:
    await client.delete(f"/carts/{path_id(cart_id)}/lines/{path_id(line_id)}")


async def apply_benefits(client: GrubhubClient, cart_id: str) -> dict[str, Any]:
    """Apply Grubhub+ and the best restaurant offer, as the website does after each change.

    Returns the bill afterwards.
    """
    try:
        # Grubhub answers a repeat with already_exists, so this is safe after every change.
        await client.post(f"/carts/{path_id(cart_id)}/payments", data={
            "type": "PROMO_CODE", "payment_id": client.session.diner_udid, "metadata": {},
        })
    except httpx.HTTPStatusError:
        pass  # Not a Grubhub+ member; the cart works without it.
    bill = await get_bill(client, cart_id)
    if await _sync_offer(client, cart_id, bill):
        bill = await get_bill(client, cart_id)
    return bill


def ranked_offers(offers: Any, subtotal: int) -> list[str]:
    """Entitlement ids of offers whose minimum the subtotal meets, largest flat discount first."""
    ranked = []
    for offer in offers.get("available_offers") or [] if isinstance(offers, dict) else []:
        if not isinstance(offer, dict) or not offer.get("entitlement_id"):
            continue
        amount = offer.get("amount") if isinstance(offer.get("amount"), dict) else {}
        minimum = amount.get("order_minimum") if isinstance(amount.get("order_minimum"), int) else 0
        if minimum > subtotal:
            continue
        value = amount.get("value") if amount.get("type") == "FLAT" and isinstance(amount.get("value"), int) else 0
        ranked.append((value, offer["entitlement_id"]))
    return [entitlement for _, entitlement in sorted(ranked, key=lambda pair: -pair[0])]


async def _sync_offer(client: GrubhubClient, cart_id: str, bill: dict[str, Any]) -> bool:
    """Make the cart carry the best offer it qualifies for. Returns whether anything changed."""
    restaurant_id = restaurant_of(bill)
    delivery = ((bill.get("fulfillment_info") or {}).get("delivery_info") or {}) if isinstance(bill, dict) else {}
    subtotal = (bill.get("charges") or {}).get("diner_subtotal") if isinstance(bill, dict) else None
    if not restaurant_id or not isinstance(subtotal, int) or not delivery.get("latitude"):
        return False
    try:
        offers = await client.get(f"/offers/availability/{path_id(restaurant_id)}", params={
            "orderType": "STANDARD", "locationMode": "DELIVERY",
            "deliveryLatitude": delivery.get("latitude"), "deliveryLongitude": delivery.get("longitude"),
            "featureFlags": "includeLockedOffers",
        })
    except (httpx.HTTPError, ValueError):
        return False
    ranked = ranked_offers(offers, subtotal)
    payments = (bill.get("payments") or {}).get("UNIFIED_REWARD") or []
    applied = [p for p in payments if isinstance(p, dict)]
    if (applied and ranked and applied[0].get("payment_id") == ranked[0]) or (not applied and not ranked):
        return False
    for payment in applied:
        payment_id = payment.get("encoded_id") or payment.get("id")
        if not isinstance(payment_id, str):
            continue
        try:
            await client.delete(f"/carts/{path_id(cart_id)}/payments/{path_id(payment_id)}")
        except (httpx.HTTPStatusError, ValueError):
            pass
    for entitlement in ranked:
        try:
            await client.post(f"/carts/{path_id(cart_id)}/payments", data={
                "payment_id": entitlement, "type": "UNIFIED_REWARD", "cart_id": cart_id,
                "metadata": {"timestamp": int(time.time() * 1000)},
            })
            return True
        except httpx.HTTPStatusError:
            continue  # Grubhub decides final eligibility, e.g. first-order offers.
    return bool(applied)
