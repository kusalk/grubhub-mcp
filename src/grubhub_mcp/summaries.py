"""Plain-language summaries derived from Grubhub's raw fields.

A summary sits beside the raw data it was derived from. Any field it can't
read is left out rather than guessed; get_cart has the exact total.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from .response import dollars

GRUBHUB_PLUS_TAG = "SUBSCRIPTION_ELIGIBLE_FOR_STANDARD_ORDER"

_FEE_LABELS = {
    "DRIVER_BENEFITS_FEE": "driver_benefits",
    "PRIORITY_DELIVERY": "priority_delivery_optional",
    "SERVICE": "service",
    "SMALL": "small_order",
    "TOLL": "toll",
}


def search_result_summary(result: dict[str, Any]) -> dict[str, Any]:
    """Summarize one search_listing result."""
    try:
        price = result.get("price_response") or {}
        time_zone = result.get("time_zone")
        is_open = result.get("open")
        grubhub_plus = GRUBHUB_PLUS_TAG in (result.get("restaurant_tags") or [])
        return {
            "grubhub_plus": grubhub_plus,
            "open": is_open,
            "closes_at": _local_time(result.get("next_closed_at"), time_zone) if is_open else None,
            "opens_at": None if is_open else _local_time(result.get("next_open_at"), time_zone),
            "delivery_estimate": _minutes(
                result.get("delivery_time_estimate_lower_bound"),
                result.get("delivery_time_estimate_upper_bound"),
            ),
            "delivery_minimum": _positive_dollars(_cents(result.get("delivery_minimum"))),
            "fees": _fees(
                (price.get("delivery_response") or {}).get("pricing_fees"),
                _cents(result.get("delivery_fee")),
                _cents(result.get("delivery_fee_without_discounts")),
                grubhub_plus,
            ),
            "promotions": _promotions(price.get("promotions")),
        }
    except Exception:  # A summary must never break the tool; the raw data is still returned.
        return {}


def restaurant_summary(data: dict[str, Any], offers: Any = None) -> dict[str, Any]:
    """Summarize a /restaurants/v4 response and, if given, its /offers/availability response."""
    try:
        restaurant = data.get("restaurant") or {}
        availability = data.get("restaurant_availability") or {}
        time_zone = availability.get("time_zone_id")
        estimate = availability.get("delivery_estimate_range_v2") or {}
        cutoff = (availability.get("next_open_closed_info") or {}).get("next_cutoff_order_send_time_delivery")
        sales_tax = availability.get("sales_tax")
        grubhub_plus = GRUBHUB_PLUS_TAG in (restaurant.get("restaurant_tags") or [])
        return {
            "grubhub_plus": grubhub_plus,
            "open": availability.get("open"),
            "delivery_orders_until": _local_time(cutoff, time_zone),
            "delivery_estimate": _minutes(estimate.get("minimum"), estimate.get("maximum")),
            "order_minimum": _positive_dollars(_cents(availability.get("order_minimum"))),
            "sales_tax": f"{sales_tax:g}%" if isinstance(sales_tax, (int, float)) else None,
            "fees": _fees(
                availability.get("delivery_fees"),
                _cents(availability.get("delivery_fee")),
                _cents(availability.get("delivery_fee_without_discounts")),
                grubhub_plus,
            ),
            "promotions": _promotions(availability.get("promotions")),
            "offers": _titled_lines(offers, "available_offers", time_zone),
            "rewards": _titled_lines(offers, "available_campaigns", time_zone),
        }
    except Exception:  # A summary must never break the tool; the raw data is still returned.
        return {}


def order_confirmation(order: dict[str, Any]) -> dict[str, Any]:
    """Summarize a /carts/{id}/checkout response: the cart as placed."""
    try:
        estimate = ((order.get("ordering_info") or {}).get("real_time_eta") or {}).get("estimate_range") or {}
        cards = ((order.get("payments") or {}).get("CREDIT_CARD")) or []
        charged = [p.get("amount") for p in cards if isinstance(p, dict) and type(p.get("amount")) is int]
        return {
            "placed": order.get("state") == "CHECKOUT_COMPLETE",
            "state": order.get("state"),
            "status": order.get("status"),
            "order_number": order.get("order_number"),
            "arriving_in": _minutes(estimate.get("start_time_minutes"), estimate.get("end_time_minutes")),
            "arriving_between": [estimate.get("start_time"), estimate.get("end_time")]
            if estimate.get("start_time") and estimate.get("end_time") else None,
            "total": _money((order.get("charges") or {}).get("diner_grand_total")),
            "charged_to_card": dollars(sum(charged)) if charged else None,
            "next": "Track it with get_order and this order_number.",
        }
    except Exception:  # A summary must never break the tool; the raw data is still returned.
        return {}


_PAYMENT_LABELS = {
    "GIFT_CARD": "Gift card",
    "GRUBCASH": "Grubhub credit",
    "PROMO_CODE": "Promo code",
    "UNIFIED_REWARD": "Restaurant offer",
}

_PROBLEMS = {
    "fulfillment_info:invalid-fulfillmentinforequired": "No delivery address.",
    "lines:invalid-required": "The cart has no items.",
    "payments:invalid-amountnotmet": "No card on the cart yet; place_order adds it.",
    "payments:invalid-required": "No card on the cart yet; place_order adds it.",
}


def cart_summary(bill: dict[str, Any]) -> dict[str, Any]:
    """Summarize a /carts/{id}/bill response.

    card_charge is the total minus discounts and credits: what place_order
    charges the card.
    """
    try:
        charges = bill.get("charges") or {}
        fees = charges.get("fees") or {}
        subtotal, total = charges.get("diner_subtotal"), charges.get("diner_grand_total")
        discounts, discount_cents = [], 0
        for kind, payments in (bill.get("payments") or {}).items():
            amount = sum(p.get("amount") for p in payments if isinstance(p, dict) and type(p.get("amount")) is int)
            if kind != "CREDIT_CARD" and amount:
                discounts.append(f"{_PAYMENT_LABELS.get(kind, kind)}: -{dollars(amount)}")
                discount_cents += amount
        delivery = (bill.get("fulfillment_info") or {}).get("delivery_info") or {}
        address_lines = (delivery.get("address") or {}).get("address_lines")
        return {
            "ready_to_order": bill.get("state") == "READY_FOR_CHECKOUT",
            "problems": [_problem(e) for e in bill.get("validation_errors") or [] if isinstance(e, dict)],
            "items": [_cart_item(line) for line in ((charges.get("lines") or {}).get("line_items") or []) if isinstance(line, dict)],
            "subtotal": _money(subtotal),
            "fees": _cart_fees(fees.get("fee_items")),
            "tax": _money((charges.get("taxes") or {}).get("total")),
            "tip": _money((charges.get("tip") or {}).get("amount")),
            "total": _money(total),
            "discounts": discounts,
            "card_charge": dollars(total - discount_cents) if type(total) is int else None,
            "savings": _positive_dollars((bill.get("savings_breakdown") or {}).get("total_savings")),
            "zero_fee_progress": _zero_fee_progress(fees.get("promotion_infos"), subtotal),
            "deliver_to": ", ".join(address_lines) if isinstance(address_lines, list) else None,
            "delivery_instructions": delivery.get("delivery_instructions"),
            "utensils": None if "green_indicated" not in delivery else not delivery["green_indicated"],
        }
    except Exception:  # A summary must never break the tool; the raw data is still returned.
        return {}


def _problem(error: dict[str, Any]) -> str:
    key = f"{error.get('property')}:{error.get('message_key')}"
    return _PROBLEMS.get(key, key)


def _cart_item(line: dict[str, Any]) -> dict[str, Any]:
    options = [o.get("name") for o in line.get("options") or [] if isinstance(o, dict) and o.get("name")]
    return {
        "line_item_id": line.get("id"),
        "name": line.get("name"),
        "quantity": line.get("quantity"),
        "options": options,
        "price": _money(line.get("diner_total")),
        "notes": line.get("special_instructions"),
    }


def _cart_fees(items: Any) -> dict[str, str]:
    fees = {}
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict) or type(item.get("calculated_amount")) is not int:
            continue
        charged, normally = item["calculated_amount"], item.get("non_adjusted_amount")
        text = dollars(charged)
        if type(normally) is int and normally > charged:
            text += f" (normally {dollars(normally)})"
        fees[str(item.get("name") or item.get("type"))] = text
    return fees


def _zero_fee_progress(promotions: Any, subtotal: Any) -> str | None:
    for promotion in promotions if isinstance(promotions, list) else []:
        if not isinstance(promotion, dict):
            continue
        threshold = promotion.get("min_subtotal_threshold")
        if promotion.get("type") == "SPEND_BASED_DISCOUNT" and type(threshold) is int and type(subtotal) is int:
            if subtotal >= threshold:
                return "$0 fees on this order"
            return f"{dollars(threshold - subtotal)} more for $0 fees"
    return None


def _money(cents: Any) -> str | None:
    return dollars(cents) if type(cents) is int else None


def _titled_lines(offers: Any, key: str, time_zone: Any) -> list[str]:
    """One line per entry from Grubhub's own title and description, plus its end date."""
    if not isinstance(offers, dict) or not isinstance(offers.get(key), list):
        return []
    lines = []
    for entry in offers[key]:
        if not isinstance(entry, dict):
            continue
        text = ": ".join(str(part).strip() for part in (entry.get("title"), entry.get("description")) if part)
        if text:
            ends = _local_date(entry.get("end_date"), time_zone)
            lines.append(f"{text} (ends {ends})" if ends else text)
    return lines


