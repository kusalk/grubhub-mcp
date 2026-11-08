"""Restaurant search and discovery MCP tools."""

from __future__ import annotations

import json
from typing import Any

from mcp.server.fastmcp import FastMCP

from ..client import get_client
from . import READ_ONLY
from .. import auth as auth_module
from ..response import FEE_NOISE, respond
from ..summaries import search_result_summary

# Ranking, ads and display bookkeeping in each search result.
SEARCH_EXCLUDE = FEE_NOISE | {
    "badge_list",
    "entity_tags",
    "features_v2",
    "highlighting_info",
    "non_supplemental_open_closed_container",
    "percentage_ad_fee",
    "quick_add_available",
    "quick_add_configuration",
    "quick_add_state",
    "rank",
    "rank_in_model",
    "rank_in_restaurant",
    "restaurant_aggregation_info",
    "restaurant_groups",
    "restaurant_hash",
    "routing_number",
    "sales_force_group",
    "score",
    "supplemental_open_closed_container",
    "tie_breaking_score",
    "uid",
    "vendor_location_id",
    "winning_ads_rank",
}


def add_summaries(data: Any) -> Any:
    """Put a plain-language summary first in each search result."""
    if isinstance(data, dict) and isinstance(data.get("results"), list):
        data["results"] = [
            {"summary": search_result_summary(result), **result} if isinstance(result, dict) else result
            for result in data["results"]
        ]
    return data


def register(mcp: FastMCP) -> None:
    @mcp.tool(structured_output=False, annotations=READ_ONLY)
    async def search_restaurants(
        latitude: float,
        longitude: float,
        query: str = "",
        page_size: int = 10,
        page_num: int = 1,
        sort_type: str = "",
        location_mode: str = "DELIVERY",
    ) -> str:
        """Search for restaurants near a location.

        Each result starts with a summary: Grubhub+ eligibility, opening hours,
        delivery estimate, fees and promotions. Menu prices exclude tax and these
        fees, which differ between restaurants, so compare restaurants on total
        cost. get_restaurant lists a restaurant's offers, and get_cart has
        the exact total for a cart.

        Args:
            latitude: Latitude of the delivery address
            longitude: Longitude of the delivery address
            query: Optional search query (cuisine, restaurant name, dish)
            page_size: Number of results per page (default 10)
            page_num: Page number for pagination (1-based, default 1)
            sort_type: Optional sort — leave empty for default relevance
            location_mode: DELIVERY or PICKUP (default DELIVERY)
        """
        client = get_client()
        if not client.session.auth_token:
            await auth_module.create_anonymous_session(client)

        params: dict[str, Any] = {
            "location": f"POINT({longitude} {latitude})",
            "locationMode": location_mode,
            "facetSet": "umamiV6",
            "pageSize": page_size,
            "pageNum": page_num,
            "hideHateos": "true",
            "searchMetrics": "true",
            "preciseLocation": "true",
            "sortSetId": "umami",
            "countOmittingTimes": "true",
        }
        if query:
            params["queryText"] = query
        if sort_type:
            params["sorts"] = json.dumps({"sortType": sort_type})

        data = await client.get("/restaurants/search/search_listing", params=params)
        return respond(
            add_summaries(data), exclude=SEARCH_EXCLUDE,
            too_large_hint="Request fewer results with a smaller page_size.",
        )
