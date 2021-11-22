from __future__ import annotations
from datetime import datetime

from homeassistant.helpers.typing import StateType
from homeassistant.components.sensor import (
    DOMAIN as PLATFORM_SENSOR,
)
try:
    from homeassistant.components.sensor import SensorEntity
except:#someone still pre 2021.5.0 ?
    from homeassistant.helpers.entity import Entity as SensorEntity
try:
    from homeassistant.components.sensor import STATE_CLASS_MEASUREMENT
except:#someone still pre 2021.8.0 ?
    STATE_CLASS_MEASUREMENT = None
try:
    from homeassistant.components.sensor import STATE_CLASS_TOTAL_INCREASING
except:#someone still pre 2021.9.0 ?
    STATE_CLASS_TOTAL_INCREASING = STATE_CLASS_MEASUREMENT

from homeassistant.const import (
    DEVICE_CLASS_POWER, POWER_WATT,
    DEVICE_CLASS_CURRENT,
    DEVICE_CLASS_VOLTAGE,
    DEVICE_CLASS_ENERGY, ENERGY_WATT_HOUR,
    DEVICE_CLASS_TEMPERATURE, TEMP_CELSIUS,
    DEVICE_CLASS_HUMIDITY, PERCENTAGE,
    DEVICE_CLASS_BATTERY
)
try:
    # new in 2021.8.0 core (#52 #53)
    from homeassistant.const import (
        ELECTRIC_CURRENT_AMPERE,
        ELECTRIC_POTENTIAL_VOLT,
    )
except:#someone still pre 2021.8.0 ?
    from homeassistant.const import (
        ELECTRICAL_CURRENT_AMPERE,
        VOLT,
    )
    ELECTRIC_CURRENT_AMPERE = ELECTRICAL_CURRENT_AMPERE
    ELECTRIC_POTENTIAL_VOLT = VOLT



from .meross_entity import (
    _MerossEntity,
    platform_setup_entry,
    platform_unload_entry,
)

CLASS_TO_UNIT_MAP = {
    DEVICE_CLASS_POWER: POWER_WATT,
    DEVICE_CLASS_CURRENT: ELECTRIC_CURRENT_AMPERE,
    DEVICE_CLASS_VOLTAGE: ELECTRIC_POTENTIAL_VOLT,
    DEVICE_CLASS_ENERGY: ENERGY_WATT_HOUR,
    DEVICE_CLASS_TEMPERATURE: TEMP_CELSIUS,
    DEVICE_CLASS_HUMIDITY: PERCENTAGE,
    DEVICE_CLASS_BATTERY: PERCENTAGE
}

CORE_HAS_NATIVE_UNIT = hasattr(SensorEntity, 'native_unit_of_measurement')


async def async_setup_entry(hass: object, config_entry: object, async_add_devices):
    platform_setup_entry(hass, config_entry, async_add_devices, PLATFORM_SENSOR)

async def async_unload_entry(hass: object, config_entry: object) -> bool:
    return platform_unload_entry(hass, config_entry, PLATFORM_SENSOR)



class MerossLanSensor(_MerossEntity, SensorEntity):

    PLATFORM = PLATFORM_SENSOR

    _attr_state_class: str | None = STATE_CLASS_MEASUREMENT
    _attr_last_reset: datetime | None = None # Deprecated, to be removed in 2021.11
    _attr_native_unit_of_measurement: str | None

    def __init__(self, device: "MerossDevice", _id: object, device_class: str, subdevice: "MerossSubDevice" = None):
        super().__init__(device, _id, device_class, subdevice)
        self._attr_native_unit_of_measurement = CLASS_TO_UNIT_MAP.get(device_class)


    @staticmethod
    def build_for_subdevice(subdevice: "MerossSubDevice", device_class: str):
        return MerossLanSensor(subdevice.hub, f"{subdevice.id}_{device_class}", device_class, subdevice)


    @property
    def state_class(self) -> str | None:
        return self._attr_state_class


    @property
    def last_reset(self) -> datetime | None: # Deprecated, to be removed in 2021.11
        return self._attr_last_reset


    @property
    def native_unit_of_measurement(self) -> str | None:
        return self._attr_native_unit_of_measurement


    @property
    def unit_of_measurement(self) -> str | None:
        if CORE_HAS_NATIVE_UNIT:
            # let the core implementation manage unit conversions
            # in it's '@final unit_of_measurement'
            return SensorEntity.unit_of_measurement.__get__(self)
        return self._attr_native_unit_of_measurement


    @property
    def native_value(self) -> StateType:
        return self._attr_state


    @property
    def state(self) -> StateType:
        if CORE_HAS_NATIVE_UNIT:
            # let the core implementation manage unit conversions
            return SensorEntity.state.__get__(self)
        return self._attr_state
