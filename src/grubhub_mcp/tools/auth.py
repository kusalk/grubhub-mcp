"""Authentication MCP tools."""

from __future__ import annotations

from typing import Any

import httpx
from mcp.server.fastmcp import FastMCP

from ..client import get_client
from . import ADDITIVE, DESTRUCTIVE
from ..response import respond
from .. import auth as auth_module


def register(mcp: FastMCP) -> None:
    @mcp.tool(structured_output=False, annotations=DESTRUCTIVE)
    async def logout() -> str:
        """Log out of the current Grubhub session."""
        client = get_client()
        await auth_module.logout(client)
        return respond({"status": "logged_out"})

    @mcp.tool(structured_output=False, annotations=ADDITIVE)
    async def send_login_otp(email: str) -> str:
        """Send a one-time passcode to the given email for login."""
        client = get_client()
        data = await auth_module.send_otp(client, email)
        return respond({"status": "otp_sent", "email": email})

    @mcp.tool(structured_output=False, annotations=ADDITIVE)
    async def verify_login_otp(email: str, code: str) -> str:
        """Verify a one-time passcode and complete login."""
        client = get_client()
        try:
            data = await auth_module.verify_otp(client, email, code)
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 401:
                return respond(
                    {"error": "OTP expired or invalid — request a new code with send_login_otp"}
                )
            raise
        return respond(
            {
                "status": "authenticated",
                "diner_udid": client.session.diner_udid,
            },
        )
