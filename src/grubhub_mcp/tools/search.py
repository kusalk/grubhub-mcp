"""Restaurant search and discovery MCP tools."""

from __future__ import annotations

import json
from typing import Any

from mcp.server.fastmcp import FastMCP

from ..client import get_client
from .. import auth as auth_module


def register(mcp: FastMCP) -> None:
    @mcp.tool()
    async def search_restaurants(
        latitude: float,
        longitude: float,
        query: str = "",
        page_size: int = 20,
        page_num: int = 1,
        sort_type: str = "",
        location_mode: str = "DELIVERY",
    ) -> str:
        """Search for restaurants near a location.

        Args:
            latitude: Latitude of the delivery address
            longitude: Longitude of the delivery address
            query: Optional search query (cuisine, restaurant name, dish)
            page_size: Number of results per page (default 20)
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
        return json.dumps(data, indent=2)
