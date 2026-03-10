from typing import TYPE_CHECKING

from homeassistant.components import number

from .const import hac
from .helpers import entity as mle

if TYPE_CHECKING:
    from typing import ClassVar, Final, NotRequired, Unpack

    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

    from .helpers.device import BaseDevice
    from .helpers.entity import ChannelType
    from .helpers.manager import EntityManager


async def async_setup_entry(
    hass: "HomeAssistant", config_entry: "ConfigEntry", async_add_devices
):
    mle.Entity.platform_setup_entry(
        hass, config_entry, async_add_devices, number.DOMAIN
    )


class Number(mle.NumericEntity, number.NumberEntity):
    """
    Base (abstract) ancestor for number entities. This has 2 specializations:
    - ConfigNumber: for configuration parameters backed by a device namespace value.
    - EmulatedNumber: for configuration parameters not directly mapped to a device ns.
    These in turn will be managed with HA state-restoration.
    """

    if TYPE_CHECKING:

        DEVICE_CLASS_DURATION: Final[number.NumberDeviceClass]
        DEVICE_CLASS_TEMPERATURE_DELTA: Final[number.NumberDeviceClass]
        # HA core entity attributes:
        _attr_device_class: ClassVar[number.NumberDeviceClass | None]
        _attr_mode: ClassVar[number.NumberMode]
        _attr_native_max_value: ClassVar[float]
        _attr_native_min_value: ClassVar[float]
        _attr_native_step: ClassVar[float]
        native_step: float

        class Args(mle.NumericEntity.Args):
            device_class: NotRequired[number.NumberDeviceClass | None]  # Override
            mode: NotRequired[number.NumberMode]
            native_max_value: NotRequired[float]
            native_min_value: NotRequired[float]
            native_step: NotRequired[float]

        def __init__(
            self,
            channel: ChannelType | None,
            parent: EntityManager,
            /,
            **kwargs: Unpack[Args],
        ): ...

    PLATFORM = number.DOMAIN
    CORE_ENTITY_ATTRIBUTES = mle.NumericEntity.CORE_ENTITY_ATTRIBUTES + (
        "mode",
        "native_max_value",
        "native_min_value",
        "native_step",
    )
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
    _attr_entity_category = mle.Entity.EntityCategory.CONFIG
    _attr_mode = number.NumberMode.BOX
    _attr_native_step = 1.0


class ParserNumber(mle.NumericParser, Number):
    """
    Base class for any configurable numeric parameter in the device.
    """

    if TYPE_CHECKING:

        class Args(Number.Args, mle.NumericParser.Args):
            pass

        def __init__(
            self,
            channel: ChannelType | None,
            parent: BaseDevice,
            /,
            **kwargs: Unpack[Args],
        ): ...

        @classmethod
        def ENTITY_DEF(
            cls,
            **kwargs: Unpack[Args],
        ) -> "ParserNumber.EntityDef[ParserNumber]":  # type: ignore[override]
            pass

        DEBOUNCE_DELAY: Final

    DEBOUNCE_DELAY = 1

    def set_unavailable(self):
        self.cancel_callback(self._async_request_debounce)
        super().set_unavailable()

    # interface: number.NumberEntity
    async def async_set_native_value(self, value: float):
        """round up the requested value to the device native resolution
        which is almost always an int number (some exceptions though).
        This method will not immediately send the request but just schedule it after a short debounce delay,
        in order to 'collapse' multiple back-to-back changes (like when using the BOXED UI slider).
        """
        device_scale = self.device_scale
        if type(device_scale) is int:
            device_value = round(value * device_scale)
            device_step = round(self.native_step * device_scale)
            device_value = round(device_value / device_step) * device_step
            self.update_native_value(device_value / device_scale)
            self.schedule_async_callback(
                self.DEBOUNCE_DELAY, self._async_request_debounce, device_value
            )
        else:
            self.update_native_value(value)
            self.schedule_async_callback(
                self.DEBOUNCE_DELAY, self._async_request_debounce, value * device_scale
            )

    # interface: self
    async def _async_request_debounce(self, device_value: int | float):
        try:
            await self.async_request_value(device_value)
        except Exception:
            # restore the last good known device value
            try:
                self.update_native_value(self.device_value / self.device_scale)  # type: ignore
            except TypeError:
                pass  # self.device_value is None


class EmulatedNumber(Number):
    """
    Number entity not directly binded to a device parameter (like ConfigNumber)
    but used to store in HA a bit of component configuration.
    """

    __slots__ = Number._calc_slots()

    async def async_added_to_hass(self):
        await super().async_added_to_hass()
        with self.exception_warning("restoring previous state"):
            if last_state := await self.get_last_state_available():
                self.native_value = float(last_state.state)

    async def async_set_native_value(self, value: float):
        self.update_native_value(value)
