"""Compact, model-friendly tool output from Grubhub API responses."""

from __future__ import annotations

import json
from typing import Any

# Claude Code truncates MCP tool output beyond roughly 25k tokens.
MAX_CHARS = 80_000

# Images, ranking internals and experiment bookkeeping, wherever they appear.
COMMON_EXCLUDE = frozenset({
    "additional_media_images",
    "cache_context",
    "dcp_response_info",
    "hit_tags",
    "intent_producer",
    "intent_search_context",
    "logo",
    "market_context",
    "media_image",
    "media_images",
    "query_expansion_context",
    "query_interpretation",
    "restaurant_cdn_image_url",
    "spellcheck_context",
    "sponsored_result",
    "text_style",
    "uprank_data",
    "variation_ids",
})

# Pricing-fee bookkeeping that doesn't change what the diner pays.
FEE_NOISE = frozenset({
    "attribution",
    "delivery_fee_allocation_info",
    "delivery_service_fee_allocation_info",
    "fee_adjustment_category",
    "fee_allocation_info",
    "fee_attribution",
    "fee_display_setting",
    "has_campus_subscription_specific_requirements",
    "non_adjusted_amount",
    "non_adjusted_values",
    "potential_adjustment",
    "potential_adjustment_values",
})


def respond(
    data: Any,
    *,
    exclude: frozenset[str] = frozenset(),
    too_large_hint: str = "",
    trim: frozenset[str] = frozenset(),
    trim_note: str = "",
) -> str:
    """Serialize data as compact JSON for the model.

    Excluded keys are dropped at any depth, so exclusions keep working if Grubhub
    reshapes a response. Empty values below the top level are dropped, as are
    delivery_/pickup_ variants equal to the plain field. Money objects become
    dollar strings. Output over MAX_CHARS also drops the trim keys and leads with
    trim_note; if it is still too large, it becomes an error with the hint.
    """
    text = _serialize(data, COMMON_EXCLUDE | exclude)
    if len(text) > MAX_CHARS and trim:
        text = _serialize(data, COMMON_EXCLUDE | exclude | trim, note=trim_note)
    if len(text) > MAX_CHARS:
        message = f"Response too large ({len(text):,} characters, limit {MAX_CHARS:,}). {too_large_hint}"
        return json.dumps({"error": message.strip()})
    return text


def _serialize(data: Any, exclude: frozenset[str], note: str = "") -> str:
    shaped = _shape(data, exclude, top=True)
    if note and isinstance(shaped, dict):
        shaped = {"note": note, **shaped}
    return json.dumps(shaped, separators=(",", ":"), ensure_ascii=False)


def dollars(cents: int | float) -> str:
    return f"${cents / 100:,.2f}"


def _money(value: dict[str, Any]) -> str | None:
    """{"amount": 1895, "currency": "USD", "styled_text": ...} becomes "$18.95"."""
    amount, currency = value.get("amount"), value.get("currency")
    if value.keys() <= {"amount", "currency", "styled_text"} and type(amount) is int and isinstance(currency, str):
        return dollars(amount) if currency == "USD" else f"{amount / 100:,.2f} {currency}"
    return None


def _is_empty(value: Any) -> bool:
    return value is None or value == "" or value == [] or value == {}


def _duplicates_sibling(parent: dict[str, Any], key: str) -> bool:
    for prefix in ("delivery_", "pickup_"):
        if key.startswith(prefix):
            base = key[len(prefix):]
            return base in parent and parent[base] == parent[key]
    return False


def _shape(value: Any, exclude: frozenset[str], top: bool = False) -> Any:
    if isinstance(value, list):
        shaped_items = (_shape(item, exclude) for item in value)
        return [item for item in shaped_items if not _is_empty(item)]
    if not isinstance(value, dict):
        return value
    if (money := _money(value)) is not None:
        return money
    shaped = {}
    for key, child in value.items():
        if key in exclude or _duplicates_sibling(value, key):
            continue
        child = _shape(child, exclude)
        if top or not _is_empty(child):
            shaped[key] = child
    return shaped
