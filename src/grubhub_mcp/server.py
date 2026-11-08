"""Grubhub MCP Server — search restaurants, browse menus, manage cart, place orders."""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP

from .tools import auth, search, restaurant, cart, order, account, payments

mcp = FastMCP(
    "grubhub",
    instructions=(
        "Grubhub MCP server for food delivery orders. "
        "Find food with search_restaurants, then get_restaurant (fees, offers, menu) "
        "and get_menu_item (sizes and add-ons). Menu prices exclude tax and fees, "
        "which differ between restaurants, so compare restaurants on total cost. "
        "create_cart delivers to the user's saved Home address unless told otherwise; "
        "Grubhub+ and the best offer apply automatically after each cart change. "
        "Before ordering: show the user get_cart's summary, ask how much to tip "
        "(set_tip), offer any credits from get_grubhub_credits, and get the user's "
        "confirmation of card_charge. Then call place_order with that amount as "
        "expected_charge. Track orders with get_order. "
        "Login (send_login_otp, then verify_login_otp) is required for carts, "
        "orders and account tools; search and menus work without login."
    ),
)

# Register all tool modules
auth.register(mcp)
search.register(mcp)
restaurant.register(mcp)
cart.register(mcp)
order.register(mcp)
account.register(mcp)
payments.register(mcp)


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
