# Grubhub MCP Server

An MCP (Model Context Protocol) server that provides programmatic access to Grubhub's food delivery platform. Search restaurants, browse menus, manage your cart, place orders, and track deliveries — all through MCP tools.

## Features

- **Search & Discovery** — Find restaurants by location, cuisine, or keyword
- **Restaurant & Menus** — View restaurant details, full menus, item options and pricing
- **Cart Management** — Create carts, add/remove items, apply promo codes, set tips
- **Order Flow** — Place orders, track deliveries in real-time, reorder past meals
- **Account Management** — Manage profile, saved addresses, and favorites
- **Payments** — View saved payment methods

### All 26 Tools

| Category | Tools |
|----------|-------|
| **Auth** | `logout`, `send_login_otp`, `verify_login_otp` |
| **Search** | `search_restaurants` |
| **Restaurant** | `get_restaurant`, `get_menu_item` |
| **Cart** | `create_cart`, `get_cart`, `add_to_cart`, `update_cart_item`, `remove_from_cart`, `apply_promo_code`, `set_tip` |
| **Order** | `place_order`, `get_order`, `get_order_history`, `track_order`, `reorder` |
| **Account** | `get_profile`, `update_profile`, `get_addresses`, `add_address`, `get_favorites`, `add_favorite`, `remove_favorite` |
| **Payments** | `get_payment_methods` |

## Installation

Install from a clone of this repository. The `grubhub-mcp` package on PyPI and
the upstream Claude Code plugin are published from the original project and
don't include this fork's changes.

Requires Python 3.11+.

```bash
git clone https://github.com/kusalk/grubhub-mcp.git
cd grubhub-mcp
uv venv && source .venv/bin/activate
uv pip install -e .
```

The install is editable, so the server runs whatever is checked out; restart it
after switching branches or pulling.

Then register it with Claude Code:

```bash
claude mcp add -s user grubhub -- /path/to/grubhub-mcp/.venv/bin/grubhub
```

## Usage with Other MCP Clients

Run the server over stdio:

```bash
/path/to/grubhub-mcp/.venv/bin/grubhub

# Or as a Python module, with the virtual environment active
python -m grubhub_mcp
```

## Authentication

**No login required** for browsing — search restaurants, view menus, and check prices without an account. The server automatically creates an anonymous session.

**Login required** for ordering, account management, and order history:

```
Log me in to Grubhub with a one-time code sent to my email
```

Login uses a one-time passcode (`send_login_otp`, then `verify_login_otp`), so your
password never passes through the model. Account creation and password changes
happen in the Grubhub app or website.

Sessions are stored in `~/.grubhub-mcp/session.json` (override the directory with
`GRUBHUB_SESSION_DIR`). Access tokens are refreshed on use, including before
expiry when Grubhub supplies expiry metadata and once after an authenticated
request returns 401. Rotated tokens are saved for the next server invocation.
Refresh responses that omit account metadata do not erase your login identity.
Concurrent refreshes are serialized within the client, and on POSIX systems
across server processes sharing the session directory. Session writes are atomic
and use private file permissions. Grubhub's lifetime fields are in minutes;
the observed access/refresh lifetimes are one hour / 30 days, but server policy
may change.
There is no background keep-alive: a revoked or expired refresh token still
requires a new login. Do not share your session file; it contains credentials.

## Tool Output

Tools return compact JSON shaped for language models:

- Images, search-ranking internals, experiment IDs and fee bookkeeping are
  removed wherever they appear, as are empty values and delivery/pickup prices
  identical to the plain price. Prices are shown as dollar strings (`"$18.95"`).
- `get_restaurant` returns the menu without option groups; `get_menu_item` has an
  item's sizes and add-ons. `menu_query` keeps only matching menu items.
- Output is limited to 80,000 characters. A larger menu drops item descriptions,
  and anything still too large returns an error explaining how to narrow the
  request.

## Order History Pagination

`get_order_history` uses server-side pagination so older orders are reachable.
`page_num` is **1-based** (default `1`), and `page_size` defaults to `20`.
Iterate from `1` through `pagination.total_pages` to retrieve the full history.
Both values must be positive. Order-detail and reorder fallbacks search across
all available history pages.

**Migration from v1.1.6:** callers using the old 0-based `page_num` must add one
to their page index. The response now provides `total_pages` and `current_page`
instead of the legacy `total_orders` value, which only counted a capped subset.

## How It Works

The API endpoints were reverse-engineered from the Grubhub Android app (v2026.11.1) by decompiling the APK with jadx and analyzing the Retrofit service interfaces, OkHttp interceptors, and data models.

Key technical details:
- **Base API**: `https://api-gtm.grubhub.com`
- **Auth**: Bearer token via one-time passcode or anonymous sessions
- **Transport**: REST/JSON for most endpoints, Protobuf for some BFF services
- **Headers**: Mimics the Android app's request headers including `x-gh-browser-id`

## Project Structure

```
src/grubhub_mcp/
├── __init__.py
├── __main__.py        # python -m entry point
├── server.py          # MCP server setup and tool registration
├── client.py          # HTTP client with auth and header management
├── auth.py            # Authentication flows (login, anonymous, OTP, refresh)
├── response.py        # Compact, size-limited tool output
└── tools/
    ├── auth.py        # Auth tools (OTP login, logout)
    ├── search.py      # Restaurant search
    ├── restaurant.py  # Restaurant details, menus, menu items
    ├── cart.py        # Cart CRUD, promo codes, tips
    ├── order.py       # Place orders, track, reorder, order history
    ├── account.py     # Profile, addresses, favorites
    └── payments.py    # Payment methods
```

## Development

Run the regression suite with:

```
uv run python -m unittest discover -s tests -v
```

CI runs tests on pushes and pull requests. The package requires MCP 1.28 or later 1.x (`mcp>=1.28,<2`).

## Disclaimer

This project is for educational and personal use. It is not affiliated with or endorsed by Grubhub. Use responsibly and in accordance with Grubhub's terms of service.

## License

MIT
