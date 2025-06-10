import enum
import typing
from typing import TYPE_CHECKING

from homeassistant.components import climate

from .helpers import entity as me, reverse_lookup
from .merossclient.protocol import const as mc
from .select import MtsTrackedSensor
from .sensor import MLTemperatureSensor

if TYPE_CHECKING:
    from typing import ClassVar, Final, TypeAlias

    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

    from .calendar import MtsSchedule
    from .helpers.device import BaseDevice
    from .helpers.namespaces import NamespaceHandler
    from .number import MtsSetPointNumber, MtsTemperatureNumber


async def async_setup_entry(
    hass: "HomeAssistant", config_entry: "ConfigEntry", async_add_devices
):
    me.platform_setup_entry(hass, config_entry, async_add_devices, climate.DOMAIN)


class MtsClimate(me.MLEntity, climate.ClimateEntity):

    class Preset(enum.StrEnum):
        CUSTOM = "custom"
        COMFORT = "comfort"
        SLEEP = "sleep"
        AWAY = "away"
        AUTO = "auto"

    if TYPE_CHECKING:
        ATTR_TEMPERATURE: Final
        TEMP_CELSIUS: Final
        device_scale: ClassVar[float]

        MTS_MODE_TO_PRESET_MAP: ClassVar[dict[int | None, str]]
        """maps device 'mode' value to the HA climate.preset_mode"""
        MTS_MODE_TO_TEMPERATUREKEY_MAP: ClassVar[dict[int | None, str]]
        """maps the current mts mode to the name of temperature setpoint key"""
        PRESET_TO_ICON_MAP: Final[dict[Preset, str]]
        """Used in Number entities for temperatues setpoint."""
        SET_TEMP_FORCE_MANUAL_MODE: Final[bool]
        """Determines the behavior of async_set_temperature."""
        manager: BaseDevice
        number_adjust_temperature: Final[MtsTemperatureNumber]
        number_preset_temperature: dict[str, MtsSetPointNumber]
        schedule: Final[MtsSchedule]
        select_tracked_sensor: Final[MtsTrackedSensor]

        # HA core entity attributes override:
        _attr_preset_modes: list[str]
        _attr_supported_features: climate.ClimateEntityFeature
        current_humidity: float | None
        current_temperature: float | None
        hvac_action: climate.HVACAction | None
        hvac_mode: climate.HVACMode | None
        max_temp: float
        min_temp: float
        preset_mode: str | None
        preset_modes: list[str]
        supported_features: climate.ClimateEntityFeature
        target_temperature: float | None

    PLATFORM = climate.DOMAIN

    ATTR_HVAC_MODE = climate.ATTR_HVAC_MODE
    ATTR_TEMPERATURE = climate.ATTR_TEMPERATURE
    ATTR_TARGET_TEMP_HIGH = climate.ATTR_TARGET_TEMP_HIGH
    ATTR_TARGET_TEMP_LOW = climate.ATTR_TARGET_TEMP_LOW
    TEMP_CELSIUS = me.MLEntity.hac.UnitOfTemperature.CELSIUS

    ClimateEntityFeature = climate.ClimateEntityFeature
    HVACAction = climate.HVACAction
    HVACMode = climate.HVACMode

    device_scale = mc.MTS_TEMP_SCALE

    PRESET_TO_ICON_MAP = {
        Preset.COMFORT: "mdi:sun-thermometer",
        Preset.SLEEP: "mdi:power-sleep",
        Preset.AWAY: "mdi:bag-checked",
    }
    """lookups used in MtsSetpointNumber to map a pretty icon to the setpoint entity"""

    SET_TEMP_FORCE_MANUAL_MODE = True
    """Determines the behavior of async_set_temperature."""

    # HA core entity attributes:
    _attr_hvac_modes = [HVACMode.OFF, HVACMode.HEAT]
    _attr_preset_modes = list(Preset)
    _attr_supported_features = (
        climate.ClimateEntityFeature.PRESET_MODE
        | climate.ClimateEntityFeature.TARGET_TEMPERATURE
        | getattr(climate.ClimateEntityFeature, "TURN_OFF", 0)
        | getattr(climate.ClimateEntityFeature, "TURN_ON", 0)
    )
    _enable_turn_on_off_backwards_compatibility = False

    target_temperature_step: float = 0.5
    temperature_unit: str = TEMP_CELSIUS
    translation_key = "mts_climate"

    __slots__ = (
        "current_humidity",
        "current_temperature",
        "hvac_action",
        "hvac_mode",
        "hvac_modes",
        "max_temp",
        "min_temp",
        "preset_mode",
        "preset_modes",
        "supported_features",
        "target_temperature",
        "_mts_active",
        "_mts_mode",
        "_mts_onoff",
        "_mts_payload",
        "number_adjust_temperature",
        "number_preset_temperature",
        "schedule",
        "select_tracked_sensor",
        "sensor_current_temperature",
    )

    def __init__(
        self,
        manager: "BaseDevice",
        channel: object,
        adjust_number_class: type["MtsTemperatureNumber"],
        preset_number_class: type["MtsSetPointNumber"] | None,
        calendar_class: type["MtsSchedule"],
    ):
        self.current_humidity = None
        self.current_temperature = None
        self.hvac_action = None
        self.hvac_mode = None
        self.hvac_modes = self._attr_hvac_modes
        self.max_temp = 35
        self.min_temp = 5
        self.preset_mode = None
        self.preset_modes = self._attr_preset_modes
        self.supported_features = self._attr_supported_features
        self.target_temperature = None
        self._mts_active = None
        self._mts_mode: int | None = None
        self._mts_onoff: int | None = None
        self._mts_payload = {}
        super().__init__(manager, channel)
        self.number_adjust_temperature = adjust_number_class(self)  # type: ignore
        self.number_preset_temperature = {}
        if preset_number_class:
            for preset in MtsClimate.PRESET_TO_ICON_MAP.keys():
                number_preset_temperature = preset_number_class(self, preset)
                self.number_preset_temperature[number_preset_temperature.key_value] = (
                    number_preset_temperature
                )
        self.schedule = calendar_class(self)
        self.select_tracked_sensor = MtsTrackedSensor(self)
        self.sensor_current_temperature = MLTemperatureSensor(manager, channel)
        self.sensor_current_temperature.entity_registry_enabled_default = False

    # interface: MLEntity
    async def async_shutdown(self):
        await super().async_shutdown()
        self.sensor_current_temperature: "MLTemperatureSensor" = None  # type: ignore
        self.select_tracked_sensor = None  # type: ignore
        self.schedule = None  # type: ignore
        self.number_adjust_temperature = None  # type: ignore
        self.number_preset_temperature = None  # type: ignore

    def set_unavailable(self):
        self._mts_active = None
        self._mts_mode = None
        self._mts_onoff = None
        self._mts_payload.clear()
        self.current_humidity = None
        self.current_temperature = None
        self.preset_mode = None
        self.hvac_action = None
        self.hvac_mode = None
        super().set_unavailable()

    def flush_state(self):
        super().flush_state()
        self.schedule.flush_state()

    # interface: ClimateEntity
    async def async_turn_on(self):
        await self.async_request_onoff(1)

    async def async_turn_off(self):
        await self.async_request_onoff(0)

    async def async_set_hvac_mode(self, hvac_mode: climate.HVACMode):
        raise NotImplementedError()

    async def async_set_preset_mode(self, preset_mode: str):
        mode = reverse_lookup(self.MTS_MODE_TO_PRESET_MAP, preset_mode)
        if mode is not None:
            await self.async_request_preset(mode)

    async def async_set_temperature(self, **kwargs):
        raise NotImplementedError()

    # interface: self
    async def async_request_preset(self, mode: int):
        """Implements the protocol to set the Meross thermostat mode"""
        raise NotImplementedError()

    async def async_request_onoff(self, onoff: int):
        """Implements the protocol to turn on the thermostat"""
        raise NotImplementedError()

    def is_mts_scheduled(self):
        raise NotImplementedError()

    def get_ns_adjust(self) -> "NamespaceHandler":
        """
        Returns the correct ns handler for the adjust namespace.
        Used to trigger a poll and the ns which is by default polled
        on a long timeout.
        """
        raise NotImplementedError()

    def _update_current_temperature(self, current_temperature: float | int):
        """
        Common handler for incoming room temperature value
        """
        current_temperature = current_temperature / self.device_scale
        if self.current_temperature != current_temperature:
            self.current_temperature = current_temperature
            self.select_tracked_sensor.check_tracking()
            self.sensor_current_temperature.update_native_value(current_temperature)
            # temp change might be an indication of a calibration so
            # we'll speed up polling for the adjust/calibration ns
            try:
                ns_adjust = self.get_ns_adjust()
                if ns_adjust.polling_epoch_next > (ns_adjust.device.lastresponse + 30):
                    ns_adjust.polling_epoch_next = 0.0
            except:
                # in case the ns is not available for this device
                pass
