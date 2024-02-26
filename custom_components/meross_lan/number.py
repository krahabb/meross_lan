from __future__ import annotations

import typing

from homeassistant.components import number
from homeassistant.const import UnitOfTemperature, UnitOfTime

from . import meross_entity as me
from .helpers import reverse_lookup, schedule_async_callback
from .merossclient import const as mc

if typing.TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

    from .climate import MtsClimate
    from .helpers.manager import EntityManager
    from .meross_device import MerossDevice


async def async_setup_entry(
    hass: HomeAssistant, config_entry: ConfigEntry, async_add_devices
):
    me.platform_setup_entry(hass, config_entry, async_add_devices, number.DOMAIN)


class MLConfigNumber(me.MerossNumericEntity, number.NumberEntity):
    PLATFORM = number.DOMAIN
    DeviceClass = number.NumberDeviceClass

    # HA core compatibility layer for NumberDeviceClass.DURATION (HA core 2023.7 misses that)
    DEVICE_CLASS_DURATION = getattr(number.NumberDeviceClass, "DURATION", "duration")

    DEVICECLASS_TO_UNIT_MAP = {
        None: None,
        DEVICE_CLASS_DURATION: UnitOfTime.SECONDS,
        DeviceClass.HUMIDITY: me.MerossNumericEntity.UNIT_PERCENTAGE,
        DeviceClass.TEMPERATURE: UnitOfTemperature.CELSIUS,
    }

    DEBOUNCE_DELAY = 1

    manager: MerossDevice

    # HA core entity attributes:
    entity_category = me.EntityCategory.CONFIG
    mode: number.NumberMode = number.NumberMode.BOX
    native_max_value: float
    native_min_value: float
    native_step: float

    __slots__ = ("_async_request_debounce_unsub",)

    def __init__(
        self,
        manager: EntityManager,
        channel: object | None,
        entitykey: str | None = None,
        device_class: DeviceClass | str | None = None,
        *,
        device_value: int | None = None,
        native_unit_of_measurement: str | None = None,
    ):
        self._async_request_debounce_unsub = None
        super().__init__(
            manager,
            channel,
            entitykey,
            device_class,
            device_value=device_value,
            native_unit_of_measurement=native_unit_of_measurement
        )

    async def async_shutdown(self):
        self._cancel_request()
        await super().async_shutdown()

    def set_unavailable(self):
        self._cancel_request()
        super().set_unavailable()

    # interface: number.NumberEntity
    async def async_set_native_value(self, value: float):
        """round up the requested value to the device native resolution
        which is almost always an int number (some exceptions though)."""
        device_value = round(value * self.device_scale)
        device_step = round(self.native_step * self.device_scale)
        device_value = round(device_value / device_step) * device_step
        # since the async_set_native_value might be triggered back-to-back
        # especially when using the BOXED UI we're debouncing the device
        # request and provide 'temporaneous' optimistic updates
        self.update_native_value(device_value / self.device_scale)
        if self._async_request_debounce_unsub:
            self._async_request_debounce_unsub.cancel()
        self._async_request_debounce_unsub = schedule_async_callback(
            self.hass, self.DEBOUNCE_DELAY, self._async_request_debounce, device_value
        )

    # interface: self
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
        self._async_request_debounce_unsub = None
        if await self.async_request(device_value):
            self.update_device_value(device_value)
        else:
            # restore the last good known device value
            device_value = self.device_value
            if device_value is not None:
                self.update_native_value(device_value / self.device_scale)

    def _cancel_request(self):
        if self._async_request_debounce_unsub:
            self._async_request_debounce_unsub.cancel()
            self._async_request_debounce_unsub = None


class MtsTemperatureNumber(MLConfigNumber):
    """
    Common number entity for representing MTS temperatures configuration
    """

    __slots__ = (
        "climate",
        "device_scale",
    )

    def __init__(self, climate: MtsClimate, entitykey: str):
        self.climate = climate
        self.device_scale = climate.device_scale
        super().__init__(
            climate.manager,
            climate.channel,
            entitykey,
            MLConfigNumber.DeviceClass.TEMPERATURE,
        )


class MtsSetPointNumber(MtsTemperatureNumber):
    """
    Helper entity to configure MTS100/150/200 setpoints
    AKA: Heat(comfort) - Cool(sleep) - Eco(away)
    """

    # HA core entity attributes:
    icon: str

    __slots__ = ("icon",)

    def __init__(self, climate: MtsClimate, preset_mode: str):
        self.key_value = climate.MTS_MODE_TO_TEMPERATUREKEY_MAP[
            reverse_lookup(climate.MTS_MODE_TO_PRESET_MAP, preset_mode)
        ]
        self.icon = climate.PRESET_TO_ICON_MAP[preset_mode]
        self.name = f"{preset_mode} {MLConfigNumber.DeviceClass.TEMPERATURE}"
        super().__init__(
            climate,
            f"config_{mc.KEY_TEMPERATURE}_{self.key_value}",
        )

    @property
    def native_max_value(self):
        return self.climate.max_temp

    @property
    def native_min_value(self):
        return self.climate.min_temp

    @property
    def native_step(self):
        return self.climate.target_temperature_step

    async def async_request(self, device_value):
        if response := await super().async_request(device_value):
            # mts100(s) reply to the setack with the 'full' (or anyway richer) payload
            # so we'll use the _parse_temperature logic (a bit overkill sometimes) to
            # make sure the climate state is consistent and all the correct roundings
            # are processed when changing any of the presets
            # not sure about mts200 replies..but we're optimist
            key_namespace = self.key_namespace
            payload = response[mc.KEY_PAYLOAD]
            if key_namespace in payload:
                # by design key_namespace is either "temperature" (mts100) or "mode" (mts200)
                self.climate._parse(payload[key_namespace][0])

        return response
