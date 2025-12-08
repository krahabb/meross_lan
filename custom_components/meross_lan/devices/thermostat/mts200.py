from typing import TYPE_CHECKING

from ...calendar import MtsSchedule
from ...climate import MtsSetPointNumber
from ...merossclient import merge_dicts
from .mtsthermostat import MtsThermostatClimate, mc, mn_t

if TYPE_CHECKING:
    from ...helpers.device import Device


class Mts200Climate(MtsThermostatClimate):
    """Climate entity for MTS200 devices"""

    ns = mn_t.Appliance_Control_Thermostat_Mode

    # MtsClimate class attributes
    device_scale = mc.MTS200_TEMP_SCALE

    class SetPointNumber(MtsSetPointNumber):
        ns = mn_t.Appliance_Control_Thermostat_Mode

    class Schedule(MtsSchedule):
        ns = mn_t.Appliance_Control_Thermostat_Schedule

    MTS_MODE_TO_PRESET_MAP = {
        mc.MTS200_MODE_MANUAL: MtsThermostatClimate.Preset.CUSTOM,
        mc.MTS200_MODE_HEAT: MtsThermostatClimate.Preset.COMFORT,
        mc.MTS200_MODE_COOL: MtsThermostatClimate.Preset.SLEEP,
        mc.MTS200_MODE_ECO: MtsThermostatClimate.Preset.AWAY,
        mc.MTS200_MODE_AUTO: MtsThermostatClimate.Preset.AUTO,
    }
    MTS_MODE_TO_TEMPERATUREKEY_MAP = mc.MTS200_MODE_TO_TARGETTEMP_MAP

    # right now we're only sure summermode == '1' is 'HEAT'
    MTS_SUMMERMODE_TO_HVAC_MODE = {
        None: MtsThermostatClimate.HVACMode.HEAT,  # mapping when no summerMode avail
        mc.MTS200_SUMMERMODE_COOL: MtsThermostatClimate.HVACMode.COOL,
        mc.MTS200_SUMMERMODE_HEAT: MtsThermostatClimate.HVACMode.HEAT,
    }
    HVAC_MODE_TO_MTS_SUMMERMODE = {
        MtsThermostatClimate.HVACMode.HEAT: mc.MTS200_SUMMERMODE_HEAT,
        MtsThermostatClimate.HVACMode.COOL: mc.MTS200_SUMMERMODE_COOL,
    }
    MTS_SUMMERMODE_TO_HVAC_ACTION: dict[int | None, MtsThermostatClimate.HVACAction] = {
        None: MtsThermostatClimate.HVACAction.HEATING,  # mapping when no summerMode avail
        mc.MTS200_SUMMERMODE_COOL: MtsThermostatClimate.HVACAction.COOLING,
        mc.MTS200_SUMMERMODE_HEAT: MtsThermostatClimate.HVACAction.HEATING,
    }

    __slots__ = (
        "_mts_summermode",
        "_mts_summermode_supported",
    )

    def __init__(self, manager: "Device", channel: object, /):
        super().__init__(manager, channel)
        self._mts_summermode = None
        self._mts_summermode_supported = (
            mn_t.Appliance_Control_Thermostat_SummerMode.name
            in manager.descriptor.ability
        )
        if self._mts_summermode_supported:
            self.hvac_modes = [
                MtsThermostatClimate.HVACMode.OFF,
                MtsThermostatClimate.HVACMode.HEAT,
                MtsThermostatClimate.HVACMode.COOL,
            ]

    # interface: MtsClimate
    def flush_state(self, /):
        self.preset_mode = self.MTS_MODE_TO_PRESET_MAP.get(self._mts_mode)
        if self._mts_onoff:
            self.hvac_mode = self.MTS_SUMMERMODE_TO_HVAC_MODE.get(self._mts_summermode)
            self.hvac_action = (
                self.MTS_SUMMERMODE_TO_HVAC_ACTION.get(self._mts_summermode)
                if self._mts_active
                else MtsThermostatClimate.HVACAction.IDLE
            )
        else:
            self.hvac_mode = MtsThermostatClimate.HVACMode.OFF
            self.hvac_action = MtsThermostatClimate.HVACAction.OFF

        super().flush_state()

    async def async_set_hvac_mode(self, hvac_mode: MtsThermostatClimate.HVACMode, /):
        if hvac_mode == MtsThermostatClimate.HVACMode.OFF:
            await self.async_request_onoff(0)
            return

        if self._mts_summermode_supported:
            # this is an indicator the device supports it
            summermode = self.HVAC_MODE_TO_MTS_SUMMERMODE[hvac_mode]
            if self._mts_summermode != summermode:
                await self.async_request_summermode(summermode)

        await self.async_request_onoff(1)

    async def async_set_temperature(self, /, **kwargs):
        mode = self._mts_mode
        if self.SET_TEMP_FORCE_MANUAL_MODE or (mode == mc.MTS200_MODE_AUTO):
            # ensure we're not in schedule mode or any other preset (#401)
            key = mc.KEY_MANUALTEMP
            mode = mc.MTS200_MODE_MANUAL
        else:
            key = mc.MTS200_MODE_TO_TARGETTEMP_MAP.get(mode) or mc.KEY_MANUALTEMP
            if key is mc.KEY_MANUALTEMP:
                mode = mc.MTS200_MODE_MANUAL
        await self._async_request_mode(
            {
                mc.KEY_CHANNEL: self.channel,
                mc.KEY_MODE: mode,
                key: round(kwargs[self.ATTR_TEMPERATURE] * self.device_scale),
            }
        )

    async def async_request_preset(self, mode: int, /):
        await self._async_request_mode(
            {
                mc.KEY_CHANNEL: self.channel,
                mc.KEY_MODE: mode,
                mc.KEY_ONOFF: 1,
            }
        )

    async def async_request_onoff(self, onoff: int, /):
        await self._async_request_mode(
            {mc.KEY_CHANNEL: self.channel, mc.KEY_ONOFF: onoff}
        )

    def is_mts_scheduled(self, /):
        return self._mts_onoff and self._mts_mode == mc.MTS200_MODE_AUTO

    # interface: self
    async def async_request_summermode(self, summermode: int, /):
        ns = mn_t.Appliance_Control_Thermostat_SummerMode
        if await self.manager.async_request_ack(
            ns.name,
            mc.METHOD_SET,
            {ns.key: [{ns.key_channel: self.channel, mc.KEY_MODE: summermode}]},
        ):
            # it looks that (at least when sending '0') even
            # if acknowledged the mts doesnt really update it
            self._mts_summermode = summermode
            self.flush_state()

    async def _async_request_mode(self, p_mode: dict, /):
        if response := await self.manager.async_request_ack(
            self.ns.name,
            mc.METHOD_SET,
            {self.ns.key: [p_mode]},
        ):
            try:
                payload = response[mc.KEY_PAYLOAD][mc.KEY_MODE][0]
            except (KeyError, IndexError):
                # optimistic update
                payload = merge_dicts(self._mts_payload, p_mode)
                if mc.KEY_MODE in p_mode:
                    key_temp = mc.MTS200_MODE_TO_TARGETTEMP_MAP.get(p_mode[mc.KEY_MODE])
                    if key_temp in payload:
                        payload[mc.KEY_TARGETTEMP] = payload[key_temp]
            self._parse_mode(payload)

    # message handlers
    def _parse_mode(self, payload: dict, /):
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
        if self._mts_payload == payload:
            return
        self._mts_payload = payload
        if mc.KEY_MODE in payload:
            self._mts_mode = payload[mc.KEY_MODE]
        if mc.KEY_ONOFF in payload:
            self._mts_onoff = payload[mc.KEY_ONOFF]
        if mc.KEY_STATE in payload:
            self._mts_active = payload[mc.KEY_STATE]
        if mc.KEY_CURRENTTEMP in payload:
            self._update_current_temperature(payload[mc.KEY_CURRENTTEMP])
        if mc.KEY_TARGETTEMP in payload:
            self.target_temperature = payload[mc.KEY_TARGETTEMP] / self.device_scale
        if mc.KEY_MIN in payload:
            self.min_temp = payload[mc.KEY_MIN] / self.device_scale
        if mc.KEY_MAX in payload:
            self.max_temp = payload[mc.KEY_MAX] / self.device_scale

        for (
            key_temp,
            number_preset_temperature,
        ) in self.number_preset_temperature.items():
            if key_temp in payload:
                number_preset_temperature.update_device_value(payload[key_temp])

        self.flush_state()

    def _parse_summerMode(self, payload: dict, /):
        """{ "channel": 0, "mode": 0 }"""
        summermode = payload[mc.KEY_MODE]
        if self._mts_summermode != summermode:
            self._mts_summermode = summermode
            self.flush_state()
