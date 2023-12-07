from __future__ import annotations

import typing

from homeassistant.components import number
from homeassistant.const import PERCENTAGE, TEMP_CELSIUS

from . import meross_entity as me
from .merossclient import const as mc

if typing.TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

    from .meross_device import MerossDevice


try:
    NumberDeviceClass = number.NumberDeviceClass  # type: ignore
except Exception:
    from .helpers import StrEnum

    class NumberDeviceClass(StrEnum):
        HUMIDITY = "humidity"
        TEMPERATURE = "temperature"


try:
    NUMBERMODE_AUTO = number.NumberMode.AUTO
    NUMBERMODE_BOX = number.NumberMode.BOX
    NUMBERMODE_SLIDER = number.NumberMode.SLIDER
except Exception:
    NUMBERMODE_AUTO = "auto"
    NUMBERMODE_BOX = "box"
    NUMBERMODE_SLIDER = "slider"


async def async_setup_entry(
    hass: HomeAssistant, config_entry: ConfigEntry, async_add_devices
):
    me.platform_setup_entry(hass, config_entry, async_add_devices, number.DOMAIN)


class MLConfigNumber(me.MerossEntity, number.NumberEntity):
    PLATFORM = number.DOMAIN
    DeviceClass = NumberDeviceClass
    DEVICECLASS_TO_UNIT_MAP = {
        NumberDeviceClass.HUMIDITY: PERCENTAGE,
        NumberDeviceClass.TEMPERATURE: TEMP_CELSIUS,
    }

    manager: MerossDevice

    _attr_entity_category = me.EntityCategory.CONFIG
    _attr_native_max_value: float
    _attr_native_min_value: float
    _attr_native_step: float
    _attr_native_unit_of_measurement: str | None
    _attr_state: int | float | None

    # customize the request payload for different
    # devices api. see 'async_set_native_value' to see how
    namespace: str
    key_namespace: str
    key_channel: str = mc.KEY_CHANNEL
    key_value: str

    __slots__ = (
        "_attr_native_max_value",
        "_attr_native_min_value",
        "_attr_native_step",
        "_attr_native_unit_of_measurement",
        "_device_value",
    )

    # interface: number.NumberEntity
    @property
    def mode(self) -> number.NumberMode:
        """Return the mode of the entity."""
        return NUMBERMODE_BOX  # type: ignore

    @property
    def native_max_value(self):
        return self._attr_native_max_value

    @property
    def native_min_value(self):
        return self._attr_native_min_value

    @property
    def native_step(self):
        return self._attr_native_step

    @property
    def native_unit_of_measurement(self):
        return self._attr_native_unit_of_measurement

    @property
    def native_value(self):
        return self._attr_state

    async def async_set_native_value(self, value: float):
        device_value = round(value * self.device_scale) + self.device_offset
        if await self.manager.async_request_ack(
            self.namespace,
            mc.METHOD_SET,
            {
                self.key_namespace: [
                    {self.key_channel: self.channel, self.key_value: device_value}
                ]
            },
        ):
            self.update_native_value(device_value)

    # interface: self
    @property
    def device_offset(self):
        """used to offset the device value when converting to/from native value"""
        return 0

    @property
    def device_scale(self):
        """used to scale the device value when converting to/from native value"""
        return 1

    @property
    def device_value(self):
        """the 'native' device value carried in protocol messages"""
        return self._device_value

    def update_native_value(self, device_value):
        self._device_value = device_value
        self.update_state((device_value - self.device_offset) / self.device_scale)
