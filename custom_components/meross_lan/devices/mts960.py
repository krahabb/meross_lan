from time import time
import typing

from ..binary_sensor import MLBinarySensor
from ..calendar import MtsSchedule
from ..climate import MtsClimate
from ..merossclient import const as mc, namespaces as mn
from ..number import MLEmulatedNumber
from ..sensor import MLDiagnosticSensor

if typing.TYPE_CHECKING:
    from ..meross_device import MerossDevice
    from ..number import MtsTemperatureNumber


class MLTimerConfigNumber(MLEmulatedNumber):
    """
    Helper entity to configure countdown/cycle timer durations.
    """

    # HA core entity attributes:
    native_max_value = 1440  # 1 day max duration (no real info just guessing)
    native_min_value = 1
    native_step = 1

    def __init__(self, climate: "Mts960Climate", entitykey: str):
        super().__init__(
            climate.manager,
            climate.channel,
            entitykey,
            MLEmulatedNumber.DEVICE_CLASS_DURATION,
            native_unit_of_measurement=MLEmulatedNumber.hac.UnitOfTime.MINUTES,
        )


class Mts960PlugState(MLBinarySensor):

    # HA core entity attributes:
    entity_registry_enabled_default = False

    @property
    def icon(self):
        return "mdi:power-plug" if self.is_on else "mdi:power-plug-off"


