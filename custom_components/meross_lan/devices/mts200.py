from __future__ import annotations

import typing

from ..binary_sensor import MLBinarySensor
from ..calendar import MtsSchedule
from ..climate import MtsClimate
from ..helpers import reverse_lookup
from ..merossclient import const as mc
from ..number import MtsCalibrationNumber, MtsSetPointNumber
from ..switch import MtsConfigSwitch

if typing.TYPE_CHECKING:
    from .thermostat import ThermostatMixin


class Mts200SetPointNumber(MtsSetPointNumber):
    """
    customize MtsSetPointNumber to interact with Mts200 family valves
    """

    namespace = mc.NS_APPLIANCE_CONTROL_THERMOSTAT_MODE
    key_namespace = mc.KEY_MODE


class Mts200Climate(MtsClimate):
    """Climate entity for MTS200 devices"""

    MTS_MODE_TO_PRESET_MAP = {
        mc.MTS200_MODE_CUSTOM: MtsClimate.PRESET_CUSTOM,
        mc.MTS200_MODE_HEAT: MtsClimate.PRESET_COMFORT,
        mc.MTS200_MODE_COOL: MtsClimate.PRESET_SLEEP,
        mc.MTS200_MODE_ECO: MtsClimate.PRESET_AWAY,
        mc.MTS200_MODE_AUTO: MtsClimate.PRESET_AUTO,
    }
    # right now we're only sure summermode == '1' is 'HEAT'
    SUMMERMODE_TO_HVACMODE = {
        None: MtsClimate.HVACMode.HEAT,  # mapping when no summerMode avail
        mc.MTS200_SUMMERMODE_COOL: MtsClimate.HVACMode.COOL,
        mc.MTS200_SUMMERMODE_HEAT: MtsClimate.HVACMode.HEAT,
    }
    HVACMODE_TO_SUMMERMODE = {
        MtsClimate.HVACMode.HEAT: mc.MTS200_SUMMERMODE_HEAT,
        MtsClimate.HVACMode.COOL: mc.MTS200_SUMMERMODE_COOL,
    }
    # when setting target temp we'll set an appropriate payload key
    # for the mts100 depending on current 'preset' mode.
    # if mts100 is in any of 'off', 'auto' we just set the 'custom'
    # target temp but of course the valve will not follow
    # this temp since it's mode is not set to follow a manual set
    PRESET_TO_TEMPERATUREKEY_MAP = {
        MtsClimate.PRESET_CUSTOM: mc.KEY_MANUALTEMP,
        MtsClimate.PRESET_COMFORT: mc.KEY_HEATTEMP,
        MtsClimate.PRESET_SLEEP: mc.KEY_COOLTEMP,
        MtsClimate.PRESET_AWAY: mc.KEY_ECOTEMP,
        MtsClimate.PRESET_AUTO: mc.KEY_MANUALTEMP,
    }

    manager: ThermostatMixin

    __slots__ = (
        "_mts_summermode",
        "switch_sensor_mode",
    )

    def __init__(self, manager: ThermostatMixin, channel: object):
        super().__init__(
            manager,
            channel,
            MLBinarySensor(
                manager, channel, mc.KEY_WINDOWOPENED, MLBinarySensor.DeviceClass.WINDOW
            ),
            MtsCalibrationNumber,
            Mts200SetPointNumber,
            Mts200Schedule,
        )
        self._mts_summermode = None
        # sensor mode: use internal(0) vs external(1) sensor as temperature loopback
        self.switch_sensor_mode = MtsConfigSwitch(
            self, "external sensor mode", mc.NS_APPLIANCE_CONTROL_THERMOSTAT_SENSOR
        )
        self.switch_sensor_mode.key_onoff = mc.KEY_MODE
        if mc.NS_APPLIANCE_CONTROL_THERMOSTAT_SUMMERMODE in manager.descriptor.ability:
            self._attr_hvac_modes = [
                MtsClimate.HVACMode.OFF,
                MtsClimate.HVACMode.HEAT,
                MtsClimate.HVACMode.COOL,
            ]

    # interface: MtsClimate
    async def async_shutdown(self):
        self.switch_sensor_mode: MtsConfigSwitch = None  # type: ignore
        await super().async_shutdown()

    def flush_state(self):
        self._attr_preset_mode = self.MTS_MODE_TO_PRESET_MAP.get(self._mts_mode)  # type: ignore
        if self._mts_onoff:
            self._attr_hvac_mode = self.SUMMERMODE_TO_HVACMODE.get(
                self._mts_summermode, MtsClimate.HVACMode.HEAT
            )
            if self._mts_active:
                self._attr_hvac_action = (
                    MtsClimate.HVACAction.HEATING
                    if self._attr_hvac_mode is MtsClimate.HVACMode.HEAT
                    else MtsClimate.HVACAction.COOLING
                )
            else:
                self._attr_hvac_action = MtsClimate.HVACAction.IDLE
        else:
            self._attr_hvac_mode = MtsClimate.HVACMode.OFF
            self._attr_hvac_action = MtsClimate.HVACAction.OFF

        super().flush_state()

    async def async_set_hvac_mode(self, hvac_mode: MtsClimate.HVACMode):
        if hvac_mode == MtsClimate.HVACMode.OFF:
            await self.async_request_onoff(0)
            return

        if not (self._mts_summermode is None):
            # this is an indicator the device supports it
            summermode = self.HVACMODE_TO_SUMMERMODE[hvac_mode]
            if self._mts_summermode != summermode:
                await self.async_request_summermode(summermode)

        await self.async_request_onoff(1)

    async def async_set_preset_mode(self, preset_mode: str):
        mode = reverse_lookup(self.MTS_MODE_TO_PRESET_MAP, preset_mode)
        if (mode is not None) and await self.manager.async_request_ack(
            mc.NS_APPLIANCE_CONTROL_THERMOSTAT_MODE,
            mc.METHOD_SET,
            {
                mc.KEY_MODE: [
                    {
                        mc.KEY_CHANNEL: self.channel,
                        mc.KEY_MODE: mode,
                        mc.KEY_ONOFF: 1,
                    }
                ]
            },
        ):
            self._mts_mode = mode
            self._mts_onoff = 1
            self.flush_state()

    async def async_set_temperature(self, **kwargs):
        t = kwargs[self.ATTR_TEMPERATURE]
        key = self.PRESET_TO_TEMPERATUREKEY_MAP[
            self._attr_preset_mode or self.PRESET_CUSTOM
        ]
        if await self.manager.async_request_ack(
            mc.NS_APPLIANCE_CONTROL_THERMOSTAT_MODE,
            mc.METHOD_SET,
            {
                mc.KEY_MODE: [
                    {mc.KEY_CHANNEL: self.channel, key: round(t * self.device_scale)}
                ]
            },
        ):
            self._attr_target_temperature = t
            self.flush_state()

    async def async_request_onoff(self, onoff: int):
        if await self.manager.async_request_ack(
            mc.NS_APPLIANCE_CONTROL_THERMOSTAT_MODE,
            mc.METHOD_SET,
            {mc.KEY_MODE: [{mc.KEY_CHANNEL: self.channel, mc.KEY_ONOFF: onoff}]},
        ):
            self._mts_onoff = onoff
            self.flush_state()

    async def async_request_summermode(self, summermode: int):
        if await self.manager.async_request_ack(
            mc.NS_APPLIANCE_CONTROL_THERMOSTAT_SUMMERMODE,
            mc.METHOD_SET,
            {
                mc.KEY_SUMMERMODE: [
                    {mc.KEY_CHANNEL: self.channel, mc.KEY_MODE: summermode}
                ]
            },
        ):
            # it looks that (at least when sending '0') even
            # if acknowledged the mts doesnt really update it
            self._mts_summermode = summermode
            self.flush_state()

    def is_mts_scheduled(self):
        return self._mts_onoff and self._mts_mode == mc.MTS200_MODE_AUTO

    @property
    def namespace(self):
        return mc.NS_APPLIANCE_CONTROL_THERMOSTAT_MODE

    @property
    def key_namespace(self):
        mc.KEY_MODE

    # message handlers
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
            self._mts_active = payload[mc.KEY_STATE]
        if mc.KEY_CURRENTTEMP in payload:
            self._attr_current_temperature = (
                payload[mc.KEY_CURRENTTEMP] / self.device_scale
            )
            self.select_tracked_sensor.check_tracking()
        if mc.KEY_TARGETTEMP in payload:
            self._attr_target_temperature = (
                payload[mc.KEY_TARGETTEMP] / self.device_scale
            )
        if mc.KEY_MIN in payload:
            self._attr_min_temp = payload[mc.KEY_MIN] / self.device_scale
        if mc.KEY_MAX in payload:
            self._attr_max_temp = payload[mc.KEY_MAX] / self.device_scale
        if mc.KEY_HEATTEMP in payload:
            self.number_comfort_temperature.update_native_value(
                payload[mc.KEY_HEATTEMP]
            )
        if mc.KEY_COOLTEMP in payload:
            self.number_sleep_temperature.update_native_value(payload[mc.KEY_COOLTEMP])
        if mc.KEY_ECOTEMP in payload:
            self.number_away_temperature.update_native_value(payload[mc.KEY_ECOTEMP])
        self.flush_state()

    def _parse_sensor(self, payload: dict):
        """{ "channel": 0, "mode": 0 }"""
        self.switch_sensor_mode.update_onoff(payload[mc.KEY_MODE])

    def _parse_summerMode(self, payload: dict):
        """{ "channel": 0, "mode": 0 }"""
        if mc.KEY_MODE in payload:
            summermode = payload[mc.KEY_MODE]
            if self._mts_summermode != summermode:
                self._mts_summermode = summermode
                self.flush_state()

    def _parse_windowOpened(self, payload: dict):
        """{ "channel": 0, "status": 0, "lmTime": 1642425303 }"""
        self.binary_sensor_window.update_onoff(payload[mc.KEY_STATUS])


class Mts200Schedule(MtsSchedule):
    namespace = mc.NS_APPLIANCE_CONTROL_THERMOSTAT_SCHEDULE
    key_namespace = mc.KEY_SCHEDULE
    key_channel = mc.KEY_CHANNEL

    def __init__(self, climate: Mts200Climate):
        super().__init__(climate)
