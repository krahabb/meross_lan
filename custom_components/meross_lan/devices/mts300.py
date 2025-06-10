from typing import TYPE_CHECKING, override

from homeassistant.components.climate import const as hacc

from ..calendar import MtsSchedule
from ..climate import MtsClimate
from ..helpers import reverse_lookup
from ..merossclient.protocol import const as mc, namespaces as mn
from ..merossclient.protocol.namespaces import thermostat as mn_t
from .thermostat import MtsCalibrationNumber

if TYPE_CHECKING:
    from typing import ClassVar, Final

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


class Mts300Climate(MtsClimate):
    """Climate entity for MTS300 devices"""

    """
    class FanModes(StrEnum):
        AUTO = hacc.FAN_AUTO
        LOW = hacc.FAN_LOW
        MEDIUM = hacc.FAN_MEDIUM
        HIGH = hacc.FAN_HIGH
    """

    if TYPE_CHECKING:
        # overrides
        manager: Final[Device]  # type: ignore
        channel: Final[int]  # type: ignore
        _mts_payload: thermostat.ModeC

        HVAC_MODE_TO_MODE_MAP: ClassVar
        _mts_work: int | None

        # HA core entity attributes:
        target_temperature_high: float | None
        target_temperature_low: float | None

    ns = mn_t.Appliance_Control_Thermostat_ModeC
    device_scale = mc.MTS300_TEMP_SCALE

    # MtsClimate class attributes
    MTS_MODE_TO_PRESET_MAP = {
        mc.MTS300_WORK_MANUAL: MtsClimate.Preset.CUSTOM,
        mc.MTS300_WORK_SCHEDULE: MtsClimate.Preset.AUTO,
    }
    MTS_MODE_TO_TEMPERATUREKEY_MAP = mc.MTS300_MODE_TO_TARGETTEMP_MAP

    # Mts300Climate class attributes
    HVAC_MODE_TO_MODE_MAP = {
        MtsClimate.HVACMode.OFF: mc.MTS300_MODE_OFF,
        MtsClimate.HVACMode.HEAT: mc.MTS300_MODE_HEAT,
        MtsClimate.HVACMode.COOL: mc.MTS300_MODE_COOL,
        MtsClimate.HVACMode.HEAT_COOL: mc.MTS300_MODE_AUTO,
    }
    FAN_MODE_TO_FAN_SPEED_MAP = {
        hacc.FAN_AUTO: mc.MTS300_FAN_SPEED_AUTO,
        hacc.FAN_LOW: mc.MTS300_FAN_SPEED_LOW,
        hacc.FAN_MEDIUM: mc.MTS300_FAN_SPEED_MEDIUM,
        hacc.FAN_HIGH: mc.MTS300_FAN_SPEED_HIGH,
    }
    STATUS_TO_HVAC_ACTION_MAP = {
        (False, False, False): MtsClimate.HVACAction.IDLE,
        # heating flag active (whatever the rest...)
        (True, False, False): MtsClimate.HVACAction.HEATING,
        (True, False, True): MtsClimate.HVACAction.HEATING,
        (True, True, False): MtsClimate.HVACAction.HEATING,
        (True, True, True): MtsClimate.HVACAction.HEATING,
        # cooling flag active (when not heating of course)
        (False, True, False): MtsClimate.HVACAction.COOLING,
        (False, True, True): MtsClimate.HVACAction.COOLING,
        # only fan active
        (False, False, True): MtsClimate.HVACAction.FAN,
    }
    """Status flags in "more" dict mapped as: (bool(hStatus), bool(cStatus), bool(fStatus))."""

    # HA core entity attributes:
    _attr_fan_modes = list(FAN_MODE_TO_FAN_SPEED_MAP)
    _attr_hvac_modes = list(HVAC_MODE_TO_MODE_MAP)
    _attr_preset_modes = list(MTS_MODE_TO_PRESET_MAP.values())
    _attr_supported_features = (
        MtsClimate.ClimateEntityFeature.PRESET_MODE
        | MtsClimate.ClimateEntityFeature.TARGET_TEMPERATURE
        | getattr(MtsClimate.ClimateEntityFeature, "TURN_OFF", 0)
        | getattr(MtsClimate.ClimateEntityFeature, "TURN_ON", 0)
        | MtsClimate.ClimateEntityFeature.TARGET_TEMPERATURE_RANGE
        | MtsClimate.ClimateEntityFeature.FAN_MODE
    )

    __slots__ = (
        "fan_mode",
        "fan_modes",
        "target_temperature_high",
        "target_temperature_low",
        "_mts_work",
    )

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
        self.fan_mode = None
        self.fan_modes = self._attr_fan_modes
        self.target_temperature_high = None
        self.target_temperature_low = None
        self._mts_work = None
        manager.register_parser_entity(self)
        manager.register_parser_entity(self.schedule)

    # interface: MtsClimate
    def set_unavailable(self):
        self.fan_mode = None
        self.target_temperature_high = None
        self.target_temperature_low = None
        self._mts_work = None
        return super().set_unavailable()

    @override
    async def async_set_hvac_mode(self, hvac_mode: MtsClimate.HVACMode):
        await self._async_request_modeC({"mode": self.HVAC_MODE_TO_MODE_MAP[hvac_mode]})

    @override
    async def async_set_temperature(self, **kwargs):
        try:
            temperature = kwargs[self.ATTR_TEMPERATURE]
            try:
                # check if maybe the service also sets hvac_mode
                mode = self.HVAC_MODE_TO_MODE_MAP[kwargs[self.ATTR_HVAC_MODE]]
            except KeyError:
                mode = self._mts_mode
            key = self.MTS_MODE_TO_TEMPERATUREKEY_MAP[mode]
            if key:
                # this is supposed to work when mts is in HEAT or COOL mode
                await self._async_request_modeC(
                    {
                        "mode": mode,
                        "work": mc.MTS300_WORK_MANUAL,
                        "targetTemp": {key: round(temperature * self.device_scale)},
                    }
                )
            else:
                raise ValueError(f"set_temperature unsupported in this mode ({mode})")

        except KeyError:
            # missing ATTR_TEMPERATURE in service call
            # it should be for RANGE mode
            await self._async_request_modeC(
                {
                    "mode": mc.MTS300_MODE_AUTO,
                    "work": mc.MTS300_WORK_MANUAL,
                    "targetTemp": {
                        "heat": round(
                            kwargs[self.ATTR_TARGET_TEMP_LOW] * self.device_scale
                        ),
                        "cold": round(
                            kwargs[self.ATTR_TARGET_TEMP_HIGH] * self.device_scale
                        ),
                    },
                }
            )

    @override
    async def async_set_fan_mode(self, fan_mode: str):
        fan_speed = self.FAN_MODE_TO_FAN_SPEED_MAP[fan_mode]
        # actually we assume: (fan_speed != 0) <-> (fMode == mc.MTS300_FAN_MODE_ON)
        await self._async_request_modeC(
            {
                "fan": {
                    "fMode": (
                        mc.MTS300_FAN_MODE_AUTO
                        if fan_speed is mc.MTS300_FAN_SPEED_AUTO
                        else mc.MTS300_FAN_MODE_ON
                    ),
                    "speed": fan_speed,
                }
            }
        )

    @override
    async def async_request_preset(self, mode: int):
        # in Mts300 we'll map 'presets' to the 'work' parameter
        await self._async_request_modeC({"work": mode})

    @override
    async def async_request_onoff(self, onoff: int):
        await self._async_request_modeC(
            {"mode": self._mts_mode if onoff else mc.MTS300_MODE_OFF}
        )

    @override
    def is_mts_scheduled(self):
        return self._mts_onoff and self._mts_work == mc.MTS300_WORK_SCHEDULE

    @override
    def get_ns_adjust(self):
        return self.manager.namespace_handlers[
            mn_t.Appliance_Control_Thermostat_Calibration.name
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
        """
        {
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
        }
        "sensorTemp" is the temperature of the built-in sensor of the device.
        "currentTemp" is the actual temperature used by the device for cooling and heating (the device supports the external sensor mode).
        "mode" is the current working mode; 0: off ; 1: heat ; 2: cool ; 3: auto. For example, if the device operates in auto mode and the target temperature is set to 2100-2400, then the device will not work when 21℃<currentTemp<24℃, heat up when the temperature is below 21℃, and cool down when the temperature is above 24℃.
        "cStatus" is the refrigeration working status; 0: Idle; Level 1 Colding; 2: Level 2 Colding;
        "humi" is the current humidity
        "cStatus" is the working status of heating. 0:Idle; 1: First-level Heating; 2: Grade 2 Heating; 3: Third-level Heating
        "fStatus" is the status of the fan; 0: Idle; 1: Low/ON; 2: Middle; 3: High
        "aStatus" is the auxiliary heating working state; 0:Idle; 1: First-level AUX 2: Secondary AUX; 3: Three-stage AUX
        "hdStatus" is the dehumidification/humidification working status, 0:Idle; 1: Dehumidification in progress; 2: Humidifying
        "work" is in the state where schedule is enabled; 1: manual  2: schedule
        "fMode" is the fan mode; 0: Auto; 1: ON (Hold);
        "speed" is wind speed; 0: Auto; 1: ON; 2: Middle; 3: High
        "hTime" is hold time, with the unit being minutes. 99999 indicates permanently
        """
        if self._mts_payload == payload:
            return
        self._mts_payload = payload
        try:
            self._mts_work = payload["work"]
            self.preset_mode = self.MTS_MODE_TO_PRESET_MAP.get(self._mts_work)
            self._update_current_temperature(payload["currentTemp"])
            targetTemp = payload["targetTemp"]
            self.target_temperature_high = targetTemp["cold"] / self.device_scale
            self.target_temperature_low = targetTemp["heat"] / self.device_scale
            more = payload["more"]
            self.current_humidity = more["humi"] / 10

            fan = payload["fan"]
            self.fan_mode = reverse_lookup(self.FAN_MODE_TO_FAN_SPEED_MAP, fan["speed"])
            match mode := payload["mode"]:
                case mc.MTS300_MODE_OFF:
                    self._mts_onoff = 0
                    # don't set _mts_mode so we remembere last one
                    self.hvac_mode = MtsClimate.HVACMode.OFF
                    self.hvac_action = MtsClimate.HVACAction.OFF
                    self.target_temperature = None
                case mc.MTS300_MODE_HEAT:
                    self._mts_onoff = 1
                    self._mts_mode = mode
                    self.hvac_mode = MtsClimate.HVACMode.HEAT
                    self.hvac_action = Mts300Climate.STATUS_TO_HVAC_ACTION_MAP[
                        (
                            bool(more["hStatus"]),
                            False,
                            bool(more["fStatus"]),
                        )
                    ]
                    """REMOVE
                    self.hvac_action = (
                        MtsClimate.HVACAction.HEATING
                        if more["hStatus"]
                        else (
                            MtsClimate.HVACAction.FAN
                            if more["fStatus"]
                            else MtsClimate.HVACAction.IDLE
                        )
                    )
                    """
                    self.target_temperature = self.target_temperature_low
                case mc.MTS300_MODE_COOL:
                    self._mts_onoff = 1
                    self._mts_mode = mode
                    self.hvac_mode = MtsClimate.HVACMode.COOL
                    self.hvac_action = Mts300Climate.STATUS_TO_HVAC_ACTION_MAP[
                        (
                            False,
                            bool(more["cStatus"]),
                            bool(more["fStatus"]),
                        )
                    ]
                    """REMOVE
                    self.hvac_action = (
                        MtsClimate.HVACAction.COOLING
                        if more["cStatus"]
                        else (
                            MtsClimate.HVACAction.FAN
                            if more["fStatus"]
                            else MtsClimate.HVACAction.IDLE
                        )
                    )
                    """
                    self.target_temperature = self.target_temperature_high
                case mc.MTS300_MODE_AUTO:
                    self._mts_onoff = 1
                    self._mts_mode = mode
                    self.hvac_mode = MtsClimate.HVACMode.HEAT_COOL
                    self.hvac_action = Mts300Climate.STATUS_TO_HVAC_ACTION_MAP[
                        (
                            bool(more["hStatus"]),
                            bool(more["cStatus"]),
                            bool(more["fStatus"]),
                        )
                    ]
                    """REMOVE
                    self.hvac_action = (
                        MtsClimate.HVACAction.COOLING
                        if more["cStatus"]
                        else (
                            MtsClimate.HVACAction.HEATING
                            if more["hStatus"]
                            else MtsClimate.HVACAction.IDLE
                        )
                    )
                    """
                    self.target_temperature = None

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
    ns = mn_t.Appliance_Control_Thermostat_ScheduleB

    # TODO: customize parsing of native payload since we have 2 temperatures
