from typing import TYPE_CHECKING

from homeassistant.components import number

from .helpers import entity as me, reverse_lookup
from .merossclient.protocol import const as mc

if TYPE_CHECKING:
    from typing import Unpack

    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

    from .climate import MtsClimate
    from .helpers.device import BaseDevice


async def async_setup_entry(
    hass: "HomeAssistant", config_entry: "ConfigEntry", async_add_devices
):
    me.platform_setup_entry(hass, config_entry, async_add_devices, number.DOMAIN)


class MLNumber(me.MLNumericEntity, number.NumberEntity):
    """
    Base (abstract) ancestor for ML number entities. This has 2 specializations:
    - MLConfigNumber: for configuration parameters backed by a device namespace value.
    - MLEmulatedNumber: for configuration parameters not directly mapped to a device ns.
    These in turn will be managed with HA state-restoration.
    """

    if TYPE_CHECKING:
        manager: "BaseDevice"
        # HA core entity attributes:
        mode: number.NumberMode
        native_max_value: float
        native_min_value: float
        native_step: float

    PLATFORM = number.DOMAIN
    DeviceClass = number.NumberDeviceClass

    # HA core compatibility layer for NumberDeviceClass.DURATION (HA core 2023.7 misses that)
    DEVICE_CLASS_DURATION = getattr(DeviceClass, "DURATION", "duration")
    # HA core compatibility layer for NumberDeviceClass.TEMPERATURE_DELTA (HA core 2025.10 misses that)
    DEVICE_CLASS_TEMPERATURE_DELTA = getattr(
        DeviceClass, "TEMPERATURE_DELTA", DeviceClass.TEMPERATURE
    )

    DEVICECLASS_TO_UNIT_MAP = {
        None: None,
        DEVICE_CLASS_DURATION: me.MLEntity.hac.UnitOfTime.SECONDS,
        DeviceClass.HUMIDITY: me.MLEntity.hac.PERCENTAGE,
        DeviceClass.TEMPERATURE: me.MLEntity.hac.UnitOfTemperature.CELSIUS,
        DEVICE_CLASS_TEMPERATURE_DELTA: me.MLEntity.hac.UnitOfTemperature.CELSIUS,
    }

    # HA core entity attributes:
    entity_category = me.MLNumericEntity.EntityCategory.CONFIG
    mode = number.NumberMode.BOX
    native_step = 1


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
        manager: "BaseDevice",
        channel: object | None,
        entitykey: str | None = None,
        device_class: MLNumber.DeviceClass | str | None = None,
        **kwargs: "Unpack[MLConfigNumber.Args]",
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
    Number entity not directly binded to a device parameter (like MLConfigNumber)
    but used to store in HA a bit of component configuration.
    """

    async def async_added_to_hass(self):
        await super().async_added_to_hass()
        with self.exception_warning("restoring previous state"):
            if last_state := await self.get_last_state_available():
                self.native_value = float(last_state.state)

    async def async_set_native_value(self, value: float):
        self.update_native_value(value)
