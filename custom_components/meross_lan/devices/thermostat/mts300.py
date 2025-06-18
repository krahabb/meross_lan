import datetime as dt
from typing import TYPE_CHECKING, override

from homeassistant.components.climate import const as hacc

from ...calendar import MtsSchedule
from ...helpers import reverse_lookup
from ...number import MLConfigNumber
from ...sensor import MLEnumSensor
from ...switch import MLEmulatedSwitch
from .mtsthermostat import (
    MtsCommonTemperatureNumber,
    MtsThermostatClimate,
    mc,
    mn_t,
)

if TYPE_CHECKING:
    from typing import ClassVar, Final

    from ...helpers.device import Device
    from ...merossclient.protocol.types import thermostat as mt_t

    """
    "Appliance.System.Ability",
    {
        "Appliance.Config.DeviceCfg": {},
        "Appliance.Config.Sensor.Association": {},
        "Appliance.Control.AlertConfig": {},
        "Appliance.Control.AlertReport": {},
        "Appliance.Control.Sensor.Association": {},
        "Appliance.Control.Sensor.HistoryX": {},
        "Appliance.Control.Thermostat.HoldAction": {},
        "Appliance.Control.Thermostat.ScheduleB": {},
        "Appliance.Control.Thermostat.System": {},
    }
    TODO:
    -Appliance.Config.Sensor.Association likely carries info about which sensor (internal/external)
    is used as observed temp
    -Appliance.Control.Thermostat.Calibration
    {"calibration":[{"channel":0,"value":150,"min":-450,"max":450,"humiValue":-60}]}
    """


