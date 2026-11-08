"""Grubhub authentication flows."""

from __future__ import annotations

from typing import Any

from .client import API_KEY, GrubhubClient


async def _complete_login(client: GrubhubClient, data: dict[str, Any], generation: str | None) -> None:
    async with client.session_transition():
        if client.session.login_generation != generation:
            raise ValueError("Session changed during login; please retry")
        client.session.set_authenticated(data)


async def create_anonymous_session(client: GrubhubClient) -> dict[str, Any]:
    """Create an anonymous session for unauthenticated browsing."""
    payload = {
        "brand": "GRUBHUB",
        "client_id": API_KEY,
        "scope": "anonymous",
    }
    data = await client.post("/auth/anon", data=payload, auth_required=False)
    async with client.session_transition():
        # A stale browsing process must not replace a newer authenticated login.
        if not client.session.is_authenticated:
            client.session.set_anonymous(data)
    return data


async def login(client: GrubhubClient, email: str, password: str) -> dict[str, Any]:
    """Authenticate with email and password."""
    payload = {
        "brand": "GRUBHUB",
        "client_id": API_KEY,
        "email": email,
        "password": password,
    }
    generation = client.session.login_generation
    data = await client.post("/auth/login", data=payload, auth_required=False)
    await _complete_login(client, data, generation)
    return data


async def logout(client: GrubhubClient) -> dict[str, Any]:
    """Log out and clear session."""
    generation = client.session.login_generation
    try:
        data = await client.post("/auth/logout", auth_required=True)
    except Exception:
        data = {}
    async with client.session_transition():
        # Do not erase a newer explicit login while logout was in flight.
        if client.session.login_generation == generation:
            client.session.clear(persist_logout=True)
    return data


async def get_session(client: GrubhubClient) -> dict[str, Any]:
    """Get current authenticated session info."""
    return await client.get("/session", auth_required=True)


async def send_otp(client: GrubhubClient, email: str) -> dict[str, Any]:
    """Send a one-time passcode for authentication."""
    # Ensure we have an anonymous session — Grubhub ties OTP to the bearer token
    if not client.session.auth_token:
        await create_anonymous_session(client)
    payload = {
        "brand": "GRUBHUB",
        "client_id": API_KEY,
        "email": email,
    }
    generation = client.session.login_generation
    data = await client.post("/auth/confirmation_code", data=payload, auth_required=True)
    # Capture csrf_token without overwriting a concurrently changed login.
    if "csrf_token" in data:
        async with client.session_transition():
            if client.session.login_generation != generation or not client.session.auth_token:
                raise ValueError("Login changed while sending OTP; request a new code")
            client.session.csrf_token = data["csrf_token"]
            client.session._save()
    return data


async def verify_otp(client: GrubhubClient, email: str, code: str) -> dict[str, Any]:
    """Verify OTP and authenticate."""
    if not client.session.auth_token:
        raise ValueError("No session found — call send_login_otp first")
    if not client.session.csrf_token:
        raise ValueError("No csrf_token found — call send_login_otp first")
    payload = {
        "brand": "GRUBHUB",
        "client_id": API_KEY,
        "email": email,
        "csrf_token": client.session.csrf_token,
        "confirmation_code": code,
    }
    generation = client.session.login_generation
    data = await client.put("/auth/confirmation_code", data=payload, auth_required=True)
    await _complete_login(client, data, generation)
    if not client.session.diner_udid:
        # OTP responses sometimes omit credential metadata; fall back to /session.
        generation = client.session.login_generation
        session_data = await get_session(client)
        async with client.session_transition():
            if client.session.login_generation != generation:
                raise ValueError("Login changed during authentication; please retry")
            client.session.update_tokens(session_data)
    return data


async def create_account(
    client: GrubhubClient,
    email: str,
    password: str,
    first_name: str,
    last_name: str,
) -> dict[str, Any]:
    """Create a new Grubhub account."""
    payload = {
        "brand": "GRUBHUB",
        "client_id": API_KEY,
        "email": email,
        "password": password,
        "first_name": first_name,
        "last_name": last_name,
    }
    generation = client.session.login_generation
    data = await client.post("/credentials", data=payload, auth_required=False)
    await _complete_login(client, data, generation)
    return data


async def get_account_details(client: GrubhubClient) -> dict[str, Any]:
    """Get account details for the current user."""
    if not client.session.diner_udid:
        return {"error": "Not authenticated"}
    return await client.get(f"/credentials/{client.session.diner_udid}")

