import enum
from typing import TYPE_CHECKING

from homeassistant.components import climate

from .helpers import entity as me, reverse_lookup
from .merossclient.protocol import const as mc
from .number import MLConfigNumber
from .select import MtsTrackedSensor
from .sensor import MLTemperatureSensor

if TYPE_CHECKING:
    from typing import ClassVar, Final, Unpack

    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

    from .calendar import MtsSchedule
    from .helpers.device import BaseDevice, Device
    from .helpers.namespaces import NamespaceHandler


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
        ATTR_HVAC_MODE: Final
        ATTR_TEMPERATURE: Final
        ATTR_TARGET_TEMP_HIGH: Final
        ATTR_TARGET_TEMP_LOW: Final

        device_scale: ClassVar[float]
        AdjustNumber: ClassVar[type["MtsTemperatureNumber"]]
        """The specific Adjust/Calibrate number class to instantiate."""
        SetPointNumber: ClassVar[type["MtsSetPointNumber"] | None]
        """The (optional) class for setting up a group of preset setpoints."""
        Schedule: ClassVar[type[MtsSchedule]]
        """The specific Schedule/Calendar class to instantiate."""

        MTS_MODE_TO_PRESET_MAP: ClassVar[dict[int | None, str]]
        """maps device 'mode' value to the HA climate.preset_mode"""
        MTS_MODE_TO_TEMPERATUREKEY_MAP: ClassVar[dict[int | None, str]]
        """maps the current mts mode to the name of temperature setpoint key"""
        PRESET_TO_ICON_MAP: Final[dict[Preset, str]]
        """Used in Number entities for temperatues setpoint."""
        SET_TEMP_FORCE_MANUAL_MODE: Final[bool]
        """Determines the behavior of async_set_temperature."""
        manager: BaseDevice
        number_adjust_temperature: Final["MtsTemperatureNumber"]
        number_preset_temperature: dict[str, "MtsSetPointNumber"]
        schedule: Final[MtsSchedule]
        select_tracked_sensor: Final[MtsTrackedSensor]
        sensor_current_temperature: Final[MLTemperatureSensor]
        _mts_active: bool | None
        _mts_mode: int | None
        _mts_onoff: int | None
        _mts_payload: dict

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
        target_temperature_step: float
        temperature_unit: Final[str]
        translation_key: Final[str]

    PLATFORM = climate.DOMAIN

    ATTR_HVAC_MODE = climate.ATTR_HVAC_MODE
    ATTR_TEMPERATURE = climate.ATTR_TEMPERATURE
    ATTR_TARGET_TEMP_HIGH = climate.ATTR_TARGET_TEMP_HIGH
    ATTR_TARGET_TEMP_LOW = climate.ATTR_TARGET_TEMP_LOW

    ClimateEntityFeature = climate.ClimateEntityFeature
    HVACAction = climate.HVACAction
    HVACMode = climate.HVACMode

    device_scale = 1

    SetPointNumber = None

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
        "target_temperature_step",
        "temperature_unit",
        "_mts_active",
        "_mts_mode",
        "_mts_onoff",
        "_mts_payload",
        "_core_config_update_unsub",
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
        # We need to implement this patch since HA doesn't 'convert' this to
        # °F when the system is configured so...this is likely due to the nature of °F
        # which have an offset to 0°C and so, converting a temperature delta should be done
        # differently (then the actual unit converters). TBH this should be resolved in
        # HA core but right now we'll patch this until better times ..
        self.target_temperature_step = (
            1
            if manager.hass.config.units.temperature_unit
            == self.hac.UnitOfTemperature.FAHRENHEIT
            else 0.5
        )
        self.temperature_unit = self.hac.UnitOfTemperature.CELSIUS
        self._mts_active = None
        self._mts_mode = None
        self._mts_onoff = None
        self._mts_payload = {}
        super().__init__(manager, channel)
        self.number_adjust_temperature = self.__class__.AdjustNumber(self)  # type: ignore
        self.number_preset_temperature = {}
        if preset_number_class := self.__class__.SetPointNumber:
            for preset in MtsClimate.PRESET_TO_ICON_MAP.keys():
                number_preset_temperature = preset_number_class(self, preset)
                self.number_preset_temperature[number_preset_temperature.key_value] = (
                    number_preset_temperature
                )
        self.schedule = self.__class__.Schedule(self)
        self.select_tracked_sensor = MtsTrackedSensor(self)
        self.sensor_current_temperature = MLTemperatureSensor(manager, channel)
        self.sensor_current_temperature.entity_registry_enabled_default = False

        self._core_config_update_unsub = manager.hass.bus.async_listen_once(
            self.hac.EVENT_CORE_CONFIG_UPDATE, self._async_core_config_update
        )

    # interface: MLEntity
    async def async_shutdown(self):
        if self._core_config_update_unsub:
            self._core_config_update_unsub()
            self._core_config_update_unsub = None
        await super().async_shutdown()
        self.sensor_current_temperature = None  # type: ignore
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

    async def _async_core_config_update(self, _event) -> None:
        self.target_temperature_step = (
            1
            if self.manager.hass.config.units.temperature_unit
            == self.hac.UnitOfTemperature.FAHRENHEIT
            else 0.5
        )
        self.flush_state()


class MtsTemperatureNumber(MLConfigNumber):
    """
    Common number entity for representing MTS temperatures configuration
    """

    # HA core entity attributes:
    _attr_suggested_display_precision = 1

    __slots__ = ()

    def __init__(
        self,
        climate: "MtsClimate",
        entitykey: str,
        **kwargs: "Unpack[MLConfigNumber.Args]",
    ):
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

    __slots__ = (
        "climate",
        "icon",
        "key_value",
    )

    def __init__(
        self,
        climate: "MtsClimate",
        preset_mode: "MtsClimate.Preset",
    ):
        self.climate = climate
        self.icon = climate.PRESET_TO_ICON_MAP[preset_mode]
        self.key_value = climate.MTS_MODE_TO_TEMPERATUREKEY_MAP[
            reverse_lookup(climate.MTS_MODE_TO_PRESET_MAP, preset_mode)
        ]
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
            ns_slug = self.ns.slug
            payload = response[mc.KEY_PAYLOAD]
            if ns_slug in payload:
                # by design ns_slug is either "temperature" (mts100) or "mode" (mts200)
                getattr(self.climate, f"_parse_{ns_slug}")(payload[ns_slug][0])

        return response
