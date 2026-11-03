import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from typing import Any, Final, TypedDict

    class DeviceParametersDictType(TypedDict):
        types: list[str]
        """list of Meross device type prefixes to apply this parameter set to."""
        name: str
        """Friendly name of the appliance type, e.g. "Smart Plug" or "Smart Bulb"."""

    DEVICES_LIBRARY: Final


_json_dict = dict[str, Any]


class DeviceParameters(_json_dict):
    def __init__(self, data: _json_dict):
        _json_dict.__init__(self, data)

    def __missing__(self, key: str) -> Any:
        raise KeyError(
            f"Device parameter '{key}' not found in device parameters dictionary."
        )


class DevicesDict(dict[str, DeviceParameters]):
    def __init__(self):
        devices: list["DeviceParametersDictType"] = json.load(
            open(
                file=Path(__file__).with_name("devices.json"),
                mode="r",
                encoding="utf-8",
            )
        )
        # Generate (global) dict entries for each type specified in the parameter set
        dict.__init__(
            self,
            (
                (device_type, DeviceParameters(content))  # type: ignore
                for content in devices
                for device_type in content["types"]
            ),
        )

    def __missing__(self, device_type: str) -> DeviceParameters:
        """
        When a device type is missing from the configuration we'll lookup the closest prefix matching
        and create a new entry with the same paramater set reference.
        """
        for type, data in self.items():
            if device_type.startswith(type):
                break
        else:
            data = DeviceParameters(
                {
                    "types": [device_type],
                    "name": device_type,
                }
            )
        self[device_type] = data
        return data

    def lookup(self, device_type: str) -> DeviceParameters | None:
        for _type, _parameters in self.items():
            if device_type.startswith(_type):
                return _parameters


DEVICES_LIBRARY = DevicesDict()
