from __future__ import annotations

from homeassistant.components.climate import (
    DOMAIN as PLATFORM_CLIMATE,
    ClimateEntity,
)
from homeassistant.components.climate.const import (
    PRESET_AWAY, PRESET_COMFORT, PRESET_SLEEP, SUPPORT_PRESET_MODE, SUPPORT_TARGET_TEMPERATURE,
    CURRENT_HVAC_HEAT, CURRENT_HVAC_IDLE, CURRENT_HVAC_OFF,
    HVAC_MODE_AUTO, HVAC_MODE_HEAT, HVAC_MODE_OFF,
)
from homeassistant.const import (
    TEMP_CELSIUS,
    ATTR_TEMPERATURE,
    DEVICE_CLASS_TEMPERATURE,
)

from .merossclient import const as mc  # mEROSS cONST
from .meross_entity import (
    _MerossEntity,
    platform_setup_entry, platform_unload_entry
)
from .number import (
    MLConfigNumber,
)


async def async_setup_entry(hass: object, config_entry: object, async_add_devices):
    platform_setup_entry(hass, config_entry, async_add_devices, PLATFORM_CLIMATE)

async def async_unload_entry(hass: object, config_entry: object) -> bool:
    return platform_unload_entry(hass, config_entry, PLATFORM_CLIMATE)


PRESET_OFF = 'off'
PRESET_CUSTOM = 'custom'
#PRESET_COMFORT = 'heat'
#PRESET_COOL = 'cool'
#PRESET_ECONOMY = 'economy'
PRESET_AUTO = 'auto'

# when HA requests an HVAC mode we'll map it to a 'preset'
HVAC_TO_PRESET_MAP = {
    HVAC_MODE_OFF: PRESET_OFF,
    HVAC_MODE_HEAT: PRESET_CUSTOM,
    HVAC_MODE_AUTO: PRESET_AUTO
}


class MtsClimate(_MerossEntity, ClimateEntity):

    PLATFORM = PLATFORM_CLIMATE

    _attr_min_temp = 5
    _attr_max_temp = 35
    _attr_target_temperature_step = 0.5

    _attr_target_temperature = None
    _attr_current_temperature = None
    _attr_preset_modes = [PRESET_OFF, PRESET_CUSTOM, PRESET_COMFORT,
                PRESET_SLEEP, PRESET_AWAY, PRESET_AUTO]
    _attr_preset_mode = None
    _attr_hvac_modes = [HVAC_MODE_OFF, HVAC_MODE_HEAT, HVAC_MODE_AUTO]
    _attr_hvac_mode = None
    _attr_hvac_action = None

    _mts_mode = None
    _mts_onoff = None
    _mts_heating = None

    # these mappings are defined in inherited MtsXXX
    # they'll map between mts device 'mode' and HA 'preset'
    MTS_MODE_AUTO : int
    MTS_MODE_TO_PRESET_MAP : dict
    PRESET_TO_TEMPERATUREKEY_MAP : dict


    def update_modes(self) -> None:
        if self._mts_onoff:
            self._attr_preset_mode = self.MTS_MODE_TO_PRESET_MAP.get(self._mts_mode)
            self._attr_hvac_mode = HVAC_MODE_AUTO if self._attr_preset_mode is PRESET_AUTO else HVAC_MODE_HEAT
            self._attr_hvac_action = CURRENT_HVAC_HEAT if self._mts_heating else CURRENT_HVAC_IDLE
        else:
            self._attr_preset_mode = PRESET_OFF
            self._attr_hvac_mode = HVAC_MODE_OFF
            self._attr_hvac_action = CURRENT_HVAC_OFF

        if self.subdevice is not None:
            self._attr_state = self._attr_hvac_mode if self.subdevice.online else None
        else:
            self._attr_state = self._attr_hvac_mode if self.device.online else None

        if self.hass and self.enabled:
            self.async_write_ha_state()


    @property
    def supported_features(self) -> int:
        return SUPPORT_TARGET_TEMPERATURE | SUPPORT_PRESET_MODE

    @property
    def temperature_unit(self) -> str:
        return TEMP_CELSIUS

    @property
    def min_temp(self) -> float:
        return self._attr_min_temp

    @property
    def max_temp(self) -> float:
        return self._attr_max_temp

    @property
    def hvac_modes(self) -> list[str]:
        return self._attr_hvac_modes

    @property
    def hvac_mode(self) -> str:
        return self._attr_hvac_mode

    @property
    def hvac_action(self) -> str | None:
        return self._attr_hvac_action

    @property
    def current_temperature(self) -> float | None:
        return self._attr_current_temperature

    @property
    def target_temperature(self) -> float | None:
        return self._attr_target_temperature

    @property
    def target_temperature_step(self) -> float | None:
        return self._attr_target_temperature_step

    @property
    def preset_modes(self) -> list[str] | None:
        return self._attr_preset_modes

    @property
    def preset_mode(self) -> str | None:
        return self._attr_preset_mode


    async def async_turn_on(self) -> None:
        await self._async_turn_onoff(1)


    async def async_turn_off(self) -> None:
        await self._async_turn_onoff(0)


    async def async_set_hvac_mode(self, hvac_mode: str) -> None:
        if hvac_mode == HVAC_MODE_HEAT:
            # when requesting HEAT we'll just switch ON the MTS
            # while leaving it's own mode (#48) if it's one of
            # the manual modes, else switch it to MTS100MODE_CUSTOM
            # through HVAC_TO_PRESET_MAP
            if self._mts_mode != self.MTS_MODE_AUTO:
                await self._async_turn_onoff(1)
                return

        await self.async_set_preset_mode(HVAC_TO_PRESET_MAP.get(hvac_mode))


    async def async_set_preset_mode(self, preset_mode: str) -> None:
        raise NotImplementedError()


    async def async_set_temperature(self, **kwargs) -> None:
        raise NotImplementedError()


    async def _async_turn_onoff(self, onoff) -> None:
        raise NotImplementedError()



class MtsSetPointNumber(MLConfigNumber):
    """
    Helper entity to configure MTS (thermostats) setpoints
    AKA: Heat(comfort) - Cool(sleep) - Eco(away)
    """
    PRESET_TO_ICON_MAP = {
        PRESET_COMFORT: 'mdi:sun-thermometer',
        PRESET_SLEEP: 'mdi:power-sleep',
        PRESET_AWAY: 'mdi:bag-checked',
    }

    def __init__(self, climate: MtsClimate, preset_mode: str):
        self._climate = climate
        self._preset_mode = preset_mode
        self._key = climate.PRESET_TO_TEMPERATUREKEY_MAP[preset_mode]
        self._attr_icon = self.PRESET_TO_ICON_MAP[preset_mode]
        super().__init__(
            climate.device,
            climate.channel,
            f"config_{mc.KEY_TEMPERATURE}_{self._key}",
            DEVICE_CLASS_TEMPERATURE,
            climate.subdevice
        )

    @property
    def name(self) -> str:
        return f"{self._climate.name} - {self._preset_mode} {DEVICE_CLASS_TEMPERATURE}"

    @property
    def step(self) -> float:
        return self._climate._attr_target_temperature_step

    @property
    def min_value(self) -> float:
        return self._climate._attr_min_temp

    @property
    def max_value(self) -> float:
        return self._climate._attr_max_temp

