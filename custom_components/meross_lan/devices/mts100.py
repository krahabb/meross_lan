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
from homeassistant.components.number import (
    DOMAIN as PLATFORM_NUMBER,
    NumberEntity,
)
from homeassistant.const import (
    DEVICE_CLASS_TEMPERATURE,
    TEMP_CELSIUS,
)

from ..meross_entity import (
    _MerossEntity,
    ENTITY_CATEGORY_CONFIG,
)
from ..merossclient import const as mc  # mEROSS cONST


MTS100_MODE_CUSTOM = 0
MTS100_MODE_COMFORT = 1 # aka 'Heat'
MTS100_MODE_SLEEP = 2 # aka 'Cool'
MTS100_MODE_AWAY = 4 # aka 'Economy'
MTS100_MODE_AUTO = 3


PRESET_OFF = 'off'
PRESET_CUSTOM = 'custom'
#PRESET_COMFORT = 'heat'
#PRESET_COOL = 'cool'
#PRESET_ECONOMY = 'economy'
PRESET_AUTO = 'auto'

# map mts100 mode enums to HA preset keys
MODE_TO_PRESET_MAP = {
    MTS100_MODE_CUSTOM: PRESET_CUSTOM,
    MTS100_MODE_COMFORT: PRESET_COMFORT,
    MTS100_MODE_SLEEP: PRESET_SLEEP,
    MTS100_MODE_AWAY: PRESET_AWAY,
    MTS100_MODE_AUTO: PRESET_AUTO
}
# reverse map
PRESET_TO_MODE_MAP = {
    PRESET_CUSTOM: MTS100_MODE_CUSTOM,
    PRESET_COMFORT: MTS100_MODE_COMFORT,
    PRESET_SLEEP: MTS100_MODE_SLEEP,
    PRESET_AWAY: MTS100_MODE_AWAY,
    PRESET_AUTO: MTS100_MODE_AUTO
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

class Mts100Climate(_MerossEntity, ClimateEntity):

    PLATFORM = PLATFORM_CLIMATE

    _attr_target_temperature = None
    _attr_current_temperature = None
    _attr_preset_modes = [PRESET_OFF, PRESET_CUSTOM, PRESET_COMFORT,
                PRESET_SLEEP, PRESET_AWAY, PRESET_AUTO]
    _attr_preset_mode = None
    _attr_hvac_modes = [HVAC_MODE_OFF, HVAC_MODE_HEAT, HVAC_MODE_AUTO]
    _attr_hvac_mode = None
    _attr_hvac_action = None

    mts100_mode = None
    mts100_onoff = None
    mts100_heating = None

    def __init__(self, subdevice: 'MerossSubDevice'):
        super().__init__(subdevice.hub, subdevice.id, None, subdevice)


    def update_modes(self) -> None:
        if self.mts100_onoff:
            self._attr_hvac_mode = HVAC_MODE_AUTO if self.mts100_mode == MTS100_MODE_AUTO else HVAC_MODE_HEAT
            self._attr_hvac_action = CURRENT_HVAC_HEAT if self.mts100_heating else CURRENT_HVAC_IDLE
            self._attr_preset_mode = MODE_TO_PRESET_MAP.get(self.mts100_mode)
        else:
            self._attr_hvac_mode = HVAC_MODE_OFF
            self._attr_hvac_action = CURRENT_HVAC_OFF
            self._attr_preset_mode = PRESET_OFF

        self._attr_state = self._attr_hvac_mode if self.subdevice.online else None

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
        return self.subdevice.temperature_min

    @property
    def max_temp(self) -> float:
        return self.subdevice.temperature_max

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
        return self.subdevice.temperature_step

    @property
    def preset_modes(self) -> list[str] | None:
        return self._attr_preset_modes

    @property
    def preset_mode(self) -> str | None:
        return self._attr_preset_mode

    async def async_set_temperature(self, **kwargs) -> None:
        t = kwargs.get('temperature')
        key = PRESET_TO_TEMPKEY_MAP[self._attr_preset_mode or PRESET_CUSTOM]

        def _ack_callback():
            self._attr_target_temperature = t
            self.update_modes()

        self.device.request(
            mc.NS_APPLIANCE_HUB_MTS100_TEMPERATURE,
            mc.METHOD_SET,
            {mc.KEY_TEMPERATURE: [{mc.KEY_ID: self.id, key: t * 10 + 1}]}, # the device rounds down ?!
            _ack_callback
        )


    async def async_set_hvac_mode(self, hvac_mode: str) -> None:
        if hvac_mode == HVAC_MODE_HEAT:
            # when requesting HEAT we'll just switch ON the MTS
            # while leaving it's own mode (#48) if it's one of
            # the manual modes, else switch it to MTS100MODE_CUSTOM
            # through HVAC_TO_PRESET_MAP
            if self.mts100_mode != MTS100_MODE_AUTO:
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
                    self.mts100_mode = mode
                    self.update_modes()

                await self.device.async_http_request(
                    mc.NS_APPLIANCE_HUB_MTS100_MODE,
                    mc.METHOD_SET,
                    {mc.KEY_MODE: [{mc.KEY_ID: self.id, mc.KEY_STATE: mode}]},
                    _ack_callback
                )

                if not self.mts100_onoff:
                    await self.async_turn_on()


    async def async_turn_on(self) -> None:
        def _ack_callback():
            self.mts100_onoff = 1
            self.update_modes()

        #same as DND: force http request to get a consistent acknowledge
        #the device will PUSH anyway a state update when the valve actually switches
        #but this way we'll update the UI consistently right after setting mode
        await self.device.async_http_request(
            mc.NS_APPLIANCE_HUB_TOGGLEX,
            mc.METHOD_SET,
            {mc.KEY_TOGGLEX: [{mc.KEY_ID: self.id, mc.KEY_ONOFF: 1}]},
            _ack_callback
        )


    async def async_turn_off(self) -> None:
        def _ack_callback():
            self.mts100_onoff = 0
            self.update_modes()

        await self.device.async_http_request(
            mc.NS_APPLIANCE_HUB_TOGGLEX,
            mc.METHOD_SET,
            {mc.KEY_TOGGLEX: [{mc.KEY_ID: self.id, mc.KEY_ONOFF: 0}]},
            _ack_callback
        )



PRESET_TO_ICON_MAP = {
    PRESET_COMFORT: 'mdi:sun-thermometer',
    PRESET_SLEEP: 'mdi:power-sleep',
    PRESET_AWAY: 'mdi:bag-checked',
}


class Mts100SetPointNumber(_MerossEntity, NumberEntity):

    PLATFORM = PLATFORM_NUMBER


    def __init__(self, subdevice: "MerossSubDevice", preset_mode: str):
        self._preset_mode = preset_mode
        self._key = PRESET_TO_TEMPKEY_MAP[preset_mode]
        self._attr_icon = PRESET_TO_ICON_MAP[preset_mode]
        super().__init__(
            subdevice.hub,
            f"{subdevice.id}_config_{mc.KEY_TEMPERATURE}_{self._key}",
            DEVICE_CLASS_TEMPERATURE,
            subdevice
        )


    @property
    def entity_category(self) -> str | None:
        return ENTITY_CATEGORY_CONFIG

    @property
    def name(self) -> str:
        return f"{self.subdevice.name} - {self._preset_mode} {DEVICE_CLASS_TEMPERATURE}"

    @property
    def step(self) -> float:
        return self.subdevice.temperature_step

    @property
    def min_value(self) -> float:
        return self.subdevice.temperature_min

    @property
    def max_value(self) -> float:
        return self.subdevice.temperature_max

    @property
    def value(self) -> float | None:
        return self._attr_state


    async def async_set_value(self, value: float) -> None:

        self.device.request(
            mc.NS_APPLIANCE_HUB_MTS100_TEMPERATURE,
            mc.METHOD_SET,
            {
                mc.KEY_TEMPERATURE: [
                    {
                        mc.KEY_ID: self.subdevice.id,
                        self._key: int(value * 10 + 1)
                    }
                ]
            },
        )


    def update_value(self, value):
        self.update_state(value / 10)
