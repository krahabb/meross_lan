from typing import TYPE_CHECKING

from homeassistant.components import number

from .const import hac
from .helpers.entity import MLNumericEntity

if TYPE_CHECKING:
    from typing import ClassVar, Final, NotRequired, Unpack

    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

    from .helpers.device import BaseDevice


async def async_setup_entry(
    hass: "HomeAssistant", config_entry: "ConfigEntry", async_add_devices
):
    MLNumericEntity.platform_setup_entry(
        hass, config_entry, async_add_devices, number.DOMAIN
    )


class MLNumber(MLNumericEntity, number.NumberEntity):
    """
    Base (abstract) ancestor for ML number entities. This has 2 specializations:
    - MLConfigNumber: for configuration parameters backed by a device namespace value.
    - MLEmulatedNumber: for configuration parameters not directly mapped to a device ns.
    These in turn will be managed with HA state-restoration.
    """

    if TYPE_CHECKING:

        class Args(MLNumericEntity.Args):
            device_class: NotRequired[number.NumberDeviceClass | None]

        manager: "BaseDevice"
        DEVICE_CLASS_DURATION: Final[number.NumberDeviceClass]
        DEVICE_CLASS_TEMPERATURE_DELTA: Final[number.NumberDeviceClass]
        # HA core entity attributes:
        _attr_device_class: ClassVar[number.NumberDeviceClass | None]
        mode: number.NumberMode
        native_max_value: float
        native_min_value: float
        native_step: float

    PLATFORM = number.DOMAIN
    DeviceClass = number.NumberDeviceClass

    # HA core compatibility layer for NumberDeviceClass.DURATION (HA core 2023.7 misses that)
    DEVICE_CLASS_DURATION = getattr(DeviceClass, "DURATION", "duration")  # type: ignore
    # HA core compatibility layer for NumberDeviceClass.TEMPERATURE_DELTA (HA core 2025.10 misses that)
    DEVICE_CLASS_TEMPERATURE_DELTA = getattr(
        DeviceClass, "TEMPERATURE_DELTA", DeviceClass.TEMPERATURE
    )

    DEVICECLASS_TO_UNIT_MAP = {
        None: None,
        DEVICE_CLASS_DURATION: hac.UnitOfTime.SECONDS,
        DeviceClass.HUMIDITY: hac.PERCENTAGE,
        DeviceClass.TEMPERATURE: hac.UnitOfTemperature.CELSIUS,
        DEVICE_CLASS_TEMPERATURE_DELTA: hac.UnitOfTemperature.CELSIUS,
    }

    # HA core entity attributes:
    entity_category = MLNumericEntity.EntityCategory.CONFIG
    mode = number.NumberMode.BOX
    native_step = 1


class MLConfigNumber(MLNumber):
    """
    Base class for any configurable numeric parameter in the device.
    """

    DEBOUNCE_DELAY = 1

    __slots__ = ("_async_request_debounce_unsub",)

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
        self._cancel_request()
        self._async_request_debounce_unsub = self.manager.schedule_async_callback(
            self.DEBOUNCE_DELAY, self._async_request_debounce, device_value
        )

    # interface: self
    async def _async_request_debounce(self, device_value):
        del self._async_request_debounce_unsub
        try:
            await self.async_request_value(device_value)
        except Exception:
            # restore the last good known device value
            device_value = self.device_value
            if device_value is not None:
                self.update_native_value(device_value / self.device_scale)

    def _cancel_request(self):
        try:
            self._async_request_debounce_unsub.cancel()
            del self._async_request_debounce_unsub
        except AttributeError:
            return


class MLEmulatedNumber(MLNumber.PartialAvailableMixin, MLNumber):
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
