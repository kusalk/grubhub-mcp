"""Authentication MCP tools."""

from __future__ import annotations

import json
from typing import Any

import httpx
from mcp.server.fastmcp import FastMCP

from ..client import get_client
from .. import auth as auth_module


def register(mcp: FastMCP) -> None:
    @mcp.tool()
    async def logout() -> str:
        """Log out of the current Grubhub session."""
        client = get_client()
        await auth_module.logout(client)
        return json.dumps({"status": "logged_out"})

    @mcp.tool()
    async def get_session_info() -> str:
        """Get current authentication state and session info."""
        client = get_client()
        return json.dumps(
            {
                "is_authenticated": client.session.is_authenticated,
                "diner_udid": client.session.diner_udid,
                "has_token": client.session.auth_token is not None,
            },
            indent=2,
        )

    @mcp.tool()
    async def send_login_otp(email: str) -> str:
        """Send a one-time passcode to the given email for login."""
        client = get_client()
        data = await auth_module.send_otp(client, email)
        return json.dumps({"status": "otp_sent", "email": email})

    @mcp.tool()
    async def verify_login_otp(email: str, code: str) -> str:
        """Verify a one-time passcode and complete login."""
        client = get_client()
        try:
            data = await auth_module.verify_otp(client, email, code)
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 401:
                return json.dumps(
                    {"error": "OTP expired or invalid — request a new code with send_login_otp"}
                )
            raise
        return json.dumps(
            {
                "status": "authenticated",
                "diner_udid": client.session.diner_udid,
            },
            indent=2,
        )

    @mcp.tool()
    async def send_password_reset(email: str) -> str:
        """Send a password reset OTP to the given email."""
        client = get_client()
        await auth_module.send_password_reset_otp(client, email)
        return json.dumps({"status": "reset_otp_sent", "email": email})
