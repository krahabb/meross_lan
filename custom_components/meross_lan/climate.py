import typing

from homeassistant.components import climate

from . import meross_entity as me
from .helpers import reverse_lookup
from .merossclient import const as mc
from .select import MtsTrackedSensor
from .sensor import UnitOfTemperature

if typing.TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

    from .calendar import MtsSchedule
    from .meross_device import MerossDeviceBase
    from .number import MtsSetPointNumber, MtsTemperatureNumber


async def async_setup_entry(
    hass: "HomeAssistant", config_entry: "ConfigEntry", async_add_devices
):
    me.platform_setup_entry(hass, config_entry, async_add_devices, climate.DOMAIN)


class MtsClimate(me.MerossEntity, climate.ClimateEntity):
    PLATFORM = climate.DOMAIN

    ATTR_TEMPERATURE: typing.Final = climate.ATTR_TEMPERATURE
    TEMP_CELSIUS: typing.Final = UnitOfTemperature.CELSIUS

    HVACAction: typing.Final = climate.HVACAction
    HVACMode: typing.Final = climate.HVACMode

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

    manager: "MerossDeviceBase"
    number_adjust_temperature: typing.Final["MtsTemperatureNumber"]
    number_preset_temperature: dict[str, "MtsSetPointNumber"]
    schedule: typing.Final["MtsSchedule"]
    select_tracked_sensor: typing.Final["MtsTrackedSensor"]

    # HA core entity attributes:
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
    _enable_turn_on_off_backwards_compatibility = False
    target_temperature: float | None
    target_temperature_step: float = 0.5
    temperature_unit: str = TEMP_CELSIUS
    translation_key = "mts_climate"

    __slots__ = (
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
        "_mts_adjust_offset",
        "number_adjust_temperature",
        "number_preset_temperature",
        "schedule",
        "select_tracked_sensor",
    )

    def __init__(
        self,
        manager: "MerossDeviceBase",
        channel: object,
        adjust_number_class: typing.Type["MtsTemperatureNumber"],
        preset_number_class: typing.Type["MtsSetPointNumber"] | None,
        calendar_class: typing.Type["MtsSchedule"],
    ):
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
        self._mts_adjust_offset = 0
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

    # interface: MerossEntity
    async def async_shutdown(self):
        await super().async_shutdown()
        self.select_tracked_sensor = None  # type: ignore
        self.schedule = None  # type: ignore
        self.number_adjust_temperature = None  # type: ignore
        self.number_preset_temperature = None  # type: ignore

    def set_unavailable(self):
        self._mts_active = None
        self._mts_mode = None
        self._mts_onoff = None
        self._mts_payload = {}
        self.preset_mode = None
        self.hvac_action = None
        self.hvac_mode = None
        super().set_unavailable()

    def flush_state(self):
        self.preset_mode = self.MTS_MODE_TO_PRESET_MAP.get(self._mts_mode)
        super().flush_state()
        self.schedule.flush_state()

    # interface: ClimateEntity
    async def async_turn_on(self):
        await self.async_request_onoff(1)

    async def async_turn_off(self):
        await self.async_request_onoff(0)

    async def async_set_hvac_mode(self, hvac_mode: "MtsClimate.HVACMode"):
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

    def _parse(self, p_temperature: dict):
        """
        This the handler for the default payload carrying the gross state of the climate entity.
        It is dynamically binded to the self.namespace NamespaceHandler on __init__.
        By convention every implementation used to define this as _parse_'key_namespace' but
        it is not needed anymore since that was due to the legacy message handle/parse engine
        """
        raise NotImplementedError()
