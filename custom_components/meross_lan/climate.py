from __future__ import annotations

import typing

from homeassistant import const as hac
from homeassistant.components import climate

from . import meross_entity as me
from .merossclient import const as mc  # mEROSS cONST
from .number import MLConfigNumber
from .select import MtsTrackedSensor

if typing.TYPE_CHECKING:
    from typing import ClassVar, Final

    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

    from .binary_sensor import MLBinarySensor
    from .calendar import MtsSchedule
    from .meross_device import MerossDeviceBase


async def async_setup_entry(
    hass: HomeAssistant, config_entry: ConfigEntry, async_add_devices
):
    me.platform_setup_entry(hass, config_entry, async_add_devices, climate.DOMAIN)


class MtsClimate(me.MerossEntity, climate.ClimateEntity):
    PLATFORM = climate.DOMAIN

    ATTR_TEMPERATURE: Final = climate.ATTR_TEMPERATURE
    TEMP_CELSIUS: Final = hac.TEMP_CELSIUS

    HVACAction = climate.HVACAction
    HVACMode = climate.HVACMode

    PRESET_CUSTOM: Final = "custom"
    PRESET_COMFORT: Final = "comfort"
    PRESET_SLEEP: Final = "sleep"
    PRESET_AWAY: Final = "away"
    PRESET_AUTO: Final = "auto"

    manager: MerossDeviceBase
    binary_sensor_window: Final[MLBinarySensor]
    number_adjust_temperature: Final[MLConfigNumber]
    number_away_temperature: Final[MtsSetPointNumber]
    number_comfort_temperature: Final[MtsSetPointNumber]
    number_sleep_temperature: Final[MtsSetPointNumber]
    schedule: Final[MtsSchedule]
    select_tracked_sensor: Final[MtsTrackedSensor]

    _attr_preset_modes: Final = [
        PRESET_CUSTOM,
        PRESET_COMFORT,
        PRESET_SLEEP,
        PRESET_AWAY,
        PRESET_AUTO,
    ]
    _attr_supported_features: Final = (
        climate.ClimateEntityFeature.PRESET_MODE
        | climate.ClimateEntityFeature.TARGET_TEMPERATURE
    )

    # these mappings are defined in inherited MtsXXX
    # they'll map between mts device 'mode' and HA 'preset'
    MTS_MODE_TO_PRESET_MAP: ClassVar[dict[int, str]]
    PRESET_TO_TEMPERATUREKEY_MAP: ClassVar[dict[str, str]]
    # in general Mts thermostats are only heating..MTS200 with 'summer mode' could override this
    MTS_HVAC_MODES: Final = [HVACMode.OFF, HVACMode.HEAT]

    __slots__ = (
        "_attr_current_temperature",
        "_attr_hvac_action",
        "_attr_hvac_mode",
        "_attr_hvac_modes",
        "_attr_max_temp",
        "_attr_min_temp",
        "_attr_preset_mode",
        "_attr_target_temperature",
        "_mts_active",
        "_mts_mode",
        "_mts_onoff",
        "_mts_adjust_offset",
        "_mts_adjusted_temperature",
        "binary_sensor_window",
        "number_adjust_temperature",
        "number_comfort_temperature",
        "number_sleep_temperature",
        "number_away_temperature",
        "schedule",
        "select_tracked_sensor",
    )

    def __init__(
        self,
        manager: MerossDeviceBase,
        channel: object,
        binary_sensor_window: MLBinarySensor,
        number_adjust_temperature: MLConfigNumber,
        preset_number_class: typing.Type[MtsSetPointNumber],
        calendar_class: typing.Type[MtsSchedule],
    ):
        self._attr_current_temperature = None
        self._attr_hvac_action = None
        self._attr_hvac_mode = None
        self._attr_hvac_modes = self.MTS_HVAC_MODES
        self._attr_max_temp = 35
        self._attr_min_temp = 5
        self._attr_preset_mode = None
        self._attr_target_temperature = None
        self._mts_active = None
        self._mts_mode: int | None = None
        self._mts_onoff: int | None = None
        self._mts_adjust_offset = 0
        self._mts_adjusted_temperature = {}
        super().__init__(manager, channel, None, None)
        self.binary_sensor_window = binary_sensor_window
        self.number_adjust_temperature = number_adjust_temperature
        self.number_away_temperature = preset_number_class(self, MtsClimate.PRESET_AWAY)
        self.number_comfort_temperature = preset_number_class(
            self, MtsClimate.PRESET_COMFORT
        )
        self.number_sleep_temperature = preset_number_class(
            self, MtsClimate.PRESET_SLEEP
        )
        self.schedule = calendar_class(self)
        self.select_tracked_sensor = MtsTrackedSensor(self)

    # interface: MerossEntity
    async def async_shutdown(self):
        self.select_tracked_sensor = None  # type: ignore
        self.schedule = None  # type: ignore
        self.number_sleep_temperature = None  # type: ignore
        self.number_comfort_temperature = None  # type: ignore
        self.number_away_temperature = None  # type: ignore
        self.number_adjust_temperature = None  # type: ignore
        self.binary_sensor_window = None  # type: ignore
        await super().async_shutdown()

    def set_unavailable(self):
        self._mts_active = None
        self._mts_mode = None
        self._mts_onoff = None
        self._mts_adjusted_temperature = {}
        super().set_unavailable()

    # interface: ClimateEntity
    @property
    def supported_features(self):
        return self._attr_supported_features

    @property
    def temperature_unit(self):
        return MtsClimate.TEMP_CELSIUS

    @property
    def min_temp(self):
        return self._attr_min_temp

    @property
    def max_temp(self):
        return self._attr_max_temp

    @property
    def hvac_modes(self):
        return self._attr_hvac_modes

    @property
    def hvac_mode(self):
        return self._attr_hvac_mode

    @property
    def hvac_action(self):
        return self._attr_hvac_action

    @property
    def current_temperature(self):
        return self._attr_current_temperature

    @property
    def target_temperature(self):
        return self._attr_target_temperature

    @property
    def target_temperature_step(self):
        return 0.5

    @property
    def preset_modes(self):
        return self._attr_preset_modes

    @property
    def preset_mode(self):
        return self._attr_preset_mode

    @property
    def translation_key(self):
        return "mts_climate"

    async def async_turn_on(self):
        await self.async_request_onoff(1)

    async def async_turn_off(self):
        await self.async_request_onoff(0)

    async def async_set_preset_mode(self, preset_mode: str):
        raise NotImplementedError()

    async def async_set_temperature(self, **kwargs):
        raise NotImplementedError()

    # interface: self
    async def async_request_onoff(self, onoff: int):
        raise NotImplementedError()

    def is_mts_scheduled(self):
        raise NotImplementedError()

    def update_mts_state(self):
        self._attr_state = self._attr_hvac_mode if self.manager.online else None
        if self._hass_connected:
            self._async_write_ha_state()
        self.schedule.update_mts_state()


