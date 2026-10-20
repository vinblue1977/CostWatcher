"""The device model shared by every vendor, plus the mock devices served until the adapters are built."""

from dataclasses import asdict, dataclass, field
from typing import Any

# command -> the capability a device needs for it
COMMANDS = {
    "on": "switch",
    "off": "switch",
    "set_level": "level",
    "open": "door",
    "close": "door",
}


@dataclass(frozen=True)
class Device:
    id: str
    vendor: str  # smartthings | lgthinq
    name: str
    type: str  # light | plug | lock | garage_door | refrigerator | dishwasher
    room: str
    state: dict[str, Any]
    capabilities: list[str] = field(default_factory=list)

    def public(self) -> dict[str, Any]:
        return asdict(self)


# Placeholder names and states, matching the approved mockups. Replaced by the SmartThings and ThinQ adapters.
MOCK_DEVICES = [
    Device("st-garage-shria", "smartthings", "Shria door", "garage_door", "Garage", {"door": "closed"}, ["door"]),
    Device(
        "st-garage-krish", "smartthings", "Krish door", "garage_door", "Garage", {"door": "open", "openMinutes": 12}, ["door"]
    ),
    Device("st-lock-front", "smartthings", "Front door lock", "lock", "Entry", {"lock": "locked"}, []),
    Device(
        "st-light-living",
        "smartthings",
        "Living room light",
        "light",
        "Living room",
        {"switch": "on", "level": 70},
        ["switch", "level"],
    ),
    Device("st-light-porch", "smartthings", "Porch light", "light", "Outside", {"switch": "off"}, ["switch"]),
    Device("st-plug-office", "smartthings", "Office plug", "plug", "Office", {"switch": "on"}, ["switch"]),
    Device("lg-fridge", "lgthinq", "Kitchen refrigerator", "refrigerator", "Kitchen", {"doorOpen": False}, []),
    Device("lg-dishwasher", "lgthinq", "Dishwasher", "dishwasher", "Kitchen", {"run": "idle"}, []),
]
BY_ID = {d.id: d for d in MOCK_DEVICES}


class CommandError(ValueError):
    """The command is unknown, unsupported by the device, or has a bad value."""


def validate_command(device: Device, command: str, value: Any = None, confirm: Any = None) -> None:
    needed = COMMANDS.get(command)
    if needed is None:
        raise CommandError(f"unknown command {command!r}")
    if needed not in device.capabilities:
        raise CommandError(f"{device.name} does not support {command!r}")
    if command == "set_level":
        if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 100:
            raise CommandError("set_level needs a whole number from 0 to 100")
    elif value is not None:
        raise CommandError(f"{command!r} takes no value")
    # Opening a door is physical access: the app sends confirm=true only after its confirmation sheet.
    if command == "open" and confirm is not True:
        raise CommandError("open needs confirm=true")
