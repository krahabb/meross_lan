from typing import TYPE_CHECKING, override

from ..calendar import MtsSchedule
from ..climate import MtsClimate
from ..merossclient.protocol import const as mc, namespaces as mn
from ..number import MtsSetPointNumber
from .thermostat import MtsCalibrationNumber

if TYPE_CHECKING:
    from typing import Final

    from ..helpers.device import Device
    from ..merossclient.protocol.types import thermostat
    from ..number import MtsTemperatureNumber

    """
    "Appliance.System.Ability",
    {
        "Appliance.Config.DeviceCfg": {},
        "Appliance.Config.Sensor.Association": {},
        "Appliance.Control.AlertConfig": {},
        "Appliance.Control.AlertReport": {},
        "Appliance.Control.FilterMaintenance": {},
        "Appliance.Control.PhysicalLock": {},
        "Appliance.Control.Screen.Brightness": {},
        "Appliance.Control.Sensor.Association": {},
        "Appliance.Control.Sensor.HistoryX": {},
        "Appliance.Control.TempUnit": {},
        "Appliance.Control.Thermostat.Calibration": {},
        "Appliance.Control.Thermostat.HoldAction": {},
        "Appliance.Control.Thermostat.ModeC": {},
        "Appliance.Control.Thermostat.ScheduleB": {},
        "Appliance.Control.Thermostat.System": {},
    }
    """


class Mts300SetPointNumber(MtsSetPointNumber):
    """
    customize MtsSetPointNumber to interact with Mts300
    """

    ns = mn.Appliance_Control_Thermostat_ModeC


