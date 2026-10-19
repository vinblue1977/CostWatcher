"""The devices API: sign-in is required, reads return the mock devices, and commands are validated."""

import json
from typing import Any

import pytest

from api.devices import BY_ID, MOCK_DEVICES, CommandError, validate_command
from api.handler import handler


def _event(route: str, device_id: str | None = None, body: str | None = None, signed_in: bool = True) -> dict[str, Any]:
    event: dict[str, Any] = {"routeKey": route, "pathParameters": {"id": device_id} if device_id else None, "body": body}
    if signed_in:
        event["requestContext"] = {"authorizer": {"jwt": {"claims": {"sub": "user-1"}}}}
    return event


def _call(*args: Any, **kwargs: Any) -> tuple[int, dict[str, Any]]:
    response = handler(_event(*args, **kwargs), None)
    return response["statusCode"], json.loads(response["body"])


def test_requires_sign_in() -> None:
    assert _call("GET /devices", signed_in=False)[0] == 401


def test_lists_mock_devices_in_the_common_model() -> None:
    status, body = _call("GET /devices")
    assert status == 200 and body["mock"] is True
    assert len(body["devices"]) == len(MOCK_DEVICES) == 8
    assert set(body["devices"][0]) == {"id", "vendor", "name", "type", "room", "state", "capabilities"}
    assert {d["vendor"] for d in body["devices"]} == {"smartthings", "lgthinq"}


def test_both_garage_doors_are_present() -> None:
    doors = [d for d in MOCK_DEVICES if d.type == "garage_door"]
    assert [d.name for d in doors] == ["Shria door", "Krish door"]


def test_gets_one_device_or_404() -> None:
    assert _call("GET /devices/{id}", "st-light-living")[1]["name"] == "Living room light"
    assert _call("GET /devices/{id}", "nope")[0] == 404


def test_unknown_route_is_404() -> None:
    assert _call("DELETE /devices")[0] == 404


def test_command_is_accepted_but_mock() -> None:
    status, body = _call("POST /devices/{id}/commands", "st-garage-krish", json.dumps({"command": "close"}))
    assert status == 202 and body == {"accepted": True, "mock": True, "device": "st-garage-krish", "command": "close"}


@pytest.mark.parametrize(
    ("device_id", "body"),
    [
        ("st-light-porch", {"command": "open"}),  # a light has no door
        ("st-light-porch", {"command": "set_level", "value": 50}),  # on/off only
        ("st-light-living", {"command": "set_level", "value": 101}),
        ("st-light-living", {"command": "set_level", "value": True}),
        ("st-light-living", {"command": "set_level"}),
        ("st-lock-front", {"command": "off"}),  # locks are read-only for now
        ("st-light-living", {"command": "explode"}),
        ("st-light-living", {"nope": 1}),
        ("st-light-living", [1, 2]),
    ],
)
def test_bad_commands_are_rejected(device_id: str, body: Any) -> None:
    assert _call("POST /devices/{id}/commands", device_id, json.dumps(body))[0] == 400


def test_command_body_rules() -> None:
    assert _call("POST /devices/{id}/commands", "st-light-living", None)[0] == 400
    assert _call("POST /devices/{id}/commands", "st-light-living", "x" * 2000)[0] == 400
    assert _call("POST /devices/{id}/commands", "st-light-living", "not json")[0] == 400
    assert _call("POST /devices/{id}/commands", "nope", json.dumps({"command": "on"}))[0] == 404


def test_set_level_accepts_0_to_100() -> None:
    for value in (0, 70, 100):
        validate_command(BY_ID["st-light-living"], "set_level", value)
    with pytest.raises(CommandError):
        validate_command(BY_ID["st-light-living"], "set_level", -1)
