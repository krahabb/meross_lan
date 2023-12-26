from __future__ import annotations

import typing

from homeassistant.components import binary_sensor

from . import meross_entity as me

if typing.TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant


try:
    BinarySensorDeviceClass = binary_sensor.BinarySensorDeviceClass  # type: ignore
except Exception:
    from .helpers import StrEnum

    class BinarySensorDeviceClass(StrEnum):
        CONNECTIVITY = "connectivity"
        PROBLEM = "problem"
        SAFETY = "safety"
        WINDOW = "window"


async def async_setup_entry(
    hass: "HomeAssistant", config_entry: "ConfigEntry", async_add_devices
):
    me.platform_setup_entry(hass, config_entry, async_add_devices, binary_sensor.DOMAIN)


class MLBinarySensor(me.MerossEntity, binary_sensor.BinarySensorEntity):
    PLATFORM = binary_sensor.DOMAIN
    DeviceClass = BinarySensorDeviceClass