class Mts300Climate(MtsClimate):
    """Climate entity for MTS300 devices"""

    if TYPE_CHECKING:
        # overrides
        manager: Final["Device"]  # type: ignore
        channel: Final[int]  # type: ignore
        _mts_payload: thermostat.ModeC

    ns = mn.Appliance_Control_Thermostat_ModeC
    device_scale = mc.MTS300_TEMP_SCALE

    MTS_MODE_TO_PRESET_MAP = {
        mc.MTS300_MODE_AUTO: MtsClimate.PRESET_AUTO,
    }

    hvac_modes = [
        MtsClimate.HVACMode.OFF,
        MtsClimate.HVACMode.HEAT,
        MtsClimate.HVACMode.COOL,
        MtsClimate.HVACMode.AUTO,
        # MtsClimate.HVACMode.FAN_ONLY,
    ]

    __slots__ = ("_mts_work",)

    def __init__(
        self,
        manager: "Device",
    ):
        super().__init__(
            manager,
            0,
            MtsCalibrationNumber,
            None,
            Mts300Schedule,
        )
        self._mts_work = None
        manager.register_parser_entity(self)
        manager.register_parser_entity(self.schedule)

    # interface: MtsClimate
    def set_unavailable(self):
        self._mts_work = None
        return super().set_unavailable()

    @override
    async def async_set_hvac_mode(self, hvac_mode: MtsClimate.HVACMode):
        match hvac_mode:
            case MtsClimate.HVACMode.OFF:
                await self._async_request_modeC({"work": mc.MTS300_WORK_OFF})
                return
            case MtsClimate.HVACMode.COOL:
                await self._async_request_modeC({"work": mc.MTS300_WORK_COOL})
                return
            case MtsClimate.HVACMode.HEAT:
                await self._async_request_modeC({"work": mc.MTS300_WORK_HEAT})
                return
            case MtsClimate.HVACMode.AUTO:
                await self._async_request_modeC(
                    {"work": self._mts_work, "mode": mc.MTS300_MODE_AUTO}
                )
                return
        raise ValueError(hvac_mode)

    @override
    async def async_set_temperature(self, **kwargs):
        match self._mts_work:
            case mc.MTS300_WORK_COOL:
                await self._async_request_modeC(
                    {
                        "targetTemp": {
                            "cold": round(
                                kwargs[self.ATTR_TEMPERATURE] * self.device_scale
                            )
                        }
                    }
                )
            case mc.MTS300_WORK_HEAT:
                await self._async_request_modeC(
                    {
                        "targetTemp": {
                            "heat": round(
                                kwargs[self.ATTR_TEMPERATURE] * self.device_scale
                            )
                        }
                    }
                )

    @override
    async def async_request_mode(self, mode: int):
        await self._async_request_modeC({"mode": mode})

    @override
    async def async_request_onoff(self, onoff: int):
        # TODO: remomber last 'work'
        await self._async_request_modeC(
            {"work": self._mts_work if onoff else mc.MTS300_WORK_OFF}
        )

    @override
    def is_mts_scheduled(self):
        return self._mts_onoff and self._mts_mode == mc.MTS300_MODE_AUTO

    @override
    def get_ns_adjust(self):
        return self.manager.namespace_handlers[
            mn.Appliance_Control_Thermostat_Calibration.name
        ]

    # interface: self
    async def _async_request_modeC(self, payload: dict):
        ns = self.ns
        payload |= {"channel": self.channel}
        if response := await self.manager.async_request_ack(
            ns.name,
            mc.METHOD_SET,
            {ns.key: [payload]},
        ):
            try:
                payload = response[mc.KEY_PAYLOAD][ns.key][0]
            except (KeyError, IndexError):
                # optimistic update
                payload = self._mts_payload | payload
                pass
            self._parse_modeC(payload)  # type: ignore

    # message handlers
    def _parse_modeC(self, payload: "thermostat.ModeC"):
        """{
            "fan": {
                "fMode": 0,
                "speed": 0,
                "hTime": 99999
            },
            "sensorTemp": 2200,
            "currentTemp": 2200,
            "more": {
                "hdStatus": 0,
                "humi": 495,
                "cStatus": 0,
                "hStatus": 0,
                "fStatus": 0,
                "aStatus": 0
            },
            "channel": 0,
            "mode": 3,
            "work": 2,
            "targetTemp": {
                "heat": 2100,
                "cold": 2400
            }
        }"""
        if self._mts_payload == payload:
            return
        self._mts_payload = payload
        try:
            self._mts_mode = payload["mode"]
            self._update_current_temperature(payload["currentTemp"])
            more = payload["more"]
            self.current_humidity = more["humi"] / 10

            # flush_state
            self.preset_mode = self.MTS_MODE_TO_PRESET_MAP.get(self._mts_mode)
            match payload["work"]:
                case mc.MTS300_WORK_OFF:
                    self._mts_onoff = 0
                    self.hvac_mode = MtsClimate.HVACMode.OFF
                    self.hvac_action = MtsClimate.HVACAction.OFF
                case mc.MTS300_WORK_HEAT:
                    self._mts_onoff = 1
                    self._mts_work = mc.MTS300_WORK_HEAT
                    self.hvac_mode = MtsClimate.HVACMode.HEAT
                    self.hvac_action = (
                        MtsClimate.HVACAction.HEATING
                        if more["hStatus"]
                        else MtsClimate.HVACAction.IDLE
                    )
                    self.target_temperature = (
                        payload["targetTemp"]["heat"] / self.device_scale
                    )
                case mc.MTS300_WORK_COOL:
                    self._mts_onoff = 1
                    self._mts_work = mc.MTS300_WORK_COOL
                    self.hvac_mode = MtsClimate.HVACMode.COOL
                    self.hvac_action = (
                        MtsClimate.HVACAction.COOLING
                        if more["cStatus"]
                        else MtsClimate.HVACAction.IDLE
                    )
                    self.target_temperature = (
                        payload["targetTemp"]["cold"] / self.device_scale
                    )

            self.flush_state()
        except Exception as e:
            self.log_exception(self.WARNING, e, "parsing thermostat ModeC", timeout=300)

    def _parse_holdAction(self, payload: dict):
        """{"channel": 0, "mode": 0, "expire": 1697010767}"""
        # TODO: it looks like this message is related to #369.
        # The trace shows the log about the missing handler in 4.5.2
        # and it looks like when we receive this, it is a notification
        # the mts is not really changing its setpoint (as per the issue).
        # We need more info about how to process this.


class Mts300Schedule(MtsSchedule):
    ns = mn.Appliance_Control_Thermostat_ScheduleB