class Mts960Climate(MtsClimate):
    """Climate entity for MTS960 devices"""

    manager: "MerossDevice"
    ns = mn.Appliance_Control_Thermostat_ModeB
    device_scale = mc.MTS960_TEMP_SCALE

    PRESET_HEATING: typing.Final = "heating"
    PRESET_COOLING: typing.Final = "cooling"
    PRESET_SCHEDULE_HEATING: typing.Final = "schedule_heating"
    PRESET_SCHEDULE_COOLING: typing.Final = "schedule_cooling"
    PRESET_TIMER_CYCLE: typing.Final = "timer_cycle"
    PRESET_TIMER_COUNTDOWN_ON: typing.Final = "timer_countdown_on"
    PRESET_TIMER_COUNTDOWN_OFF: typing.Final = "timer_countdown_off"

    TIMER_TYPE_KEY = {
        mc.MTS960_TIMER_TYPE_COUNTDOWN: mc.KEY_DOWN,
        mc.MTS960_TIMER_TYPE_CYCLE: mc.KEY_CYCLE,
    }

    MTS_MODE_TO_PRESET_MAP = {}

    DIAGNOSTIC_SENSOR_KEYS = (
        mc.KEY_MODE,
        mc.KEY_ONOFF,
        mc.KEY_STATE,
        mc.KEY_SENSORSTATUS,
        mc.KEY_WORKING,
    )

    # HA core entity attributes:
    hvac_modes = [
        MtsClimate.HVACMode.OFF,
        MtsClimate.HVACMode.HEAT,
        MtsClimate.HVACMode.COOL,
        MtsClimate.HVACMode.AUTO,
        MtsClimate.HVACMode.FAN_ONLY,
    ]
    preset_modes = [
        PRESET_HEATING,
        PRESET_COOLING,
        PRESET_SCHEDULE_HEATING,
        PRESET_SCHEDULE_COOLING,
        PRESET_TIMER_CYCLE,
        PRESET_TIMER_COUNTDOWN_ON,
        PRESET_TIMER_COUNTDOWN_OFF,
    ]

    __slots__ = (
        "binary_sensor_plug_state",
        "number_timer_down_duration",
        "number_timer_cycle_off_duration",
        "number_timer_cycle_on_duration",
        "_mts_working",
        "_mts_timer_payload",
        "_mts_timer_mode",
    )

    def __init__(
        self,
        manager: "MerossDevice",
        channel: object,
        adjust_number_class: typing.Type["MtsTemperatureNumber"],
    ):
        self._mts_working = None
        self._mts_timer_payload = None
        self._mts_timer_mode = None
        super().__init__(
            manager,
            channel,
            adjust_number_class,
            None,
            Mts960Schedule,
        )
        self.binary_sensor_plug_state = Mts960PlugState(manager, channel, "plug_state")
        self.number_timer_down_duration = MLTimerConfigNumber(
            self, "timer_down_duration"
        )
        self.number_timer_cycle_off_duration = MLTimerConfigNumber(
            self, "timer_cycle_off_duration"
        )
        self.number_timer_cycle_on_duration = MLTimerConfigNumber(
            self, "timer_cycle_on_duration"
        )

    # interface: MerossEntity
    async def async_shutdown(self):
        await super().async_shutdown()
        self.binary_sensor_plug_state: Mts960PlugState = None  # type: ignore
        self.number_timer_down_duration: MLTimerConfigNumber = None  # type: ignore
        self.number_timer_cycle_off_duration: MLTimerConfigNumber = None  # type: ignore
        self.number_timer_cycle_on_duration: MLTimerConfigNumber = None  # type: ignore

    def set_unavailable(self):
        self._mts_working = None
        self._mts_timer_payload = None
        self._mts_timer_mode = None
        super().set_unavailable()

    def flush_state(self):
        """interface: MtsClimate."""
        if self._mts_onoff:
            match self._mts_mode:
                case mc.MTS960_MODE_HEAT_COOL:
                    match self._mts_working:
                        case mc.MTS960_WORKING_HEAT:
                            self.preset_mode = Mts960Climate.PRESET_HEATING
                            self.hvac_mode = MtsClimate.HVACMode.HEAT
                            self.hvac_action = (
                                MtsClimate.HVACAction.HEATING
                                if self._mts_active
                                else MtsClimate.HVACAction.IDLE
                            )
                        case mc.MTS960_WORKING_COOL:
                            self.preset_mode = Mts960Climate.PRESET_COOLING
                            self.hvac_mode = MtsClimate.HVACMode.COOL
                            self.hvac_action = (
                                MtsClimate.HVACAction.COOLING
                                if self._mts_active
                                else MtsClimate.HVACAction.IDLE
                            )
                        case _:
                            self.preset_mode = None
                            self.hvac_mode = None
                            self.hvac_action = None
                            # TODO: log warning?
                case mc.MTS960_MODE_SCHEDULE:
                    self.hvac_mode = MtsClimate.HVACMode.AUTO
                    match self._mts_working:
                        case mc.MTS960_WORKING_HEAT:
                            self.preset_mode = Mts960Climate.PRESET_SCHEDULE_HEATING
                            self.hvac_action = (
                                MtsClimate.HVACAction.HEATING
                                if self._mts_active
                                else MtsClimate.HVACAction.IDLE
                            )
                        case mc.MTS960_WORKING_COOL:
                            self.preset_mode = Mts960Climate.PRESET_SCHEDULE_COOLING
                            self.hvac_action = (
                                MtsClimate.HVACAction.COOLING
                                if self._mts_active
                                else MtsClimate.HVACAction.IDLE
                            )
                        case _:
                            self.preset_mode = None
                            self.hvac_action = None
                            # TODO: log warning?
                case mc.MTS960_MODE_TIMER:
                    match self._mts_timer_mode:
                        case (mc.MTS960_TIMER_TYPE_CYCLE, _):
                            self.preset_mode = Mts960Climate.PRESET_TIMER_CYCLE
                        case (mc.MTS960_TIMER_TYPE_COUNTDOWN, mc.MTS960_ONOFF_OFF):
                            self.preset_mode = Mts960Climate.PRESET_TIMER_COUNTDOWN_OFF
                        case (mc.MTS960_TIMER_TYPE_COUNTDOWN, mc.MTS960_ONOFF_ON):
                            self.preset_mode = Mts960Climate.PRESET_TIMER_COUNTDOWN_ON
                        case _:
                            self.preset_mode = None
                    self.hvac_mode = MtsClimate.HVACMode.FAN_ONLY
                    self.hvac_action = (
                        MtsClimate.HVACAction.FAN
                        if self._mts_active
                        else MtsClimate.HVACAction.IDLE
                    )
                case _:
                    self.preset_mode = None
                    self.hvac_mode = None
                    self.hvac_action = None
                    # TODO: log warning?
        else:
            self.hvac_mode = MtsClimate.HVACMode.OFF
            self.hvac_action = MtsClimate.HVACAction.OFF

        super().flush_state()

    async def async_set_hvac_mode(self, hvac_mode: MtsClimate.HVACMode):
        match hvac_mode:
            case MtsClimate.HVACMode.OFF:
                await self.async_request_onoff(0)
            case MtsClimate.HVACMode.HEAT:
                await self._async_request_modeb(
                    {
                        mc.KEY_CHANNEL: self.channel,
                        mc.KEY_ONOFF: mc.MTS960_ONOFF_ON,
                        mc.KEY_MODE: mc.MTS960_MODE_HEAT_COOL,
                        mc.KEY_WORKING: mc.MTS960_WORKING_HEAT,
                    }
                )
            case MtsClimate.HVACMode.COOL:
                await self._async_request_modeb(
                    {
                        mc.KEY_CHANNEL: self.channel,
                        mc.KEY_ONOFF: mc.MTS960_ONOFF_ON,
                        mc.KEY_MODE: mc.MTS960_MODE_HEAT_COOL,
                        mc.KEY_WORKING: mc.MTS960_WORKING_COOL,
                    }
                )
            case MtsClimate.HVACMode.AUTO:
                # preserves heating/cooling as actually set in the device
                await self._async_request_modeb(
                    {
                        mc.KEY_CHANNEL: self.channel,
                        mc.KEY_ONOFF: mc.MTS960_ONOFF_ON,
                        mc.KEY_MODE: mc.MTS960_MODE_SCHEDULE,
                    }
                )
            case MtsClimate.HVACMode.FAN_ONLY:
                await self._async_request_modeb(
                    {
                        mc.KEY_CHANNEL: self.channel,
                        mc.KEY_ONOFF: mc.MTS960_ONOFF_ON,
                        mc.KEY_MODE: mc.MTS960_MODE_TIMER,
                    }
                )

    async def async_set_preset_mode(self, preset_mode: str):
        match preset_mode:
            case Mts960Climate.PRESET_HEATING:
                await self.async_set_hvac_mode(MtsClimate.HVACMode.HEAT)
            case Mts960Climate.PRESET_COOLING:
                await self.async_set_hvac_mode(MtsClimate.HVACMode.COOL)
            case Mts960Climate.PRESET_SCHEDULE_HEATING:
                await self._async_request_modeb(
                    {
                        mc.KEY_CHANNEL: self.channel,
                        mc.KEY_ONOFF: mc.MTS960_ONOFF_ON,
                        mc.KEY_MODE: mc.MTS960_MODE_SCHEDULE,
                        mc.KEY_WORKING: mc.MTS960_WORKING_HEAT,
                    }
                )
            case Mts960Climate.PRESET_SCHEDULE_COOLING:
                await self._async_request_modeb(
                    {
                        mc.KEY_CHANNEL: self.channel,
                        mc.KEY_ONOFF: mc.MTS960_ONOFF_ON,
                        mc.KEY_MODE: mc.MTS960_MODE_SCHEDULE,
                        mc.KEY_WORKING: mc.MTS960_WORKING_COOL,
                    }
                )
            case Mts960Climate.PRESET_TIMER_CYCLE:
                # how to start the timer is still unknown..here a guessed impl
                # trying to start a cycle timer in 'on' state
                offduration = round(
                    self.number_timer_cycle_off_duration.native_value or 1
                )
                onduration = round(
                    self.number_timer_cycle_on_duration.native_value or 1
                )
                device_timestamp = round(time() - self.manager.device_timedelta)
                if await self._async_request_timer(
                    mc.MTS960_TIMER_TYPE_CYCLE,
                    {
                        mc.KEY_OFFDURATION: offduration,
                        mc.KEY_ONDURATION: onduration,
                        mc.KEY_STATE: mc.MTS960_STATE_ON,
                        mc.KEY_END: device_timestamp + (onduration * 60),
                    },
                ):
                    await self.async_set_hvac_mode(MtsClimate.HVACMode.FAN_ONLY)
            case Mts960Climate.PRESET_TIMER_COUNTDOWN_ON:
                duration = round(self.number_timer_down_duration.native_value or 1)
                device_timestamp = round(time() - self.manager.device_timedelta)
                if await self._async_request_timer(
                    mc.MTS960_TIMER_TYPE_COUNTDOWN,
                    {
                        mc.KEY_DURATION: duration,
                        mc.KEY_ONOFF: mc.MTS960_ONOFF_ON,
                        mc.KEY_END: device_timestamp + (duration * 60),
                    },
                ):
                    await self.async_set_hvac_mode(MtsClimate.HVACMode.FAN_ONLY)
            case Mts960Climate.PRESET_TIMER_COUNTDOWN_OFF:
                duration = round(self.number_timer_down_duration.native_value or 1)
                device_timestamp = round(time() - self.manager.device_timedelta)
                if await self._async_request_timer(
                    mc.MTS960_TIMER_TYPE_COUNTDOWN,
                    {
                        mc.KEY_DURATION: duration,
                        mc.KEY_ONOFF: mc.MTS960_ONOFF_OFF,
                        mc.KEY_END: device_timestamp + (duration * 60),
                    },
                ):
                    await self.async_set_hvac_mode(MtsClimate.HVACMode.FAN_ONLY)

    async def async_set_temperature(self, **kwargs):
        # bumps out of any timer/schedule mode and sets target temp
        # preserving heating/cooling mode
        await self._async_request_modeb(
            {
                mc.KEY_CHANNEL: self.channel,
                mc.KEY_MODE: mc.MTS960_MODE_HEAT_COOL,
                mc.KEY_WORKING: self._mts_working or mc.MTS960_WORKING_HEAT,
                mc.KEY_TARGETTEMP: round(
                    kwargs[self.ATTR_TEMPERATURE] * self.device_scale
                ),
            }
        )

    async def async_request_mode(self, mode: int):
        await self._async_request_modeb(
            {
                mc.KEY_CHANNEL: self.channel,
                mc.KEY_ONOFF: mc.MTS960_ONOFF_ON,
                mc.KEY_MODE: mode,
            }
        )

    async def async_request_onoff(self, onoff: int):
        await self._async_request_modeb(
            {
                mc.KEY_CHANNEL: self.channel,
                mc.KEY_ONOFF: mc.MTS960_ONOFF_ON if onoff else mc.MTS960_ONOFF_OFF,
            }
        )

    def is_mts_scheduled(self):
        return self._mts_onoff and (self._mts_mode == mc.MTS960_MODE_SCHEDULE)

    def get_ns_adjust(self):
        return self.manager.namespace_handlers[
            mn.Appliance_Control_Thermostat_Calibration.name
        ]

    # interface: self
    async def _async_request_modeb(self, p_modeb: dict):
        if response := await self.manager.async_request_ack(
            self.ns.name,
            mc.METHOD_SET,
            {self.ns.key: [p_modeb]},
        ):
            try:
                payload = response[mc.KEY_PAYLOAD][mc.KEY_MODEB][0]
            except (KeyError, IndexError):
                # optimistic update
                payload = self._mts_payload | p_modeb
            self._parse_modeB(payload)

    async def _async_request_timer(self, timer_type: int, payload: dict):
        p_timer = {
            mc.KEY_CHANNEL: self.channel,
            mc.KEY_TYPE: timer_type,
            Mts960Climate.TIMER_TYPE_KEY[timer_type]: payload,
        }
        if response := await self.manager.async_request_ack(
            mn.Appliance_Control_Thermostat_Timer.name,
            mc.METHOD_SET,
            {mc.KEY_TIMER: [p_timer]},
        ):
            try:
                payload = response[mc.KEY_PAYLOAD][mc.KEY_TIMER][0]
            except (KeyError, IndexError):
                # optimistic update
                payload = p_timer
            self._parse_timer(payload)
            return True

    # message handlers
    def _parse_ctlRange(self, payload: dict):
        """
        {
            "channel": 0,
            "max": 11000,
            "min": -3000,
            "ctlMax": 3600,
            "ctlMin": 300,
        }
        """
        self.max_temp = payload[mc.KEY_CTLMAX] / self.device_scale
        self.min_temp = payload[mc.KEY_CTLMIN] / self.device_scale

    def _parse_modeB(self, payload: dict):
        """
        {
            "mode": 3,
            "targetTemp": 0,
            "working": 2,
            "currentTemp": 1936,
            "state": 2,
            "onoff": 1,
            "sensorStatus": 1,
            "channel": 0,
        }
        """
        if self._mts_payload == payload:
            return
        self._mts_payload = payload
        if mc.KEY_MODE in payload:
            self._mts_mode = payload[mc.KEY_MODE]
        if mc.KEY_ONOFF in payload:
            self._mts_onoff = payload[mc.KEY_ONOFF] == mc.MTS960_ONOFF_ON
        if mc.KEY_STATE in payload:
            self._mts_active = payload[mc.KEY_STATE] == mc.MTS960_STATE_ON
            self.binary_sensor_plug_state.update_onoff(self._mts_active)
        if mc.KEY_WORKING in payload:
            self._mts_working = payload[mc.KEY_WORKING]
        if mc.KEY_CURRENTTEMP in payload:
            self._update_current_temperature(payload[mc.KEY_CURRENTTEMP])
        if mc.KEY_TARGETTEMP in payload:
            self.target_temperature = (
                (payload[mc.KEY_TARGETTEMP] / self.device_scale)
                if self._mts_mode != mc.MTS960_MODE_TIMER
                else None
            )

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

    def _parse_timer(self, payload: dict):
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
                payload = payload[mc.KEY_DOWN]
                self._mts_timer_mode = (
                    mc.MTS960_TIMER_TYPE_COUNTDOWN,
                    payload[mc.KEY_ONOFF],
                )
                self.number_timer_down_duration.update_native_value(
                    payload[mc.KEY_DURATION]
                )
            case mc.MTS960_TIMER_TYPE_CYCLE:
                payload = payload[mc.KEY_CYCLE]
                self._mts_timer_mode = (mc.MTS960_TIMER_TYPE_CYCLE, None)
                self.number_timer_cycle_off_duration.update_native_value(
                    payload[mc.KEY_OFFDURATION]
                )
                self.number_timer_cycle_on_duration.update_native_value(
                    payload[mc.KEY_ONDURATION]
                )
            case _:
                self._mts_timer_mode = (payload[mc.KEY_TYPE], None)

        if self._mts_mode == mc.MTS960_MODE_TIMER:
            self.flush_state()


class Mts960Schedule(MtsSchedule):
    ns = mn.Appliance_Control_Thermostat_ScheduleB
