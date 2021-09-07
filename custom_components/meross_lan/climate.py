from __future__ import annotations

from homeassistant.components.climate import (
    DOMAIN as PLATFORM_CLIMATE,
    ClimateEntity
)
from homeassistant.components.climate.const import (
    PRESET_AWAY, PRESET_COMFORT, PRESET_SLEEP, SUPPORT_PRESET_MODE, SUPPORT_TARGET_TEMPERATURE,
    CURRENT_HVAC_HEAT, CURRENT_HVAC_IDLE, CURRENT_HVAC_OFF,
    HVAC_MODE_AUTO, HVAC_MODE_HEAT, HVAC_MODE_OFF,
)
from homeassistant.const import TEMP_CELSIUS


from .merossclient import const as mc  # mEROSS cONST
from .meross_entity import _MerossHubEntity, platform_setup_entry, platform_unload_entry


async def async_setup_entry(hass: object, config_entry: object, async_add_devices):
    platform_setup_entry(hass, config_entry, async_add_devices, PLATFORM_CLIMATE)

async def async_unload_entry(hass: object, config_entry: object) -> bool:
    return platform_unload_entry(hass, config_entry, PLATFORM_CLIMATE)


MTS100MODE_CUSTOM = 0
MTS100MODE_COMFORT = 1 # aka 'Heat'
MTS100MODE_SLEEP = 2 # aka 'Cool'
MTS100MODE_AWAY = 4 # aka 'Economy'
MTS100MODE_AUTO = 3

PRESET_OFF = 'off'
PRESET_CUSTOM = 'custom'
#PRESET_COMFORT = 'heat'
#PRESET_COOL = 'cool'
#PRESET_ECONOMY = 'economy'
PRESET_AUTO = 'auto'

# map mts100 mode enums to HA preset keys
MODE_TO_PRESET_MAP = {
    MTS100MODE_CUSTOM: PRESET_CUSTOM,
    MTS100MODE_COMFORT: PRESET_COMFORT,
    MTS100MODE_SLEEP: PRESET_SLEEP,
    MTS100MODE_AWAY: PRESET_AWAY,
    MTS100MODE_AUTO: PRESET_AUTO
}
# reverse map
PRESET_TO_MODE_MAP = {
    PRESET_CUSTOM: MTS100MODE_CUSTOM,
    PRESET_COMFORT: MTS100MODE_COMFORT,
    PRESET_SLEEP: MTS100MODE_SLEEP,
    PRESET_AWAY: MTS100MODE_AWAY,
    PRESET_AUTO: MTS100MODE_AUTO
}
# when HA requests an HVAC mode we'll map it to a 'preset'
HVAC_TO_PRESET_MAP = {
    HVAC_MODE_OFF: PRESET_OFF,
    HVAC_MODE_HEAT: PRESET_CUSTOM,
    HVAC_MODE_AUTO: PRESET_AUTO
}
# when setting target temp we'll set an appropriate payload key
# for the mts100 depending on current 'preset' mode.
# if mts100 is in any of 'off', 'auto' we just set the 'custom'
# target temp but of course the valve will not follow
# this temp since it's mode is not set to follow a manual set
PRESET_TO_TEMPKEY_MAP = {
    PRESET_OFF: mc.KEY_CUSTOM,
    PRESET_CUSTOM: mc.KEY_CUSTOM,
    PRESET_COMFORT: mc.KEY_COMFORT,
    PRESET_SLEEP: mc.KEY_ECONOMY,
    PRESET_AWAY: mc.KEY_AWAY,
    PRESET_AUTO: mc.KEY_CUSTOM
}

