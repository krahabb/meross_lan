from __future__ import annotations
from datetime import datetime

from homeassistant.components.sensor import SensorEntity, STATE_CLASS_MEASUREMENT

from .meross_entity import (
    _MerossEntity,
    _MerossHubEntity,
    platform_setup_entry,
    platform_unload_entry,
)
from .const import PLATFORM_SENSOR


async def async_setup_entry(hass: object, config_entry: object, async_add_devices):
    platform_setup_entry(hass, config_entry, async_add_devices, PLATFORM_SENSOR)
    return

async def async_unload_entry(hass: object, config_entry: object) -> bool:
    return platform_unload_entry(hass, config_entry, PLATFORM_SENSOR)


class _MerossSensorEntity(SensorEntity):

    PLATFORM = PLATFORM_SENSOR
    _attr_last_reset: datetime | None = None

    @property
    def state_class(self) -> str:
        return STATE_CLASS_MEASUREMENT

    @property
    def last_reset(self) -> datetime | None:
        return self._attr_last_reset


class MerossLanSensor(_MerossEntity, _MerossSensorEntity):

    def __init__(self, device: "MerossDevice", id: object, device_class: str):
        super().__init__(device, id, device_class)


class MerossLanHubSensor(_MerossHubEntity, _MerossSensorEntity):

    def __init__(self, subdevice: "MerossSubDevice", device_class: str):
        super().__init__(subdevice, f"{subdevice.id}_{device_class}", device_class)
