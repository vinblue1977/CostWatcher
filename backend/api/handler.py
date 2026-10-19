"""HTTP API Lambda: the signed-in user's devices. Serves mock data until the vendor adapters land."""

import json
from typing import Any

from api.devices import BY_ID, MOCK_DEVICES, CommandError, validate_command

MAX_BODY = 1024


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    try:
        _ = event["requestContext"]["authorizer"]["jwt"]["claims"]["sub"]
    except KeyError, TypeError:
        return _json(401, {"error": "unauthorized"})
    route = event.get("routeKey", "")
    device_id = (event.get("pathParameters") or {}).get("id", "")
    if route == "GET /devices":
        return _json(200, {"devices": [d.public() for d in MOCK_DEVICES], "mock": True})
    if route == "GET /devices/{id}":
        device = BY_ID.get(device_id)
        return _json(200, device.public()) if device else _json(404, {"error": "unknown device"})
    if route == "POST /devices/{id}/commands":
        return _command(device_id, event.get("body"))
    return _json(404, {"error": "not found"})


def _command(device_id: str, body: str | None) -> dict[str, Any]:
    device = BY_ID.get(device_id)
    if device is None:
        return _json(404, {"error": "unknown device"})
    if body is None or len(body.encode()) > MAX_BODY:
        return _json(400, {"error": "a JSON body of at most 1 KB is required"})
    try:
        payload = json.loads(body)
        command = payload["command"]
        validate_command(device, command, payload.get("value"), payload.get("confirm"))
    except (ValueError, KeyError, TypeError) as err:
        return _json(400, {"error": str(err) if isinstance(err, CommandError) else "body must be {command, value?}"})
    # Mock: nothing is sent to a real device yet.
    return _json(202, {"accepted": True, "mock": True, "device": device.id, "command": command})


def _json(status: int, body: dict[str, Any]) -> dict[str, Any]:
    return {
        "statusCode": status,
        "headers": {"Content-Type": "application/json", "Cache-Control": "no-store"},
        "body": json.dumps(body),
    }