class Mts100Climate(_MerossHubEntity, ClimateEntity):

    PLATFORM = PLATFORM_CLIMATE

    def __init__(self, subdevice: 'MerossSubDevice'):
        super().__init__(subdevice, subdevice.id, None)
        self._min_temp = None
        self._max_temp = None
        self._target_temperature = None
        self._current_temperature = None
        self._preset_mode = None
        self._hvac_mode = None
        self._hvac_action = None
        self._mts100_mode = None
        self._mts100_onoff = None
        self._mts100_heating = None


    def update_modes(self) -> None:
        if self._mts100_onoff:
            self._hvac_mode = HVAC_MODE_AUTO if self._mts100_mode == MTS100MODE_AUTO else HVAC_MODE_HEAT
            self._hvac_action = CURRENT_HVAC_HEAT if self._mts100_heating else CURRENT_HVAC_IDLE
            self._preset_mode = MODE_TO_PRESET_MAP.get(self._mts100_mode)
        else:
            self._hvac_mode = HVAC_MODE_OFF
            self._hvac_action = CURRENT_HVAC_OFF
            self._preset_mode = PRESET_OFF

        self._state = self._hvac_mode if self.subdevice.online else None

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
        return self._min_temp

    @property
    def max_temp(self) -> float:
        return self._max_temp

    @property
    def hvac_modes(self) -> list[str]:
        return [HVAC_MODE_OFF, HVAC_MODE_HEAT, HVAC_MODE_AUTO]

    @property
    def hvac_mode(self) -> str:
        return self._hvac_mode

    @property
    def hvac_action(self) -> str | None:
        return self._hvac_action

    @property
    def current_temperature(self) -> float | None:
        return self._current_temperature

    @property
    def target_temperature(self) -> float | None:
        return self._target_temperature

    @property
    def target_temperature_step(self) -> float | None:
        return 0.5

    @property
    def preset_mode(self) -> str | None:
        return self._preset_mode

    @property
    def preset_modes(self) -> list[str] | None:
        return [PRESET_OFF, PRESET_CUSTOM, PRESET_COMFORT,
                PRESET_SLEEP, PRESET_AWAY, PRESET_AUTO]


    async def async_set_temperature(self, **kwargs) -> None:
        t = kwargs.get('temperature')
        key = PRESET_TO_TEMPKEY_MAP[self._preset_mode or PRESET_CUSTOM]

        def _ack_callback():
            self._target_temperature = t
            self.update_modes()

        self._device.request(
            mc.NS_APPLIANCE_HUB_MTS100_TEMPERATURE,
            mc.METHOD_SET,
            {mc.KEY_TEMPERATURE: [{mc.KEY_ID: self.subdevice.id, key: t * 10 + 1}]}, # the device rounds down ?!
            _ack_callback
        )


    async def async_set_hvac_mode(self, hvac_mode: str) -> None:
        if hvac_mode == HVAC_MODE_HEAT:
            # when requesting HEAT we'll just switch ON the MTS
            # while leaving it's own mode (#48) if it's one of
            # the manual modes, else switch it to MTS100MODE_CUSTOM
            # through HVAC_TO_PRESET_MAP
            if self._mts100_mode != MTS100MODE_AUTO:
                await self.async_turn_on()
                return

        await self.async_set_preset_mode(HVAC_TO_PRESET_MAP.get(hvac_mode))


    async def async_set_preset_mode(self, preset_mode: str) -> None:
        if preset_mode == PRESET_OFF:
            await self.async_turn_off()
        else:
            mode = PRESET_TO_MODE_MAP.get(preset_mode)
            if mode is not None:

                def _ack_callback():
                    self._mts100_mode = mode
                    self.update_modes()

                await self._device.async_http_request(
                    mc.NS_APPLIANCE_HUB_MTS100_MODE,
                    mc.METHOD_SET,
                    {mc.KEY_MODE: [{mc.KEY_ID: self.subdevice.id, mc.KEY_STATE: mode}]},
                    _ack_callback
                )

                if not self._mts100_onoff:
                    await self.async_turn_on()


    async def async_turn_on(self) -> None:
        def _ack_callback():
            self._mts100_onoff = 1
            self.update_modes()

        #same as DND: force http request to get a consistent acknowledge
        #the device will PUSH anyway a state update when the valve actually switches
        #but this way we'll update the UI consistently right after setting mode
        await self._device.async_http_request(
            mc.NS_APPLIANCE_HUB_TOGGLEX,
            mc.METHOD_SET,
            {mc.KEY_TOGGLEX: [{mc.KEY_ID: self.subdevice.id, mc.KEY_ONOFF: 1}]},
            _ack_callback
        )


    async def async_turn_off(self) -> None:
        def _ack_callback():
            self._mts100_onoff = 0
            self.update_modes()

        await self._device.async_http_request(
            mc.NS_APPLIANCE_HUB_TOGGLEX,
            mc.METHOD_SET,
            {mc.KEY_TOGGLEX: [{mc.KEY_ID: self.subdevice.id, mc.KEY_ONOFF: 0}]},
            _ack_callback
        )