class MtsSetPointNumber(MLConfigNumber):
    """
    Helper entity to configure MTS (thermostats) setpoints
    AKA: Heat(comfort) - Cool(sleep) - Eco(away)
    """

    PRESET_TO_ICON_MAP: Final = {
        MtsClimate.PRESET_COMFORT: "mdi:sun-thermometer",
        MtsClimate.PRESET_SLEEP: "mdi:power-sleep",
        MtsClimate.PRESET_AWAY: "mdi:bag-checked",
    }

    __slots__ = ("climate",)

    def __init__(self, climate: MtsClimate, preset_mode: str):
        self.climate = climate
        self._preset_mode = preset_mode
        self.key_value = climate.PRESET_TO_TEMPERATUREKEY_MAP[preset_mode]
        self._attr_icon = MtsSetPointNumber.PRESET_TO_ICON_MAP[preset_mode]
        self._attr_name = f"{preset_mode} {MLConfigNumber.DeviceClass.TEMPERATURE}"
        super().__init__(
            climate.manager,
            climate.channel,
            f"config_{mc.KEY_TEMPERATURE}_{self.key_value}",
            MLConfigNumber.DeviceClass.TEMPERATURE,
        )

    @property
    def native_max_value(self):
        return self.climate._attr_max_temp

    @property
    def native_min_value(self):
        return self.climate._attr_min_temp

    @property
    def native_step(self):
        return self.climate.target_temperature_step

    @property
    def native_unit_of_measurement(self):
        return MtsClimate.TEMP_CELSIUS

    @property
    def device_scale(self):
        return mc.MTS_TEMP_SCALE

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
                getattr(self.climate, f"_parse_{key_namespace}")(
                    payload[key_namespace][0]
                )

        return response