class Mts300Climate(MtsThermostatClimate):
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
        _mts_payload: mt_t.ModeC_C

        HVAC_MODE_TO_MODE_MAP: ClassVar
        _mts_work: int | None

        # HA core entity attributes:
        target_temperature_high: float | None
        target_temperature_low: float | None

        # entities
        number_fan_hold: MLConfigNumber
        switch_fan_hold: MLEmulatedSwitch
        sensor_hStatus: MLEnumSensor
        sensor_cStatus: MLEnumSensor
        sensor_fStatus: MLEnumSensor

    # MtsClimate class attributes
    ns = mn_t.Appliance_Control_Thermostat_ModeC
    device_scale = mc.MTS300_TEMP_SCALE

    class AdjustNumber(MtsThermostatClimate.AdjustNumber):

        if TYPE_CHECKING:
            """{"channel":0,"value":150,"min":-450,"max":450,"humiValue":-60}"""
            number_calibration_humi: MLConfigNumber

        ns = mn_t.Appliance_Control_Thermostat_Calibration

        __slots__ = ("number_calibration_humi",)

        def __init__(self, climate: "MtsThermostatClimate"):
            super().__init__(climate)
            self.native_max_value = 4.5
            self.native_min_value = -4.5
            self.native_step = 0.1

        def _parse(self, payload: "mt_t.Calibration_C"):
            if "humiValue" in payload:
                try:
                    self.number_calibration_humi.update_device_value(
                        payload["humiValue"]
                    )
                except AttributeError:
                    self.number_calibration_humi = MLConfigNumber(
                        self.manager,
                        self.channel,
                        "humidity_calibration",
                        device_class=MLConfigNumber.DeviceClass.HUMIDITY,
                        device_scale=10,
                        device_value=payload["humiValue"],
                    )
                    self.number_calibration_humi.ns = self.ns
                    self.number_calibration_humi.key_value = "humiValue"
                    self.number_calibration_humi.native_max_value = 5
                    self.number_calibration_humi.native_min_value = -5
                    self.number_calibration_humi.native_step = 0.1

            super()._parse(payload)

    class Schedule(MtsSchedule):
        ns = mn_t.Appliance_Control_Thermostat_ScheduleB

        # TODO: customize parsing of native payload since we have 2 temperatures

    MTS_MODE_TO_PRESET_MAP = {
        mc.MTS300_WORK_MANUAL: MtsThermostatClimate.Preset.CUSTOM,
        mc.MTS300_WORK_SCHEDULE: MtsThermostatClimate.Preset.AUTO,
    }
    MTS_MODE_TO_TEMPERATUREKEY_MAP = mc.MTS300_MODE_TO_TARGETTEMP_MAP

    # Mts300Climate class attributes
    HVAC_MODE_TO_MODE_MAP = {
        MtsThermostatClimate.HVACMode.OFF: mc.MTS300_MODE_OFF,
        MtsThermostatClimate.HVACMode.HEAT: mc.MTS300_MODE_HEAT,
        MtsThermostatClimate.HVACMode.COOL: mc.MTS300_MODE_COOL,
        MtsThermostatClimate.HVACMode.HEAT_COOL: mc.MTS300_MODE_AUTO,
    }
    FAN_MODE_TO_FAN_SPEED_MAP = {
        hacc.FAN_AUTO: mc.MTS300_FAN_SPEED_AUTO,
        hacc.FAN_LOW: mc.MTS300_FAN_SPEED_LOW,
        hacc.FAN_MEDIUM: mc.MTS300_FAN_SPEED_MEDIUM,
        hacc.FAN_HIGH: mc.MTS300_FAN_SPEED_HIGH,
    }
    STATUS_TO_HVAC_ACTION_MAP = {
        (False, False, False): MtsThermostatClimate.HVACAction.IDLE,
        # heating flag active (whatever the rest...)
        (True, False, False): MtsThermostatClimate.HVACAction.HEATING,
        (True, False, True): MtsThermostatClimate.HVACAction.HEATING,
        (True, True, False): MtsThermostatClimate.HVACAction.HEATING,
        (True, True, True): MtsThermostatClimate.HVACAction.HEATING,
        # cooling flag active (when not heating of course)
        (False, True, False): MtsThermostatClimate.HVACAction.COOLING,
        (False, True, True): MtsThermostatClimate.HVACAction.COOLING,
        # only fan active
        (False, False, True): MtsThermostatClimate.HVACAction.FAN,
    }
    """Status flags in "more" dict mapped as: (bool(hStatus), bool(cStatus), bool(fStatus))."""
    STATUS_SENSOR_DEF_MAP = {
        "hdStatus": MLEnumSensor.SensorDef(
            "(de)humidifier_status",
            translation_key="mts300_hdstatus",
            entity_category=MLEnumSensor.EntityCategory.DIAGNOSTIC,
        ),
        "hStatus": MLEnumSensor.SensorDef(
            "heating_status",
            translation_key="mts300_status",
            entity_category=MLEnumSensor.EntityCategory.DIAGNOSTIC,
        ),
        "cStatus": MLEnumSensor.SensorDef(
            "cooling_status",
            translation_key="mts300_status",
            entity_category=MLEnumSensor.EntityCategory.DIAGNOSTIC,
        ),
        "fStatus": MLEnumSensor.SensorDef("fan_speed", translation_key="mts300_status"),
        "aStatus": MLEnumSensor.SensorDef(
            "auxiliary_status",
            translation_key="mts300_status",
            entity_category=MLEnumSensor.EntityCategory.DIAGNOSTIC,
        ),
    }

    # HA core entity attributes:
    _attr_fan_modes = list(FAN_MODE_TO_FAN_SPEED_MAP)
    _attr_hvac_modes = list(HVAC_MODE_TO_MODE_MAP)
    _attr_preset_modes = list(MTS_MODE_TO_PRESET_MAP.values())
    _attr_supported_features = (
        MtsThermostatClimate.ClimateEntityFeature.PRESET_MODE
        | MtsThermostatClimate.ClimateEntityFeature.TARGET_TEMPERATURE
        | getattr(MtsThermostatClimate.ClimateEntityFeature, "TURN_OFF", 0)
        | getattr(MtsThermostatClimate.ClimateEntityFeature, "TURN_ON", 0)
        | MtsThermostatClimate.ClimateEntityFeature.TARGET_TEMPERATURE_RANGE
        | MtsThermostatClimate.ClimateEntityFeature.FAN_MODE
    )

    __slots__ = (
        "fan_mode",
        "fan_modes",
        "target_temperature_high",
        "target_temperature_low",
        "_mts_work",
        "number_fan_hold",
        "switch_fan_hold",
    ) + tuple(f"sensor_{_key}" for _key in STATUS_SENSOR_DEF_MAP)

    def __init__(
        self,
        manager: "Device",
        channel=0,
    ):
        super().__init__(manager, channel)
        self.fan_mode = None
        self.fan_modes = self._attr_fan_modes
        self.target_temperature_high = None
        self.target_temperature_low = None
        self._mts_work = None
        for _key, _def in Mts300Climate.STATUS_SENSOR_DEF_MAP.items():
            setattr(
                self,
                f"sensor_{_key}",
                _def.type(manager, channel, _def.entitykey, **_def.kwargs),
            )

        self.number_fan_hold = MLConfigNumber(
            manager,
            channel,
            "fan_hold_time",
            MLConfigNumber.DEVICE_CLASS_DURATION,
            device_scale=1,
            native_unit_of_measurement=MLConfigNumber.hac.UnitOfTime.MINUTES,
        )
        self.number_fan_hold.async_request_value = (
            self._async_request_value_number_fan_hold
        )
        self.switch_fan_hold = MLEmulatedSwitch(
            manager,
            channel,
            "fan_hold_enable",
        )
        self.switch_fan_hold.async_turn_on = self._async_turn_on_switch_fan_hold
        self.switch_fan_hold.async_turn_off = self._async_turn_off_switch_fan_hold

    async def async_shutdown(self):
        await super().async_shutdown()
        self.switch_fan_hold = None  # type:ignore
        self.number_fan_hold = None  # type:ignore
        for _key in Mts300Climate.STATUS_SENSOR_DEF_MAP:
            setattr(self, f"sensor_{_key}", None)

    # interface: MtsClimate
    def set_unavailable(self):
        self.fan_mode = None
        self.target_temperature_high = None
        self.target_temperature_low = None
        self._mts_work = None
        return super().set_unavailable()

    @override
    async def async_set_hvac_mode(self, hvac_mode: MtsThermostatClimate.HVACMode):
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
                raise ValueError(
                    f"set_temperature unsupported in this mode ({self.hvac_mode})"
                )

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
            self._parse_modeC(payload)  # type: ignore

    # message handlers
    def _parse_modeC(self, payload: "mt_t.ModeC_C"):
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
            for _key in Mts300Climate.STATUS_SENSOR_DEF_MAP:
                getattr(self, f"sensor_{_key}").update_native_value(more[_key])

            fan = payload["fan"]
            self.fan_mode = reverse_lookup(self.FAN_MODE_TO_FAN_SPEED_MAP, fan["speed"])
            fan_hold_time = fan["hTime"]
            if fan_hold_time == mc.MTS300_FAN_HOLD_DISABLED:
                # this doesn't update device_value so that it is saved and
                # eventually reused when switch_fan_hold toggles on
                self.number_fan_hold.update_native_value(None)
                self.switch_fan_hold.update_onoff(0)
            else:
                if not self.number_fan_hold.update_device_value(fan_hold_time):
                    # might happen when we toggle-on switch_fan_hold
                    self.number_fan_hold.update_native_value(fan_hold_time)
                self.switch_fan_hold.update_onoff(1)

            match mode := payload["mode"]:
                case mc.MTS300_MODE_OFF:
                    self._mts_onoff = 0
                    # don't set _mts_mode so we remembere last one
                    self.hvac_mode = MtsThermostatClimate.HVACMode.OFF
                    self.hvac_action = MtsThermostatClimate.HVACAction.OFF
                    self.target_temperature = None
                case mc.MTS300_MODE_HEAT:
                    self._mts_onoff = 1
                    self._mts_mode = mode
                    self.hvac_mode = MtsThermostatClimate.HVACMode.HEAT
                    self.hvac_action = Mts300Climate.STATUS_TO_HVAC_ACTION_MAP[
                        (
                            bool(more["hStatus"]),
                            False,
                            bool(more["fStatus"]),
                        )
                    ]
                    self.target_temperature = self.target_temperature_low
                case mc.MTS300_MODE_COOL:
                    self._mts_onoff = 1
                    self._mts_mode = mode
                    self.hvac_mode = MtsThermostatClimate.HVACMode.COOL
                    self.hvac_action = Mts300Climate.STATUS_TO_HVAC_ACTION_MAP[
                        (
                            False,
                            bool(more["cStatus"]),
                            bool(more["fStatus"]),
                        )
                    ]
                    self.target_temperature = self.target_temperature_high
                case mc.MTS300_MODE_AUTO:
                    self._mts_onoff = 1
                    self._mts_mode = mode
                    self.hvac_mode = MtsThermostatClimate.HVACMode.HEAT_COOL
                    self.hvac_action = Mts300Climate.STATUS_TO_HVAC_ACTION_MAP[
                        (
                            bool(more["hStatus"]),
                            bool(more["cStatus"]),
                            bool(more["fStatus"]),
                        )
                    ]
                    self.target_temperature = None

            self.flush_state()
        except Exception as e:
            self.log_exception(self.WARNING, e, "parsing thermostat ModeC", timeout=300)

    async def _async_request_value_number_fan_hold(self, device_value):
        # this method (ovverriding MLConfig.Number.async_request_value) should
        # return Success/Failure but we just return None (feailure) since the
        # number entity stata has already been updated/flushed in our _parse_modeC in case
        await self._async_request_modeC({"fan": {"hTime": device_value}})

    async def _async_turn_on_switch_fan_hold(self, **kwargs):
        h_time = self.number_fan_hold.device_value
        await self._async_request_modeC(
            {"fan": {"hTime": 60 if h_time is None else h_time}}
        )

    async def _async_turn_off_switch_fan_hold(self, **kwargs):
        await self._async_request_modeC({"fan": {"hTime": mc.MTS300_FAN_HOLD_DISABLED}})
