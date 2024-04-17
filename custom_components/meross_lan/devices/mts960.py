from __future__ import annotations

import typing

from ..calendar import MtsSchedule
from ..climate import MtsClimate
from ..merossclient import const as mc
from ..number import MtsSetPointNumber
from ..sensor import MLDiagnosticSensor

if typing.TYPE_CHECKING:
    from .thermostat import ThermostatMixin


class Mts960FakeSetPointNumber(MtsSetPointNumber):
    """
    faked placeholder to avoid instantiating MtsSetPointNumbers when
    not needed (mts960)
    """

    def __new__(cls, *args):
        return cls


class Mts960Climate(MtsClimate):
    """Climate entity for MTS960 devices"""

    namespace = mc.NS_APPLIANCE_CONTROL_THERMOSTAT_MODEB
    key_namespace = mc.KEY_MODEB
    device_scale = mc.MTS960_TEMP_SCALE

    # default choice to map when any 'non thermostat' mode swapping
    # needs an heating/cooling final choice
    MTS_MODE_DEFAULT = mc.MTS960_MODE_HEAT

    MTS_MODE_TO_PRESET_MAP = {
        mc.MTS960_MODE_HEAT: "heat",
        mc.MTS960_MODE_COOL: "cool",
        mc.MTS960_MODE_CYCLE: "cycle_timer",
        mc.MTS960_MODE_COUNTDOWN_ON: "countdown_on",
        mc.MTS960_MODE_COUNTDOWN_OFF: "countdown_off",
        mc.MTS960_MODE_SCHEDULE_HEAT: "schedule_heat",
        mc.MTS960_MODE_SCHEDULE_COOL: "schedule_cool",
    }

    MTS_MODE_TO_HVAC_MODE: dict[int | None, MtsClimate.HVACMode] = {
        mc.MTS960_MODE_HEAT: MtsClimate.HVACMode.HEAT,
        mc.MTS960_MODE_COOL: MtsClimate.HVACMode.COOL,
        mc.MTS960_MODE_CYCLE: MtsClimate.HVACMode.AUTO,
        mc.MTS960_MODE_COUNTDOWN_ON: MtsClimate.HVACMode.AUTO,
        mc.MTS960_MODE_COUNTDOWN_OFF: MtsClimate.HVACMode.AUTO,
        mc.MTS960_MODE_SCHEDULE_HEAT: MtsClimate.HVACMode.AUTO,
        mc.MTS960_MODE_SCHEDULE_COOL: MtsClimate.HVACMode.AUTO,
    }

    HVAC_MODE_TO_MTS_MODE = {
        MtsClimate.HVACMode.HEAT: lambda mts_mode: mc.MTS960_MODE_HEAT,
        MtsClimate.HVACMode.COOL: lambda mts_mode: mc.MTS960_MODE_COOL,
        MtsClimate.HVACMode.AUTO: lambda mts_mode: (
            mc.MTS960_MODE_SCHEDULE_HEAT
            if mts_mode == mc.MTS960_MODE_HEAT
            else (
                mc.MTS960_MODE_SCHEDULE_COOL
                if mts_mode == mc.MTS960_MODE_COOL
                else Mts960Climate.MTS_MODE_DEFAULT if mts_mode is None else mts_mode
            )
        ),
    }

    MTS_MODE_TO_HVAC_ACTION: dict[int | None, MtsClimate.HVACAction] = {
        mc.MTS960_MODE_HEAT: MtsClimate.HVACAction.HEATING,
        mc.MTS960_MODE_COOL: MtsClimate.HVACAction.COOLING,
        mc.MTS960_MODE_CYCLE: MtsClimate.HVACAction.FAN,
        mc.MTS960_MODE_COUNTDOWN_ON: MtsClimate.HVACAction.FAN,
        mc.MTS960_MODE_COUNTDOWN_OFF: MtsClimate.HVACAction.FAN,
        mc.MTS960_MODE_SCHEDULE_HEAT: MtsClimate.HVACAction.HEATING,
        mc.MTS960_MODE_SCHEDULE_COOL: MtsClimate.HVACAction.COOLING,
    }

    # used to eventually bump out of any AUTO modes when manually setting
    # the setpoint temp
    MTS_MODE_TO_MTS_MODE_NOAUTO = {
        None: MTS_MODE_DEFAULT,
        mc.MTS960_MODE_HEAT: mc.MTS960_MODE_HEAT,
        mc.MTS960_MODE_COOL: mc.MTS960_MODE_COOL,
        mc.MTS960_MODE_CYCLE: MTS_MODE_DEFAULT,
        mc.MTS960_MODE_COUNTDOWN_ON: MTS_MODE_DEFAULT,
        mc.MTS960_MODE_COUNTDOWN_OFF: MTS_MODE_DEFAULT,
        mc.MTS960_MODE_SCHEDULE_HEAT: mc.MTS960_MODE_HEAT,
        mc.MTS960_MODE_SCHEDULE_COOL: mc.MTS960_MODE_COOL,
    }

    DIAGNOSTIC_SENSOR_KEYS = (
        mc.KEY_MODE,
        mc.KEY_ONOFF,
        mc.KEY_STATE,
        mc.KEY_SENSORSTATUS,
        mc.KEY_WORKING,
    )

    manager: ThermostatMixin

    # HA core entity attributes:
    hvac_modes = [
        MtsClimate.HVACMode.OFF,
        MtsClimate.HVACMode.HEAT,
        MtsClimate.HVACMode.COOL,
        MtsClimate.HVACMode.AUTO,
    ]
    preset_modes = [value for value in MTS_MODE_TO_PRESET_MAP.values()]

    __slots__ = ()

    def __init__(self, manager: ThermostatMixin, channel: object):
        super().__init__(
            manager,
            channel,
            manager.AdjustNumberClass,
            Mts960FakeSetPointNumber,
            Mts960Schedule,
        )

    # interface: MtsClimate
    def flush_state(self):
        if self._mts_onoff:
            self.hvac_mode = self.MTS_MODE_TO_HVAC_MODE.get(self._mts_mode)
            if self._mts_active:
                self.hvac_action = self.MTS_MODE_TO_HVAC_ACTION.get(self._mts_mode)
            else:
                self.hvac_action = MtsClimate.HVACAction.IDLE
        else:
            self.hvac_mode = MtsClimate.HVACMode.OFF
            self.hvac_action = MtsClimate.HVACAction.OFF
        super().flush_state()

    async def async_set_hvac_mode(self, hvac_mode: MtsClimate.HVACMode):
        if hvac_mode == MtsClimate.HVACMode.OFF:
            await self.async_request_onoff(0)
            return
        # here special handling is applied to hvac_mode == AUTO,
        # trying to preserve the previous mts_mode if it was already
        # among the AUTO(s) else mapping to a 'closest' one (see the lambdas
        # in HVAC_MODE_TO_MTS_MODE)
        mode = self.HVAC_MODE_TO_MTS_MODE[hvac_mode](self._mts_mode)
        await self.async_request_mode(mode)

    async def async_set_temperature(self, **kwargs):
        mode = self.MTS_MODE_TO_MTS_MODE_NOAUTO.get(
            self._mts_mode, self.MTS_MODE_DEFAULT
        )
        if response := await self.manager.async_request_ack(
            mc.NS_APPLIANCE_CONTROL_THERMOSTAT_MODEB,
            mc.METHOD_SET,
            {
                mc.KEY_MODEB: [
                    {
                        mc.KEY_CHANNEL: self.channel,
                        mc.KEY_MODE: mode,
                        mc.KEY_ONOFF: 1,
                        mc.KEY_TARGETTEMP: round(
                            kwargs[self.ATTR_TEMPERATURE] * self.device_scale
                        ),
                    }
                ]
            },
        ):
            try:
                payload = response[mc.KEY_PAYLOAD][mc.KEY_MODEB]
                self._parse(payload[0] if isinstance(payload, list) else payload)
            except KeyError:
                # optimistic update
                self.target_temperature = kwargs[self.ATTR_TEMPERATURE]
                self._mts_mode = mode
                self._mts_onoff = 1
                self.flush_state()

    async def async_request_mode(self, mode: int):
        if await self.manager.async_request_ack(
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

    async def async_request_onoff(self, onoff: int):
        if await self.manager.async_request_ack(
            mc.NS_APPLIANCE_CONTROL_THERMOSTAT_MODEB,
            mc.METHOD_SET,
            {mc.KEY_MODEB: [{mc.KEY_CHANNEL: self.channel, mc.KEY_ONOFF: onoff}]},
        ):
            self._mts_onoff = onoff
            self.flush_state()

    def is_mts_scheduled(self):
        return self._mts_onoff and (
            self._mts_mode
            in (mc.MTS960_MODE_SCHEDULE_HEAT, mc.MTS960_MODE_SCHEDULE_COOL)
        )

    # message handlers
    def _parse(self, payload: dict):
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
            self._mts_active = payload[mc.KEY_STATE] == mc.MTS960_STATE_ON
        if mc.KEY_CURRENTTEMP in payload:
            self.current_temperature = payload[mc.KEY_CURRENTTEMP] / self.device_scale
            self.select_tracked_sensor.check_tracking()
        if mc.KEY_TARGETTEMP in payload:
            self.target_temperature = payload[mc.KEY_TARGETTEMP] / self.device_scale

        manager = self.manager
        if manager.create_diagnostic_entities:
            entities = manager.entities
            channel = self.channel
            for key in self.DIAGNOSTIC_SENSOR_KEYS:
                if key in payload:
                    try:
                        entities[f"{channel}_{key}"].update_native_value(payload[key])
                    except KeyError:
                        MLDiagnosticSensor(
                            manager,
                            channel,
                            key,
                            native_value=payload[key],
                        )

        self.flush_state()


class Mts960Schedule(MtsSchedule):
    namespace = mc.NS_APPLIANCE_CONTROL_THERMOSTAT_SCHEDULEB
    key_namespace = mc.KEY_SCHEDULEB
    key_channel = mc.KEY_CHANNEL

    def __init__(self, climate: Mts960Climate):
        super().__init__(climate)
