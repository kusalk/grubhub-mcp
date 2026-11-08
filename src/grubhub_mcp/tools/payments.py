"""Payment management MCP tools."""

from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import FastMCP

from ..client import get_client
from . import READ_ONLY
from ..response import respond


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
        return respond(data)