def _fees(
    pricing_fees: list[dict[str, Any]] | None, delivery: int | None, undiscounted: int | None,
    grubhub_plus: bool = False,
) -> dict[str, str]:
    fees: dict[str, str] = {}
    if delivery is not None:
        fees["delivery"] = dollars(delivery)
        if undiscounted and undiscounted > delivery:
            fees["delivery"] += f" (normally {dollars(undiscounted)})"
    for fee in pricing_fees or []:
        if not isinstance(fee, dict):
            continue
        kind, amount = fee.get("type") or fee.get("fee_name"), fee.get("amount")
        if kind == "DELIVERY" or not amount:
            continue
        threshold = fee.get("threshold") or {}
        if fee.get("operand") == "PERCENT":
            text = f"{amount / 100:g}% of subtotal"
            if threshold.get("maximum_amount_for_percentage"):
                text += f", max {dollars(threshold['maximum_amount_for_percentage'])}"
            if grubhub_plus and kind == "SERVICE":
                text += ", before Grubhub+ savings (see get_cart)"
        else:
            text = dollars(amount)
            if threshold.get("threshold"):
                text += f" on subtotals under {dollars(threshold['threshold'])}"
        fees[_FEE_LABELS.get(kind, str(kind).lower())] = text
    return fees


def _promotions(promotions: list[dict[str, Any]] | None) -> list[str]:
    described = []
    for promotion in promotions or []:
        if not isinstance(promotion, dict):
            continue
        kind = promotion.get("promotion_type")
        threshold = promotion.get("spend_based_threshold")
        if kind == "SPEND_BASED_DISCOUNT" and threshold:
            described.append(f"$0 fees on orders of {dollars(threshold)}+")
        elif kind:
            described.append(str(kind))
    return described


