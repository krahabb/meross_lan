from typing import TYPE_CHECKING

from . import MtsThermostatClimate, mc, mn_t

if TYPE_CHECKING:
    from ...merossclient.protocol import types as mt


class Mts200Climate(MtsThermostatClimate):
    """Climate entity for MTS200 devices"""

    if TYPE_CHECKING:
        ns_value: mt.thermostat.Mode_C

    # MtsClimate class attributes
    temperature_scale = mc.MTS200_TEMP_SCALE
    SCHEDULE_NS = mn_t.Appliance_Control_Thermostat_Schedule
    MTS_MODE_TO_PRESET_MAP = {
        mc.MTS200_MODE_MANUAL: MtsThermostatClimate.Preset.CUSTOM,
        mc.MTS200_MODE_HEAT: MtsThermostatClimate.Preset.COMFORT,
        mc.MTS200_MODE_COOL: MtsThermostatClimate.Preset.SLEEP,
        mc.MTS200_MODE_ECO: MtsThermostatClimate.Preset.AWAY,
        mc.MTS200_MODE_AUTO: MtsThermostatClimate.Preset.AUTO,
    }
    MTS_MODE_TO_TEMPERATUREKEY_MAP = {
        k: MtsThermostatClimate.SimpleKeyValue(v)
        for k, v in mc.MTS200_MODE_TO_TARGETTEMP_MAP.items()
    }

    # interface: MtsClimate
    def flush_state(self, /):
        self.preset_mode = self.MTS_MODE_TO_PRESET_MAP.get(self._mts_mode)
        if self._mts_onoff:
            self.hvac_mode = self.hvac_modes[1]
            self.hvac_action = (
                (
                    MtsThermostatClimate.HVACAction.HEATING
                    if self.hvac_mode == MtsThermostatClimate.HVACMode.HEAT
                    else MtsThermostatClimate.HVACAction.COOLING
                )
                if self._mts_active
                else MtsThermostatClimate.HVACAction.IDLE
            )
        else:
            self.hvac_mode = MtsThermostatClimate.HVACMode.OFF
            self.hvac_action = MtsThermostatClimate.HVACAction.OFF
        MtsThermostatClimate.flush_state(self)

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

        target_temp = round(kwargs[self.ATTR_TEMPERATURE] * self.temperature_scale)
        self.ns_value[mc.KEY_TARGETTEMP] = target_temp  # optimistic update
        await self.async_request_parse_ex({mc.KEY_MODE: mode, key: target_temp})

    async def async_request_preset(self, mode: int, /):
        await self.async_request_parse_ex({mc.KEY_MODE: mode, mc.KEY_ONOFF: 1})

    async def async_request_onoff(self, onoff: int, /):
        await self.async_request_parse_ex({mc.KEY_ONOFF: onoff})

    def is_mts_scheduled(self, /):
        return self._mts_onoff and self._mts_mode == mc.MTS200_MODE_AUTO

    def __call__(self, payload: "mt.thermostat.Mode_C", /):
        if self.ns_value == payload:
            return
        self.ns_value = payload
        if mc.KEY_MODE in payload:
            self._mts_mode = payload[mc.KEY_MODE]
        if mc.KEY_ONOFF in payload:
            self._mts_onoff = payload[mc.KEY_ONOFF]
        if mc.KEY_STATE in payload:
            self._mts_active = payload[mc.KEY_STATE]
        if mc.KEY_CURRENTTEMP in payload:
            self._update_current_temperature(payload[mc.KEY_CURRENTTEMP])
        if mc.KEY_TARGETTEMP in payload:
            self.target_temperature = (
                payload[mc.KEY_TARGETTEMP] / self.temperature_scale
            )
        if mc.KEY_MIN in payload:
            self.min_temp = payload[mc.KEY_MIN] / self.temperature_scale
        if mc.KEY_MAX in payload:
            self.max_temp = payload[mc.KEY_MAX] / self.temperature_scale

        for _number in self.number_preset_temperature:
            try:
                _number.native_max_value = self.max_temp
                _number.native_min_value = self.min_temp
                _number(payload)
            except KeyError:
                pass
        self.flush_state()
