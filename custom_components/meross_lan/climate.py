import typing

from homeassistant.components import climate

from .helpers import entity as me, reverse_lookup
from .merossclient.protocol import const as mc
from .select import MtsTrackedSensor
from .sensor import MLTemperatureSensor

if typing.TYPE_CHECKING:
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
    PLATFORM = climate.DOMAIN

    ATTR_TEMPERATURE: typing.Final = climate.ATTR_TEMPERATURE
    TEMP_CELSIUS: typing.Final = me.MLEntity.hac.UnitOfTemperature.CELSIUS

    HVACAction: typing.TypeAlias = climate.HVACAction
    HVACMode: typing.TypeAlias = climate.HVACMode

    PRESET_CUSTOM: typing.Final = "custom"
    PRESET_COMFORT: typing.Final = "comfort"
    PRESET_SLEEP: typing.Final = "sleep"
    PRESET_AWAY: typing.Final = "away"
    PRESET_AUTO: typing.Final = "auto"

    device_scale: typing.ClassVar[float] = mc.MTS_TEMP_SCALE

    MTS_MODE_TO_PRESET_MAP: typing.ClassVar[dict[int | None, str]]
    """maps device 'mode' value to the HA climate.preset_mode"""
    MTS_MODE_TO_TEMPERATUREKEY_MAP: typing.ClassVar[dict[int | None, str]]
    """maps the current mts mode to the name of temperature setpoint key"""
    PRESET_TO_ICON_MAP: typing.Final = {
        PRESET_COMFORT: "mdi:sun-thermometer",
        PRESET_SLEEP: "mdi:power-sleep",
        PRESET_AWAY: "mdi:bag-checked",
    }
    """lookups used in MtsSetpointNumber to map a pretty icon to the setpoint entity"""

    SET_TEMP_FORCE_MANUAL_MODE = True
    """Determines the behavior of async_set_temperature."""

    manager: "BaseDevice"
    number_adjust_temperature: typing.Final["MtsTemperatureNumber"]
    number_preset_temperature: dict[str, "MtsSetPointNumber"]
    schedule: typing.Final["MtsSchedule"]
    select_tracked_sensor: typing.Final["MtsTrackedSensor"]

    # HA core entity attributes:
    current_humidity: float | None
    current_temperature: float | None
    hvac_action: climate.HVACAction | None
    hvac_mode: climate.HVACMode | None
    hvac_modes: list[climate.HVACMode] = [HVACMode.OFF, HVACMode.HEAT]
    max_temp: float
    min_temp: float
    preset_mode: str | None
    preset_modes: list[str] = [
        PRESET_CUSTOM,
        PRESET_COMFORT,
        PRESET_SLEEP,
        PRESET_AWAY,
        PRESET_AUTO,
    ]
    supported_features: climate.ClimateEntityFeature = (
        climate.ClimateEntityFeature.PRESET_MODE
        | climate.ClimateEntityFeature.TARGET_TEMPERATURE
        | getattr(climate.ClimateEntityFeature, "TURN_OFF", 0)
        | getattr(climate.ClimateEntityFeature, "TURN_ON", 0)
    )
    _enable_turn_on_off_backwards_compatibility = (
        False  # compatibility flag (see HA core climate)
    )
    target_temperature: float | None
    target_temperature_step: float = 0.5
    temperature_unit: str = TEMP_CELSIUS
    translation_key = "mts_climate"

    __slots__ = (
        "current_humidity",
        "current_temperature",
        "hvac_action",
        "hvac_mode",
        "max_temp",
        "min_temp",
        "preset_mode",
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
        self.max_temp = 35
        self.min_temp = 5
        self.preset_mode = None
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
            await self.async_request_mode(mode)

    async def async_set_temperature(self, **kwargs):
        raise NotImplementedError()

    # interface: self
    async def async_request_mode(self, mode: int):
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
