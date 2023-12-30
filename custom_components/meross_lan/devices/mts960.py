from __future__ import annotations

import typing

from ..binary_sensor import MLBinarySensor
from ..calendar import MtsSchedule
from ..climate import MtsClimate
from ..helpers import reverse_lookup
from ..merossclient import const as mc
from ..number import MtsCalibrationNumber, MtsSetPointNumber

if typing.TYPE_CHECKING:
    from .thermostat import ThermostatMixin


class Mts960SetPointNumber(MtsSetPointNumber):
    """
    customize MtsSetPointNumber to interact with Mts960 family valves
    Actually it doesn't look like this feature exists
    """

    namespace = mc.NS_APPLIANCE_CONTROL_THERMOSTAT_MODEB
    key_namespace = mc.KEY_MODEB


class Mts960FakeSetPointNumber(MtsSetPointNumber):
    """
    faked placeholder to avoid instantiating MtsSetPointNumbers when
    not needed (mts960)
    """

    def __new__(cls, *args):
        return cls


class Mts960Climate(MtsClimate):
    """Climate entity for MTS200 devices"""

    MTS_MODE_TO_PRESET_MAP = {
        mc.MTS960_MODE_CUSTOM: MtsClimate.PRESET_CUSTOM,
        mc.MTS960_MODE_HEAT: MtsClimate.PRESET_COMFORT,
        mc.MTS960_MODE_COOL: MtsClimate.PRESET_SLEEP,
        mc.MTS960_MODE_ECO: MtsClimate.PRESET_AWAY,
        mc.MTS960_MODE_AUTO: MtsClimate.PRESET_AUTO,
    }
    # when setting target temp we'll set an appropriate payload key
    # for the mts depending on current 'preset' mode.
    # if mts is in any of 'off', 'auto' we just set the 'custom'
    # target temp but of course the valve will not follow
    # this temp since it's mode is not set to follow a manual set
    PRESET_TO_TEMPERATUREKEY_MAP = {
        MtsClimate.PRESET_CUSTOM: mc.KEY_TARGETTEMP,
        MtsClimate.PRESET_COMFORT: mc.KEY_TARGETTEMP,
        MtsClimate.PRESET_SLEEP: mc.KEY_TARGETTEMP,
        MtsClimate.PRESET_AWAY: mc.KEY_TARGETTEMP,
        MtsClimate.PRESET_AUTO: mc.KEY_TARGETTEMP,
    }

    manager: ThermostatMixin

    __slots__ = ()

    def __init__(self, manager: ThermostatMixin, channel: object):
        super().__init__(
            manager,
            channel,
            MLBinarySensor(
                manager, channel, mc.KEY_WINDOWOPENED, MLBinarySensor.DeviceClass.WINDOW
            ),
            MtsCalibrationNumber,
            Mts960FakeSetPointNumber,
            Mts960Schedule,
        )

    # interface: MtsClimate
    async def async_shutdown(self):
        await super().async_shutdown()

    def flush_state(self):
        self._attr_preset_mode = self.MTS_MODE_TO_PRESET_MAP.get(self._mts_mode)  # type: ignore
        if self._mts_onoff:
            self._attr_hvac_mode = MtsClimate.HVACMode.HEAT
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

        await self.async_request_onoff(1)

    async def async_set_preset_mode(self, preset_mode: str):
        mode = reverse_lookup(self.MTS_MODE_TO_PRESET_MAP, preset_mode)
        if (mode is not None) and await self.manager.async_request_ack(
            mc.NS_APPLIANCE_CONTROL_THERMOSTAT_MODEB,
            mc.METHOD_SET,
            {
                mc.KEY_MODEB: [
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
            mc.NS_APPLIANCE_CONTROL_THERMOSTAT_MODEB,
            mc.METHOD_SET,
            {
                mc.KEY_MODEB: [
                    {mc.KEY_CHANNEL: self.channel, key: round(t * self.device_scale)}
                ]
            },
        ):
            self._attr_target_temperature = t
            self.flush_state()

    async def async_request_onoff(self, onoff: int):
        if await self.manager.async_request_ack(
            mc.NS_APPLIANCE_CONTROL_THERMOSTAT_MODEB,
            mc.METHOD_SET,
            {mc.KEY_MODEB: [{mc.KEY_CHANNEL: self.channel, mc.KEY_ONOFF: onoff}]},
        ):
            self._mts_onoff = onoff
            self.flush_state()

    def is_mts_scheduled(self):
        return self._mts_onoff and self._mts_mode == mc.MTS960_MODE_AUTO

    @property
    def device_scale(self):
        """historically set at 10. Overriden in mts960 to 100"""
        return mc.MTS960_TEMP_SCALE

    # message handlers
    def _parse_modeB(self, payload: dict):
        """
        {
            "channel": 0,
            "mode": 2,
            "targetTemp": 2000,
            "working": 1,
            "currentTemp": 1915,
            "state": 1,
            "onoff": 1,
            "sensorStatus": 1
        }
        TODO:
        - decode "mode" (likely mapping mts modes like other mts)
        - interpret "working" - "sensorStatus"
        """
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
        self.flush_state()


class Mts960Schedule(MtsSchedule):
    namespace = mc.NS_APPLIANCE_CONTROL_THERMOSTAT_SCHEDULEB
    key_namespace = mc.KEY_SCHEDULEB
    key_channel = mc.KEY_CHANNEL

    def __init__(self, climate: Mts960Climate):
        super().__init__(climate)
