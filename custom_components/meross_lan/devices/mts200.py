from __future__ import annotations


from ..climate import (
    MtsClimate, MtsSetPointNumber,
    PRESET_OFF, PRESET_CUSTOM, PRESET_COMFORT, PRESET_SLEEP, PRESET_AWAY, PRESET_AUTO,
    ATTR_TEMPERATURE,
)
from ..binary_sensor import (
    MLBinarySensor,
    DEVICE_CLASS_WINDOW,
)

from ..merossclient import const as mc  # mEROSS cONST


class Mts200Climate(MtsClimate):

    MTS_MODE_AUTO = mc.MTS200_MODE_AUTO
    MTS_MODE_TO_PRESET_MAP = {
        mc.MTS200_MODE_CUSTOM: PRESET_CUSTOM,
        mc.MTS200_MODE_HEAT: PRESET_COMFORT,
        mc.MTS200_MODE_COOL: PRESET_SLEEP,
        mc.MTS200_MODE_ECO: PRESET_AWAY,
        mc.MTS200_MODE_AUTO: PRESET_AUTO
    }

    # reverse map
    PRESET_TO_MTS_MODE_MAP = {
        PRESET_CUSTOM: mc.MTS200_MODE_CUSTOM,
        PRESET_COMFORT: mc.MTS200_MODE_HEAT,
        PRESET_SLEEP: mc.MTS200_MODE_COOL,
        PRESET_AWAY: mc.MTS200_MODE_ECO,
        PRESET_AUTO: mc.MTS200_MODE_AUTO
    }

    # when setting target temp we'll set an appropriate payload key
    # for the mts100 depending on current 'preset' mode.
    # if mts100 is in any of 'off', 'auto' we just set the 'custom'
    # target temp but of course the valve will not follow
    # this temp since it's mode is not set to follow a manual set
    PRESET_TO_TEMPERATUREKEY_MAP = {
        PRESET_OFF: mc.KEY_MANUALTEMP,
        PRESET_CUSTOM: mc.KEY_MANUALTEMP,
        PRESET_COMFORT: mc.KEY_HEATTEMP,
        PRESET_SLEEP: mc.KEY_COOLTEMP,
        PRESET_AWAY: mc.KEY_ECOTEMP,
        PRESET_AUTO: mc.KEY_MANUALTEMP
    }


    def __init__(self, device: 'MerossDevice', channel: object):
        super().__init__(device, channel, None, None, None)
        self.binary_sensor_window = MLBinarySensor(
            device, channel, mc.KEY_WINDOWOPENED, DEVICE_CLASS_WINDOW)
        self.number_comfort_temperature = Mts200SetPointNumber(self, PRESET_COMFORT)
        self.number_sleep_temperature = Mts200SetPointNumber(self, PRESET_SLEEP)
        self.number_away_temperature = Mts200SetPointNumber(self, PRESET_AWAY)


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
                    mc.NS_APPLIANCE_CONTROL_THERMOSTAT_MODE,
                    mc.METHOD_SET,
                    {mc.KEY_MODE: [{mc.KEY_CHANNEL: self.channel, mc.KEY_MODE: mode}]},
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

        await self.device.async_http_request(
            mc.NS_APPLIANCE_CONTROL_THERMOSTAT_MODE,
            mc.METHOD_SET,
            {mc.KEY_MODE: [{mc.KEY_CHANNEL: self.channel, key: int(t * 10)}]}, # the device rounds down ?!
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
            mc.NS_APPLIANCE_CONTROL_THERMOSTAT_MODE,
            mc.METHOD_SET,
            {mc.KEY_MODE: [{mc.KEY_CHANNEL: self.channel, mc.KEY_ONOFF: onoff}]},
            _ack_callback
        )


    def _parse_mode(self, payload: dict):
        """{
                "channel": 0,
                "onoff": 1,
                "mode": 3,
                "state": 0,
                "currentTemp": 210,
                "heatTemp": 240,
                "coolTemp": 210,
                "ecoTemp": 120,
                "manualTemp": 230,
                "warning": 0,
                "targetTemp": 205,
                "min": 50,
                "max": 350,
                "lmTime": 1642425303
            }"""
        if mc.KEY_MODE in payload:
            self._mts_mode = payload[mc.KEY_MODE]
        if mc.KEY_ONOFF in payload:
            self._mts_onoff = payload[mc.KEY_ONOFF]
        if mc.KEY_STATE in payload:
            self._mts_heating = payload[mc.KEY_STATE]
        if isinstance(_t := payload.get(mc.KEY_CURRENTTEMP), int):
            self._attr_current_temperature = _t / 10
        if isinstance(_t := payload.get(mc.KEY_TARGETTEMP), int):
            self._attr_target_temperature = _t / 10
        if isinstance(_t := payload.get(mc.KEY_MIN), int):
            self._attr_min_temp = _t / 10
        if isinstance(_t := payload.get(mc.KEY_MAX), int):
            self._attr_max_temp = _t / 10
        if isinstance(_t := payload.get(mc.KEY_HEATTEMP), int):
            self.number_comfort_temperature.update_state(_t / 10)
        if isinstance(_t := payload.get(mc.KEY_COOLTEMP), int):
            self.number_sleep_temperature.update_state(_t / 10)
        if isinstance(_t := payload.get(mc.KEY_ECOTEMP), int):
            self.number_away_temperature.update_state(_t / 10)
        self.update_modes()


    def _parse_windowOpened(self, payload: dict):
        """{ "channel": 0, "status": 0, "lmTime": 1642425303 }"""
        self.binary_sensor_window.update_onoff(payload.get(mc.KEY_STATUS))



