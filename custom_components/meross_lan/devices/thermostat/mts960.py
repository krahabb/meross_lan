import enum
from typing import TYPE_CHECKING, override

from . import MtsThermostatClimate, mc, mlc, mn_t
from ...binary_sensor import BinarySensor
from ...number import EmulatedNumber
from ...sensor import DiagnosticSensor

if TYPE_CHECKING:
    from typing import Final

    from ...helpers.device import Device
    from ...merossclient.protocol import types as mt


class Mts960Climate(MtsThermostatClimate):
    """Climate entity for MTS960 devices"""

    class Preset(enum.StrEnum):
        HEATING = enum.auto()
        COOLING = enum.auto()
        SCHEDULE_HEATING = enum.auto()
        SCHEDULE_COOLING = enum.auto()
        TIMER_CYCLE = enum.auto()
        TIMER_COUNTDOWN_ON = enum.auto()
        TIMER_COUNTDOWN_OFF = enum.auto()

    class PlugState(BinarySensor):

        init_entity_key = "plug_state"

        # HA core entity attributes:
        _attr_entity_registry_enabled_default = False

        @property
        def icon(self):
            return "mdi:power-plug" if self.is_on else "mdi:power-plug-off"

    class TimerConfigNumber(EmulatedNumber):
        """
        Helper entity to configure countdown/cycle timer durations.
        """

        # HA core entity attributes:
        _attr_device_class = EmulatedNumber.DEVICE_CLASS_DURATION
        _attr_native_unit_of_measurement = mlc.hac.UnitOfTime.MINUTES
        _attr_native_max_value = 1440  # 1 day max duration (no real info just guessing)
        _attr_native_min_value = 1
        _attr_native_step = 1

        def __init__(self, climate: "Mts960Climate", entity_key: str, /):
            EmulatedNumber.__init__(
                self, climate.channel, climate.parent, entity_key=entity_key
            )

    if TYPE_CHECKING:
        ns_payload: mt.thermostat.ModeB_C
        binary_sensor_plug_state: PlugState
        number_timer_down_duration: TimerConfigNumber
        number_timer_cycle_off_duration: TimerConfigNumber
        number_timer_cycle_on_duration: TimerConfigNumber

    temperature_scale = mc.MTS960_TEMP_SCALE
    SCHEDULE_NS = mn_t.Appliance_Control_Thermostat_ScheduleB
    MTS_MODE_TO_PRESET_MAP = {}

    TIMER_TYPE_KEY = {
        mc.MTS960_TIMER_TYPE_COUNTDOWN: mc.KEY_DOWN,
        mc.MTS960_TIMER_TYPE_CYCLE: mc.KEY_CYCLE,
    }

    DIAGNOSTIC_SENSOR_KEYS = (
        mc.KEY_MODE,
        mc.KEY_ONOFF,
        mc.KEY_STATE,
        mc.KEY_SENSORSTATUS,
        mc.KEY_WORKING,
    )

    # HA core entity attributes:
    _attr_hvac_modes = [
        MtsThermostatClimate.HVACMode.OFF,
        MtsThermostatClimate.HVACMode.HEAT,
        MtsThermostatClimate.HVACMode.COOL,
        MtsThermostatClimate.HVACMode.AUTO,
        MtsThermostatClimate.HVACMode.FAN_ONLY,
    ]
    _attr_preset_modes = list(Preset)

    __slots__ = (
        "binary_sensor_plug_state",
        "number_timer_down_duration",
        "number_timer_cycle_off_duration",
        "number_timer_cycle_on_duration",
        "_mts_working",
        "_mts_timer_payload",
        "_mts_timer_mode",
    )

    def __init__(self, channel: int, device: "Device", /, **kwargs):
        self._mts_working = None
        self._mts_timer_payload = None
        self._mts_timer_mode = None
        super().__init__(channel, device, **kwargs)
        device.register_parser_ex(
            self,
            mn_t.Appliance_Control_Thermostat_CtlRange,
            mn_t.Appliance_Control_Thermostat_Timer,
        )
        self.binary_sensor_plug_state = Mts960Climate.PlugState(channel, device)
        self.number_timer_down_duration = Mts960Climate.TimerConfigNumber(
            self, "timer_down_duration"
        )
        self.number_timer_cycle_off_duration = Mts960Climate.TimerConfigNumber(
            self, "timer_cycle_off_duration"
        )
        self.number_timer_cycle_on_duration = Mts960Climate.TimerConfigNumber(
            self, "timer_cycle_on_duration"
        )

    def shutdown(self):
        super().shutdown()
        del self.binary_sensor_plug_state
        del self.number_timer_down_duration
        del self.number_timer_cycle_off_duration
        del self.number_timer_cycle_on_duration

    def set_unavailable(self):
        self._mts_working = None
        self._mts_timer_payload = None
        self._mts_timer_mode = None
        super().set_unavailable()

    # interface: MtsThermostatClimate
    def flush_state(self):
        if self._mts_onoff:
            try:
                match self._mts_mode:
                    case mc.MTS960_MODE_HEAT_COOL:
                        match self._mts_working:
                            case mc.MTS960_WORKING_HEAT:
                                self.preset_mode = Mts960Climate.Preset.HEATING
                                self.hvac_mode = MtsThermostatClimate.HVACMode.HEAT
                                self.hvac_action = (
                                    MtsThermostatClimate.HVACAction.HEATING
                                    if self._mts_active
                                    else MtsThermostatClimate.HVACAction.IDLE
                                )
                            case mc.MTS960_WORKING_COOL:
                                self.preset_mode = Mts960Climate.Preset.COOLING
                                self.hvac_mode = MtsThermostatClimate.HVACMode.COOL
                                self.hvac_action = (
                                    MtsThermostatClimate.HVACAction.COOLING
                                    if self._mts_active
                                    else MtsThermostatClimate.HVACAction.IDLE
                                )
                            case _:
                                raise ValueError(
                                    f"Unknown MTS960 working mode: {self._mts_working}"
                                )
                    case mc.MTS960_MODE_SCHEDULE:
                        match self._mts_working:
                            case mc.MTS960_WORKING_HEAT:
                                self.preset_mode = Mts960Climate.Preset.SCHEDULE_HEATING
                                self.hvac_action = (
                                    MtsThermostatClimate.HVACAction.HEATING
                                    if self._mts_active
                                    else MtsThermostatClimate.HVACAction.IDLE
                                )
                            case mc.MTS960_WORKING_COOL:
                                self.preset_mode = Mts960Climate.Preset.SCHEDULE_COOLING
                                self.hvac_action = (
                                    MtsThermostatClimate.HVACAction.COOLING
                                    if self._mts_active
                                    else MtsThermostatClimate.HVACAction.IDLE
                                )
                            case _:
                                raise ValueError(
                                    f"Unknown MTS960 working mode: {self._mts_working}"
                                )
                        self.hvac_mode = MtsThermostatClimate.HVACMode.AUTO
                    case mc.MTS960_MODE_TIMER:
                        match self._mts_timer_mode:
                            case (mc.MTS960_TIMER_TYPE_CYCLE, _):
                                self.preset_mode = Mts960Climate.Preset.TIMER_CYCLE
                            case (mc.MTS960_TIMER_TYPE_COUNTDOWN, mc.MTS960_ONOFF_OFF):
                                self.preset_mode = (
                                    Mts960Climate.Preset.TIMER_COUNTDOWN_OFF
                                )
                            case (mc.MTS960_TIMER_TYPE_COUNTDOWN, mc.MTS960_ONOFF_ON):
                                self.preset_mode = (
                                    Mts960Climate.Preset.TIMER_COUNTDOWN_ON
                                )
                            case _:
                                raise ValueError(
                                    f"Unknown MTS960 timer mode: {self._mts_timer_mode}"
                                )
                        self.hvac_mode = MtsThermostatClimate.HVACMode.FAN_ONLY
                        self.hvac_action = (
                            MtsThermostatClimate.HVACAction.FAN
                            if self._mts_active
                            else MtsThermostatClimate.HVACAction.IDLE
                        )
                    case _:
                        raise ValueError(f"Unknown MTS960 mode: {self._mts_mode}")
            except Exception as e:
                self.preset_mode = None
                self.hvac_mode = None
                self.hvac_action = None
                self.log_exception(
                    self.WARNING, e, "sending state to HomeAssistant", timeout=14400
                )
        else:
            self.hvac_mode = MtsThermostatClimate.HVACMode.OFF
            self.hvac_action = MtsThermostatClimate.HVACAction.OFF

        super().flush_state()

    async def async_set_hvac_mode(self, hvac_mode: MtsThermostatClimate.HVACMode):
        match hvac_mode:
            case MtsThermostatClimate.HVACMode.OFF:
                await self.async_request_onoff(0)
            case MtsThermostatClimate.HVACMode.HEAT:
                await self.async_request_parse_ex(
                    {
                        mc.KEY_ONOFF: mc.MTS960_ONOFF_ON,
                        mc.KEY_MODE: mc.MTS960_MODE_HEAT_COOL,
                        mc.KEY_WORKING: mc.MTS960_WORKING_HEAT,
                    },
                )
            case MtsThermostatClimate.HVACMode.COOL:
                await self.async_request_parse_ex(
                    {
                        mc.KEY_ONOFF: mc.MTS960_ONOFF_ON,
                        mc.KEY_MODE: mc.MTS960_MODE_HEAT_COOL,
                        mc.KEY_WORKING: mc.MTS960_WORKING_COOL,
                    },
                )
            case MtsThermostatClimate.HVACMode.AUTO:
                # preserves heating/cooling as actually set in the device
                await self.async_request_parse_ex(
                    {
                        mc.KEY_ONOFF: mc.MTS960_ONOFF_ON,
                        mc.KEY_MODE: mc.MTS960_MODE_SCHEDULE,
                    }
                )
            case MtsThermostatClimate.HVACMode.FAN_ONLY:
                await self.async_request_parse_ex(
                    {
                        mc.KEY_ONOFF: mc.MTS960_ONOFF_ON,
                        mc.KEY_MODE: mc.MTS960_MODE_TIMER,
                    }
                )

    async def async_set_preset_mode(self, preset_mode: str):
        match preset_mode:
            case Mts960Climate.Preset.HEATING:
                await self.async_set_hvac_mode(MtsThermostatClimate.HVACMode.HEAT)
            case Mts960Climate.Preset.COOLING:
                await self.async_set_hvac_mode(MtsThermostatClimate.HVACMode.COOL)
            case Mts960Climate.Preset.SCHEDULE_HEATING:
                await self.async_request_parse_ex(
                    {
                        mc.KEY_ONOFF: mc.MTS960_ONOFF_ON,
                        mc.KEY_MODE: mc.MTS960_MODE_SCHEDULE,
                        mc.KEY_WORKING: mc.MTS960_WORKING_HEAT,
                    },
                )
            case Mts960Climate.Preset.SCHEDULE_COOLING:
                await self.async_request_parse_ex(
                    {
                        mc.KEY_ONOFF: mc.MTS960_ONOFF_ON,
                        mc.KEY_MODE: mc.MTS960_MODE_SCHEDULE,
                        mc.KEY_WORKING: mc.MTS960_WORKING_COOL,
                    },
                )
            case Mts960Climate.Preset.TIMER_CYCLE:
                # how to start the timer is still unknown..here a guessed impl
                # trying to start a cycle timer in 'on' state
                offduration = round(
                    self.number_timer_cycle_off_duration.native_value or 1
                )
                onduration = round(
                    self.number_timer_cycle_on_duration.native_value or 1
                )
                device_timestamp = round(self.time() - self.parent.device_timedelta)
                await self._async_request_timer(
                    mc.MTS960_TIMER_TYPE_CYCLE,
                    {
                        mc.KEY_OFFDURATION: offduration,
                        mc.KEY_ONDURATION: onduration,
                        mc.KEY_STATE: mc.MTS960_STATE_ON,
                        mc.KEY_END: device_timestamp + (onduration * 60),
                    },
                )
            case Mts960Climate.Preset.TIMER_COUNTDOWN_ON:
                duration = round(self.number_timer_down_duration.native_value or 1)
                device_timestamp = round(self.time() - self.parent.device_timedelta)
                await self._async_request_timer(
                    mc.MTS960_TIMER_TYPE_COUNTDOWN,
                    {
                        mc.KEY_DURATION: duration,
                        mc.KEY_ONOFF: mc.MTS960_ONOFF_ON,
                        mc.KEY_END: device_timestamp + (duration * 60),
                    },
                )
            case Mts960Climate.Preset.TIMER_COUNTDOWN_OFF:
                duration = round(self.number_timer_down_duration.native_value or 1)
                device_timestamp = round(self.time() - self.parent.device_timedelta)
                await self._async_request_timer(
                    mc.MTS960_TIMER_TYPE_COUNTDOWN,
                    {
                        mc.KEY_DURATION: duration,
                        mc.KEY_ONOFF: mc.MTS960_ONOFF_OFF,
                        mc.KEY_END: device_timestamp + (duration * 60),
                    },
                )

    async def async_set_temperature(self, **kwargs):
        # bumps out of any timer/schedule mode and sets target temp
        # preserving heating/cooling mode
        await self.async_request_parse_ex(
            {
                mc.KEY_MODE: mc.MTS960_MODE_HEAT_COOL,
                mc.KEY_WORKING: self._mts_working or mc.MTS960_WORKING_HEAT,
                mc.KEY_TARGETTEMP: round(
                    kwargs[self.ATTR_TEMPERATURE] * self.temperature_scale
                ),
            },
        )

    @override
    async def async_request_preset(self, mode: int, /):
        await self.async_request_parse_ex(
            {mc.KEY_ONOFF: mc.MTS960_ONOFF_ON, mc.KEY_MODE: mode},
        )

    @override
    async def async_request_onoff(self, onoff: int, /):
        await self.async_request_parse_ex(
            {
                mc.KEY_ONOFF: mc.MTS960_ONOFF_ON if onoff else mc.MTS960_ONOFF_OFF,
            },
        )

    @override
    def is_mts_scheduled(self):
        return self._mts_onoff and (self._mts_mode == mc.MTS960_MODE_SCHEDULE)

    # interface: self
    async def _async_request_timer(self, timer_type: int, payload: dict, /):
        await self.handlers[mn_t.Appliance_Control_Thermostat_Timer].async_set_c_ex(
            {
                mc.KEY_TYPE: timer_type,
                Mts960Climate.TIMER_TYPE_KEY[timer_type]: payload,
            },
            self,
        )
        await self.async_request_parse_ex(
            {
                mc.KEY_ONOFF: mc.MTS960_ONOFF_ON,
                mc.KEY_MODE: mc.MTS960_MODE_TIMER,
            }
        )

    # message handlers
    def _parse(self, payload: "mt.thermostat.ModeB_C", /):
        if self.ns_payload == payload:
            return
        self.ns_payload = payload
        if mc.KEY_MODE in payload:
            self._mts_mode = payload[mc.KEY_MODE]
        if mc.KEY_ONOFF in payload:
            self._mts_onoff = payload[mc.KEY_ONOFF] == mc.MTS960_ONOFF_ON
        if mc.KEY_STATE in payload:
            self._mts_active = payload[mc.KEY_STATE] == mc.MTS960_STATE_ON
            self.binary_sensor_plug_state.update_boolean_value(self._mts_active)
        if mc.KEY_WORKING in payload:
            self._mts_working = payload[mc.KEY_WORKING]
        if mc.KEY_CURRENTTEMP in payload:
            self._update_current_temperature(payload[mc.KEY_CURRENTTEMP])
        if mc.KEY_TARGETTEMP in payload:
            self.target_temperature = (
                (payload[mc.KEY_TARGETTEMP] / self.temperature_scale)
                if self._mts_mode != mc.MTS960_MODE_TIMER
                else None
            )

        device = self.parent
        if device.create_diagnostic_entities:
            entities: dict[str, DiagnosticSensor] = device.entities  # type: ignore
            channel = self.channel
            for key in self.DIAGNOSTIC_SENSOR_KEYS:
                try:
                    native_value = payload[key]
                    entities[f"{channel}_{key}"].update_device_value(native_value)
                except KeyError as key_error:
                    if key_error.args[0] != key:
                        DiagnosticSensor(
                            channel, device, entity_key=key, native_value=native_value
                        )

        self.flush_state()

    def _parse_ctlRange(self, payload: dict, /):
        """
        {
            "channel": 0,
            "max": 11000,
            "min": -3000,
            "ctlMax": 3600,
            "ctlMin": 300,
        }
        """
        self.max_temp = payload[mc.KEY_CTLMAX] / self.temperature_scale
        self.min_temp = payload[mc.KEY_CTLMIN] / self.temperature_scale

    def _parse_timer(self, payload: "mt.thermostat.Timer_C", /):
        """
        {'channel': 0, 'type': 1, 'down': {'duration': 1, 'end': 1718724107, 'onoff': 2}} ==> Count down Off
        {'channel': 0, 'type': 1, 'down': {'duration': 1, 'end': 1718724107, 'onoff': 1}} ==> Count down On
        {'channel': 0, 'type': 2, 'cycle': {'offDuration': 15, 'state': 1, 'end': 1718725103, 'onDuration': 15} } ==> cycle Current On
        {'channel': 0, 'type': 2, 'cycle': {'offDuration': 15, 'state': 2, 'end': 1718725103, 'onDuration': 15} } ==> cycle Current Off
        """
        if self._mts_timer_payload == payload:
            return
        self._mts_timer_payload = payload
        match payload[mc.KEY_TYPE]:
            case mc.MTS960_TIMER_TYPE_COUNTDOWN:
                p_down: "mt.thermostat.Timer_Down" = payload[mc.KEY_DOWN]  # type: ignore
                self._mts_timer_mode = (
                    mc.MTS960_TIMER_TYPE_COUNTDOWN,
                    p_down[mc.KEY_ONOFF],
                )
                self.number_timer_down_duration.update_native_value(
                    p_down[mc.KEY_DURATION]
                )
            case mc.MTS960_TIMER_TYPE_CYCLE:
                p_cycle: "mt.thermostat.Timer_Cycle" = payload[mc.KEY_CYCLE]  # type: ignore
                self._mts_timer_mode = (mc.MTS960_TIMER_TYPE_CYCLE, None)
                self.number_timer_cycle_off_duration.update_native_value(
                    p_cycle[mc.KEY_OFFDURATION]
                )
                self.number_timer_cycle_on_duration.update_native_value(
                    p_cycle[mc.KEY_ONDURATION]
                )
            case _:
                self._mts_timer_mode = (payload[mc.KEY_TYPE], None)

        if self._mts_mode == mc.MTS960_MODE_TIMER:
            self.flush_state()
