from __future__ import annotations

from ..climate import (
    MtsClimate, MtsSetPointNumber,
    PRESET_OFF, PRESET_CUSTOM, PRESET_COMFORT, PRESET_SLEEP, PRESET_AWAY, PRESET_AUTO,
    ATTR_TEMPERATURE,
)
from ..merossclient import const as mc  # mEROSS cONST


class Mts100Climate(MtsClimate):

    MTS_MODE_AUTO = mc.MTS100_MODE_AUTO
    MTS_MODE_TO_PRESET_MAP = {
        mc.MTS100_MODE_CUSTOM: PRESET_CUSTOM,
        mc.MTS100_MODE_HEAT: PRESET_COMFORT,
        mc.MTS100_MODE_COOL: PRESET_SLEEP,
        mc.MTS100_MODE_ECO: PRESET_AWAY,
        mc.MTS100_MODE_AUTO: PRESET_AUTO
    }

    PRESET_TO_MTS_MODE_MAP = {
        PRESET_CUSTOM: mc.MTS100_MODE_CUSTOM,
        PRESET_COMFORT: mc.MTS100_MODE_HEAT,
        PRESET_SLEEP: mc.MTS100_MODE_COOL,
        PRESET_AWAY: mc.MTS100_MODE_ECO,
        PRESET_AUTO: mc.MTS100_MODE_AUTO
    }
    # when setting target temp we'll set an appropriate payload key
    # for the mts100 depending on current 'preset' mode.
    # if mts100 is in any of 'off', 'auto' we just set the 'custom'
    # target temp but of course the valve will not follow
    # this temp since it's mode is not set to follow a manual set
    PRESET_TO_TEMPERATUREKEY_MAP = {
        PRESET_OFF: mc.KEY_CUSTOM,
        PRESET_CUSTOM: mc.KEY_CUSTOM,
        PRESET_COMFORT: mc.KEY_COMFORT,
        PRESET_SLEEP: mc.KEY_ECONOMY,
        PRESET_AWAY: mc.KEY_AWAY,
        PRESET_AUTO: mc.KEY_CUSTOM
    }


    def __init__(self, subdevice: 'MerossSubDevice'):
        super().__init__(subdevice.hub, subdevice.id, None, None, subdevice)
        self.number_comfort_temperature = Mts100SetPointNumber(
            self, PRESET_COMFORT)
        self.number_sleep_temperature = Mts100SetPointNumber(
            self, PRESET_SLEEP)
        self.number_away_temperature = Mts100SetPointNumber(
            self, PRESET_AWAY)


    async def async_set_preset_mode(self, preset_mode: str) -> None:
        if preset_mode == PRESET_OFF:
            await self._async_turn_onoff(0)
        else:
            mode = self.PRESET_TO_MTS_MODE_MAP.get(preset_mode)
            if mode is not None:

                def _ack_callback():
                    self._mts_mode = mode
                    self.update_modes()

                await self.device.async_http_request(
                    mc.NS_APPLIANCE_HUB_MTS100_MODE,
                    mc.METHOD_SET,
                    {mc.KEY_MODE: [{mc.KEY_ID: self.id, mc.KEY_STATE: mode}]},
                    _ack_callback
                )

                if not self._mts_onoff:
                    await self._async_turn_onoff(1)


    async def async_set_temperature(self, **kwargs) -> None:
        t = kwargs.get(ATTR_TEMPERATURE)
        key = self.PRESET_TO_TEMPERATUREKEY_MAP[self._attr_preset_mode or PRESET_CUSTOM]

        def _ack_callback():
            self._attr_target_temperature = t
            self.update_modes()

        # when sending a temp this way the device will automatically
        # exit auto mode if needed
        await self.device.async_http_request(
            mc.NS_APPLIANCE_HUB_MTS100_TEMPERATURE,
            mc.METHOD_SET,
            {mc.KEY_TEMPERATURE: [{mc.KEY_ID: self.id, key: int(t * 10)}]}, # the device rounds down ?!
            _ack_callback
        )


    async def _async_turn_onoff(self, onoff) -> None:
        def _ack_callback():
            self._mts_onoff = onoff
            self.update_modes()
        #same as DND: force http request to get a consistent acknowledge
        #the device will PUSH anyway a state update when the valve actually switches
        #but this way we'll update the UI consistently right after setting mode
        await self.device.async_http_request(
            mc.NS_APPLIANCE_HUB_TOGGLEX,
            mc.METHOD_SET,
            {mc.KEY_TOGGLEX: [{mc.KEY_ID: self.id, mc.KEY_ONOFF: onoff}]},
            _ack_callback
        )



class Mts100SetPointNumber(MtsSetPointNumber):
    """
    customize MtsSetPointNumber to interact with Mts100 family valves
    """
    async def async_set_value(self, value: float) -> None:
        self.device.request(
            mc.NS_APPLIANCE_HUB_MTS100_TEMPERATURE,
            mc.METHOD_SET,
            {
                mc.KEY_TEMPERATURE: [
                    {
                        mc.KEY_ID: self.subdevice.id,
                        self._key: int(value * 10)
                    }
                ]
            },
        )