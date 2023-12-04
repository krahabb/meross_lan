from __future__ import annotations

import typing

from homeassistant.components import number
from homeassistant.const import PERCENTAGE, TEMP_CELSIUS

from . import meross_entity as me
from .merossclient import const as mc, get_namespacekey  # mEROSS cONST

if typing.TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

    from .meross_device import MerossDevice
    from .meross_device_hub import MerossSubDevice


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


DEVICECLASS_TO_UNIT_MAP = {
    NumberDeviceClass.HUMIDITY: PERCENTAGE,
    NumberDeviceClass.TEMPERATURE: TEMP_CELSIUS,
}


class MLConfigNumber(me.MerossEntity, number.NumberEntity):
    PLATFORM = number.DOMAIN
    DeviceClass = NumberDeviceClass

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
    )

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

    def update_native_value(self, value):
        self.update_state(value / self.ml_multiplier)

    async def async_set_native_value(self, value: float):
        value = round(value * self.ml_multiplier)

        if await self.manager.async_request_ack(
            self.namespace,
            mc.METHOD_SET,
            {
                self.key_namespace: [
                    {self.key_channel: self.channel, self.key_value: value}
                ]
            },
        ):
            self.update_native_value(value)

    @property
    def ml_multiplier(self):
        return 1


class MLHubAdjustNumber(MLConfigNumber):
    key_channel = mc.KEY_ID

    def __init__(
        self,
        manager: "MerossSubDevice",
        key: str,
        namespace: str,
        device_class: NumberDeviceClass,
        min_value: float,
        max_value: float,
        step: float,
    ):
        self.namespace = namespace
        self.key_namespace = get_namespacekey(namespace)
        self.key_value = key
        self._attr_native_min_value = min_value
        self._attr_native_max_value = max_value
        self._attr_native_step = step
        self._attr_native_unit_of_measurement = DEVICECLASS_TO_UNIT_MAP.get(
            device_class
        )
        self._attr_name = f"Adjust {device_class}"
        super().__init__(
            manager,
            manager.id,
            f"config_{self.key_namespace}_{key}",
            device_class,
        )

    @property
    def ml_multiplier(self):
        return 100
