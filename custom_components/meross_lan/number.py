import typing

from homeassistant.components import number

from . import meross_entity as me
from .helpers import reverse_lookup
from .merossclient import const as mc

if typing.TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

    from .climate import MtsClimate
    from .meross_device import MerossDeviceBase

    # optional arguments for MLConfigNumber init
    class MLConfigNumberArgs(me.MerossNumericEntityArgs):
        pass


async def async_setup_entry(
    hass: "HomeAssistant", config_entry: "ConfigEntry", async_add_devices
):
    me.platform_setup_entry(hass, config_entry, async_add_devices, number.DOMAIN)


class MLNumber(me.MerossNumericEntity, number.NumberEntity):
    """
    Base (abstract) ancestor for ML number entities. This has 2 specializations:
    - MLConfigNumber: for configuration parameters backed by a device namespace value.
    - MLEmulatedNumber: for configuration parameters not directly mapped to a device ns.
    These in turn will be managed with HA state-restoration.
    """

    PLATFORM = number.DOMAIN
    DeviceClass = number.NumberDeviceClass

    # HA core compatibility layer for NumberDeviceClass.DURATION (HA core 2023.7 misses that)
    DEVICE_CLASS_DURATION = getattr(number.NumberDeviceClass, "DURATION", "duration")

    DEVICECLASS_TO_UNIT_MAP = {
        None: None,
        DEVICE_CLASS_DURATION: me.MerossEntity.hac.UnitOfTime.SECONDS,
        DeviceClass.HUMIDITY: me.MerossEntity.hac.PERCENTAGE,
        DeviceClass.TEMPERATURE: me.MerossEntity.hac.UnitOfTemperature.CELSIUS,
    }

    manager: "MerossDeviceBase"

    # HA core entity attributes:
    entity_category = me.EntityCategory.CONFIG
    mode: number.NumberMode = number.NumberMode.BOX
    native_max_value: float
    native_min_value: float
    native_step: float


class MLConfigNumber(me.MEListChannelMixin, MLNumber):
    """
    Base class for any configurable numeric parameter in the device. This works much-like
    MLSwitch by refining the 'async_request_value' api in order to send the command.
    Contrary to MLSwitch (which is abstract), this has a default implementation for
    payloads sent in a list through me.MEListChannelMixin since this looks to be
    widely adopted (thermostats and the likes) but some care needs to be taken for
    some namespaces not supporting channels (i.e. Appliance.GarageDoor.Config) or
    not understanding the list payload (likely all the RollerShutter stuff)
    """

    DEBOUNCE_DELAY = 1

    __slots__ = ("_async_request_debounce_unsub",)

    def __init__(
        self,
        manager: "MerossDeviceBase",
        channel: object | None,
        entitykey: str | None = None,
        device_class: MLNumber.DeviceClass | str | None = None,
        **kwargs: "typing.Unpack[MLConfigNumberArgs]",
    ):
        self._async_request_debounce_unsub = None
        super().__init__(
            manager,
            channel,
            entitykey,
            device_class,
            **kwargs,
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
        self._async_request_debounce_unsub = self.manager.schedule_async_callback(
            self.DEBOUNCE_DELAY, self._async_request_debounce, device_value
        )

    # interface: self
    async def _async_request_debounce(self, device_value):
        self._async_request_debounce_unsub = None
        if await self.async_request_value(device_value):
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


class MLEmulatedNumber(me.MEPartialAvailableMixin, MLNumber):
    """
    Number entity for locally (HA recorder) stored parameters.
    """

    async def async_added_to_hass(self):
        await super().async_added_to_hass()
        with self.exception_warning("restoring previous state"):
            if last_state := await self.get_last_state_available():
                self.native_value = float(last_state.state)

    async def async_set_native_value(self, value: float):
        self.update_native_value(value)


class MtsTemperatureNumber(MLConfigNumber):
    """
    Common number entity for representing MTS temperatures configuration
    """

    # HA core entity attributes:
    _attr_suggested_display_precision = 1

    __slots__ = ("climate",)

    def __init__(
        self,
        climate: "MtsClimate",
        entitykey: str,
        **kwargs: "typing.Unpack[MLConfigNumberArgs]",
    ):
        self.climate = climate
        kwargs["device_scale"] = climate.device_scale
        super().__init__(
            climate.manager,
            climate.channel,
            entitykey,
            MLConfigNumber.DeviceClass.TEMPERATURE,
            **kwargs,
        )


class MtsSetPointNumber(MtsTemperatureNumber):
    """
    Helper entity to configure MTS100/150/200 setpoints
    AKA: Heat(comfort) - Cool(sleep) - Eco(away)
    """

    # HA core entity attributes:
    icon: str

    __slots__ = ("icon",)

    def __init__(
        self,
        climate: "MtsClimate",
        preset_mode: str,
    ):
        self.key_value = climate.MTS_MODE_TO_TEMPERATUREKEY_MAP[
            reverse_lookup(climate.MTS_MODE_TO_PRESET_MAP, preset_mode)
        ]
        self.icon = climate.PRESET_TO_ICON_MAP[preset_mode]
        super().__init__(
            climate,
            f"config_temperature_{self.key_value}",
            name=f"{preset_mode} temperature",
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

    async def async_request_value(self, device_value):
        if response := await super().async_request_value(device_value):
            # mts100(s) reply to the setack with the 'full' (or anyway richer) payload
            # so we'll use the _parse_temperature logic (a bit overkill sometimes) to
            # make sure the climate state is consistent and all the correct roundings
            # are processed when changing any of the presets
            # not sure about mts200 replies..but we're optimist
            key_namespace = self.ns.key
            payload = response[mc.KEY_PAYLOAD]
            if key_namespace in payload:
                # by design key_namespace is either "temperature" (mts100) or "mode" (mts200)
                getattr(self.climate, f"_parse_{key_namespace}")(
                    payload[key_namespace][0]
                )

        return response
