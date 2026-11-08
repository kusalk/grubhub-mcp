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