class Mts200SetPointNumber(MtsSetPointNumber):
    """
    customize MtsSetPointNumber to interact with Mts200 family valves
    """
    async def async_set_value(self, value: float) -> None:
        self.device.request(
            mc.NS_APPLIANCE_CONTROL_THERMOSTAT_MODE,
            mc.METHOD_SET,
            {mc.KEY_MODE: [{mc.KEY_CHANNEL: self.channel, self._key: int(value * 10)}]} # the device rounds down ?!
        )



class ThermostatMixin:


    def _init_thermostat(self, payload: dict):
        mode = payload.get(mc.KEY_MODE)
        if isinstance(mode, list):
            for m in mode:
                Mts200Climate(self, m[mc.KEY_CHANNEL])


    def _handle_Appliance_Control_Thermostat_Mode(self,
    namespace: str, method: str, payload: dict, header: dict):
        self._parse_thermostat_mode(payload.get(mc.KEY_MODE))


    def _handle_Appliance_Control_Thermostat_windowOpened(self,
    namespace: str, method: str, payload: dict, header: dict):
        self._parse_thermostat_windowOpened(payload.get(mc.KEY_WINDOWOPENED))


    def _parse_thermostat_mode(self, payload: dict):
        self._parse__generic(mc.KEY_MODE, payload)


    def _parse_thermostat_windowOpened(self, payload: dict):
        self._parse__generic(mc.KEY_WINDOWOPENED, payload)


    def _parse_thermostat(self, payload: dict):
        """
        "thermostat": {
            "mode": [{
                "channel": 0,
                "onoff": 1,
                "mode": 3,
                "state": 0,
                "currentTemp": 210,
                "heatTemp": 240,
                "coolTemp": 210,
                "ecoTemp": 120,
                "manualTemp": 230,
                "warning": 0,
                "targetTemp": 205,
                "min": 50,
                "max": 350,
                "lmTime": 1642425303
            }],
            "windowOpened": [{
                "channel": 0,
                "status": 0,
                "lmTime": 1642425303
            }]
        }
        """
        for key, value in payload.items():
            _parse = getattr(self, f"_parse_thermostat_{key}", None)
            if _parse is not None:
                _parse(value)

