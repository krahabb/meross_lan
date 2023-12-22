from __future__ import annotations

import typing

from homeassistant.components import number
from homeassistant.const import PERCENTAGE, TEMP_CELSIUS

from . import meross_entity as me
from .helpers import schedule_async_callback
from .merossclient import const as mc

if typing.TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

    from .helpers import EntityManager
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

    DEBOUNCE_DELAY = 1

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
        "_unsub_request",
    )

    def __init__(
        self,
        manager: EntityManager,
        channel: object | None,
        entitykey: str | None = None,
        device_class: NumberDeviceClass | None = None,
    ):
        super().__init__(manager, channel, entitykey, device_class)
        self._unsub_request = None

    async def async_shutdown(self):
        self._cancel_request()
        await super().async_shutdown()

    def set_unavailable(self):
        self._cancel_request()
        super().set_unavailable()

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
        device_value = round(value * self.device_scale)
        device_step = round(self.native_step * self.device_scale)
        device_value = round(device_value / device_step) * device_step
        # since the async_set_native_value might be triggered back-to-back
        # especially when using the BOXED UI we're debouncing the device
        # request and provide 'temporaneous' optimistic updates
        self.update_state(device_value / self.device_scale)
        if self._unsub_request:
            self._unsub_request.cancel()
        self._unsub_request = schedule_async_callback(
            self.hass, self.DEBOUNCE_DELAY, self._async_request_debounce, device_value
        )

    # interface: self
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
        self.update_state(device_value / self.device_scale)

    async def async_request(self, device_value):
        """sends the actual request to the device. this is likely to be overloaded"""
        return await self.manager.async_request_ack(
            self.namespace,
            mc.METHOD_SET,
            {
                self.key_namespace: [
                    {self.key_channel: self.channel, self.key_value: device_value}
                ]
            },
        )

    async def _async_request_debounce(self, device_value):
        self._unsub_request = None
        if await self.async_request(device_value):
            self.update_native_value(device_value)
        else:
            # restore the last good known device value
            if self.manager.online:
                self.update_state(self._device_value / self.device_scale)

    def _cancel_request(self):
        if self._unsub_request:
            self._unsub_request.cancel()
            self._unsub_request = None
