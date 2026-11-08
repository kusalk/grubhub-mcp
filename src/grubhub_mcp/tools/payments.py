"""Payment management MCP tools."""

from __future__ import annotations

import json
from typing import Any

from mcp.server.fastmcp import FastMCP

from ..client import get_client


def register(mcp: FastMCP) -> None:
    @mcp.tool()
    async def get_payment_methods() -> str:
        """Get saved payment methods. Requires authentication."""
        client = get_client()
        if not client.session.is_authenticated or not client.session.diner_udid:
            return json.dumps({"error": "Must be logged in to view payment methods"})

        data = await client.get(
            f"/payments/{client.session.diner_udid}/payments"
        )
        return json.dumps(data, indent=2)