def _cents(money: Any) -> int | None:
    """Search uses {"price": cents}; restaurant responses use {"amount": cents}."""
    if not isinstance(money, dict):
        return None
    value = money.get("price", money.get("amount"))
    return value if type(value) is int else None


def _positive_dollars(cents: int | None) -> str | None:
    return dollars(cents) if cents else None


def _minutes(low: Any, high: Any) -> str | None:
    if low and high:
        return f"{low}-{high} min" if low != high else f"{low} min"
    return f"{low or high} min" if low or high else None


def _local(timestamp: Any, time_zone: Any) -> datetime | None:
    if not isinstance(timestamp, str) or not isinstance(time_zone, str):
        return None
    try:
        return datetime.fromisoformat(timestamp.replace("Z", "+00:00")).astimezone(ZoneInfo(time_zone))
    except (KeyError, ValueError):  # ZoneInfoNotFoundError is a KeyError
        return None


def _local_time(timestamp: Any, time_zone: Any) -> str | None:
    moment = _local(timestamp, time_zone)
    return f"{moment:%a} {moment.hour % 12 or 12}:{moment:%M} {moment:%p}" if moment else None


def _local_date(timestamp: Any, time_zone: Any) -> str | None:
    moment = _local(timestamp, time_zone)
    return f"{moment:%b} {moment.day}" if moment else None
