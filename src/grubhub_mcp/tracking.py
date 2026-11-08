"""Live order status from Grubhub's order-tracking feed.

The website's order status page polls a protobuf endpoint. Field numbers and
names below come from the message definitions in Grubhub's website code
(postpurchasebff.order_tracking.v1 and order_status_domain.v1). Protobuf field
numbers are stable by design, and every field not listed here is skipped, so
new or reordered fields don't affect decoding.
"""

from __future__ import annotations

import base64
from datetime import datetime, timezone
import struct
from typing import Any
import uuid
from zoneinfo import ZoneInfo

FEED_PATH = "/postpurchasebff/bff/postpurchasebff.order_tracking.v1.OrderStatusPagePollRequest"

# order_tracking.v1 ORDER_TRACKING_STATE_* and ETA_STATE_* values.
TRACKING_STATES = {
    1: "Order scheduled", 2: "Order sent", 3: "Order confirmed", 4: "In the works", 5: "Ready for pickup",
    6: "Heading to store", 7: "Picking up order", 8: "Out for delivery", 9: "Arriving soon",
    10: "Dropoff arrived", 11: "Delivered", 12: "Canceled", 13: "Ready for self delivery",
}
ETA_STATES = {1: "On time", 2: "Early", 3: "Late", 4: "Behind", 5: "Ahead"}


def cart_id_for(order_uuid: str) -> str:
    """The feed identifies an order by its cart id: the order UUID's bytes, base64url-encoded."""
    return base64.urlsafe_b64encode(uuid.UUID(order_uuid).bytes).rstrip(b"=").decode()


def encode_request(diner_id: str, cart_id: str) -> bytes:
    """OrderStatusPagePollRequest {1: diner id, 2: cart id}."""
    return _string_field(1, diner_id) + _string_field(2, cart_id)


async def live_status(client: Any, order_uuid: Any) -> dict[str, Any] | None:
    """The order's live status, or None if the feed can't be read."""
    try:
        body = encode_request(client.session.diner_udid, cart_id_for(order_uuid))
        return decode_status(await client.post_protobuf(FEED_PATH, body)) or None
    except Exception:  # Tracking is a bonus: order history still has the status.
        return None


def decode_status(data: bytes) -> dict[str, Any]:
    """Read OrderStatusPageModel -> domainModel(1) -> {staticData(1), dynamicData(2)}."""
    try:
        domain = _message(_fields(data), 1)
        static, dynamic = _message(domain, 1), _message(domain, 2)
        merchant = _message(static, 1)
        courier, eta, tracking = _message(dynamic, 1), _message(dynamic, 2), _message(dynamic, 3)
        location = _message(courier, 4)
        zone = _string(merchant, 11)
        if not (_int(tracking, 1) or eta or _string(dynamic, 4)):
            return {}  # Nothing the status screen would show.
        return {
            "state": _label(TRACKING_STATES, _int(tracking, 1)),
            "arriving": _window(_string(eta, 1), _string(eta, 2), zone),
            "originally": _window(_string(eta, 3), _string(eta, 4), zone),
            "timing": _label(ETA_STATES, _int(eta, 5)),
            "delivered_at": _window(_string(eta, 6), None, zone),
            "delivery_code": _string(dynamic, 4),
            "restaurant": _string(merchant, 1),
            "order_number": _string(_message(static, 3), 2),
            "courier": {
                "name": _string(courier, 1),
                "phone": _string(courier, 11),
                "contactable": _bool(courier, 10),
                "location": {
                    "latitude": _double(location, 1),
                    "longitude": _double(location, 2),
                    "updated": _epoch_millis(_int(location, 4)),
                },
            },
        }
    except Exception:  # Malformed or unexpected data: the caller falls back to order history.
        return {}


Fields = dict[int, list[Any]]


def _varint(data: bytes, pos: int) -> tuple[int, int]:
    result = shift = 0
    while True:
        byte = data[pos]
        pos += 1
        result |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return result, pos
        shift += 7


def _fields(data: bytes) -> Fields:
    """Field number -> values: ints for varints, bytes for everything else."""
    fields: Fields = {}
    pos = 0
    while pos < len(data):
        key, pos = _varint(data, pos)
        number, wire = key >> 3, key & 7
        if wire == 0:
            value, pos = _varint(data, pos)
        elif wire == 1:
            value, pos = data[pos:pos + 8], pos + 8
        elif wire == 2:
            length, pos = _varint(data, pos)
            value, pos = data[pos:pos + length], pos + length
        elif wire == 5:
            value, pos = data[pos:pos + 4], pos + 4
        else:
            raise ValueError(f"unsupported wire type {wire}")
        if pos > len(data):
            raise ValueError("truncated message")
        fields.setdefault(number, []).append(value)
    return fields


def _first(fields: Fields, number: int) -> Any:
    values = fields.get(number)
    return values[0] if values else None


def _message(fields: Fields, number: int) -> Fields:
    value = _first(fields, number)
    return _fields(value) if isinstance(value, bytes) else {}


def _string(fields: Fields, number: int) -> str | None:
    value = _first(fields, number)
    return value.decode("utf-8") if isinstance(value, bytes) and value else None


def _int(fields: Fields, number: int) -> int | None:
    value = _first(fields, number)
    return value if isinstance(value, int) else None


def _bool(fields: Fields, number: int) -> bool | None:
    value = _int(fields, number)
    return None if value is None else bool(value)


def _double(fields: Fields, number: int) -> float | None:
    value = _first(fields, number)
    return struct.unpack("<d", value)[0] if isinstance(value, bytes) and len(value) == 8 else None


def _label(labels: dict[int, str], value: int | None) -> str | None:
    if not value:
        return None
    return labels.get(value, f"Unknown ({value})")


def _local(timestamp: str | None, zone: str | None) -> datetime | None:
    if not timestamp:
        return None
    moment = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    try:
        return moment.astimezone(ZoneInfo(zone)) if zone else moment
    except (KeyError, ValueError):
        return moment


def _clock(moment: datetime) -> str:
    return f"{moment.hour % 12 or 12}:{moment:%M} {moment:%p}"


def _window(start: str | None, end: str | None, zone: str | None) -> str | None:
    first, last = _local(start, zone), _local(end, zone)
    if first and last and first != last:
        return f"{_clock(first)}–{_clock(last)}"
    return _clock(first or last) if first or last else None


def _epoch_millis(value: int | None) -> str | None:
    if not value:
        return None
    return datetime.fromtimestamp(value / 1000, timezone.utc).isoformat(timespec="seconds")


def _string_field(number: int, text: str) -> bytes:
    data = text.encode()
    length, encoded = len(data), b""
    while True:
        byte = length & 0x7F
        length >>= 7
        encoded += bytes([byte | (0x80 if length else 0)])
        if not length:
            break
    return bytes([number << 3 | 2]) + encoded + data
