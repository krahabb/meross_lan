import typing

from ..calendar import MtsSchedule
from ..climate import MtsClimate
from ..merossclient import const as mc
from ..sensor import MLDiagnosticSensor
from ..helpers import reverse_lookup
from homeassistant.exceptions import Unauthorized

if typing.TYPE_CHECKING:
    from ..meross_device import MerossDevice
    from ..number import MtsTemperatureNumber


class Mts960Climate(MtsClimate):
    """Climate entity for MTS960 devices"""

    manager: "MerossDevice"
    namespace = mc.NS_APPLIANCE_CONTROL_THERMOSTAT_MODEB
    key_namespace = mc.KEY_MODEB
    device_scale = mc.MTS960_TEMP_SCALE

    # default choice to map when any 'non thermostat' mode swapping
    # needs an heating/cooling final choice
    MTS_MODE_DEFAULT = mc.MTS960_MODE_HEAT_COOL

    MTS_WORKING_TO_HVAC_ACTION: dict[int | None, MtsClimate.HVACAction] = {
        mc.MTS960_WORKING_HEAT: MtsClimate.HVACAction.HEATING,
        mc.MTS960_WORKING_COOL: MtsClimate.HVACAction.COOLING,
    }
    MTS_WORKING_TO_HVAC_MODE: dict[int | None, MtsClimate.HVACMode] = {
        mc.MTS960_WORKING_HEAT: MtsClimate.HVACMode.HEAT,
        mc.MTS960_WORKING_COOL: MtsClimate.HVACMode.COOL,
    }
    MTS_MODE_TO_HVAC_MODE = {
        mc.MTS960_MODE_HEAT_COOL: lambda mts_working: Mts960Climate.MTS_WORKING_TO_HVAC_MODE.get(
            mts_working, MtsClimate.HVACMode.OFF
        ),
        mc.MTS960_MODE_SCHEDULE: lambda mts_working: MtsClimate.HVACMode.AUTO,
        mc.MTS960_MODE_TIMER: lambda mts_working: MtsClimate.HVACMode.FAN_ONLY,
    }

    HVAC_MODE_TO_MTS_MODE = {
        MtsClimate.HVACMode.OFF: lambda mts_mode: (
            mts_mode if mts_mode is not None else Mts960Climate.MTS_MODE_DEFAULT
        ),
        MtsClimate.HVACMode.HEAT: lambda mts_mode: mc.MTS960_MODE_HEAT_COOL,
        MtsClimate.HVACMode.COOL: lambda mts_mode: mc.MTS960_MODE_HEAT_COOL,
        MtsClimate.HVACMode.AUTO: lambda mts_mode: mc.MTS960_MODE_SCHEDULE,
        MtsClimate.HVACMode.FAN_ONLY: lambda mts_mode: mc.MTS960_MODE_TIMER,
    }

    HVAC_MODE_TO_MTS_WORKING = {
        MtsClimate.HVACMode.OFF: lambda mts_working: mts_working,
        MtsClimate.HVACMode.HEAT: lambda mts_working: mc.MTS960_WORKING_HEAT,
        MtsClimate.HVACMode.COOL: lambda mts_working: mc.MTS960_WORKING_COOL,
        MtsClimate.HVACMode.AUTO: lambda mts_working: mts_working,
        MtsClimate.HVACMode.FAN_ONLY: lambda mts_working: mts_working,
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
        MtsClimate.HVACMode.FAN_ONLY,
        MtsClimate.HVACMode.AUTO,
    ]

    preset_modes = None

    __slots__ = ("_mts_working","_mts_mode_timer","_mts_mode_timer_attributs")

    def __init__(
        self,
        manager: "MerossDevice",
        channel: object,
        adjust_number_class: typing.Type["MtsTemperatureNumber"],
    ):
        manager._push_payload_data_as_array = True
        super().__init__(
            manager,
            channel,
            adjust_number_class,
            None,
            Mts960Schedule,
            mc.MTSCLIMATE_MASK_SENSOR_OUTPUT_POWER_STATE|mc.MTSCLIMATE_MASK_SENSOR_MODE_STATE
        )
        self._mts_working = None
        self._mts_mode_timer = None
        self._mts_mode_timer_attributs = None

    def set_unavailable(self):
        self._mts_working = None
        self._mts_mode_timer = None
        self._mts_mode_timer_attributs = None
        super().set_unavailable()

    def flush_state(self):
        """interface: MtsClimate."""
        if self._mts_onoff == mc.MTS960_STATE_ON:
            self.hvac_mode = self.MTS_MODE_TO_HVAC_MODE.get(self._mts_mode, lambda mts_working: MtsClimate.HVACMode.OFF)(self._mts_working)
            if self._mts_active == mc.MTS960_STATE_ON:
                if self._mts_mode == mc.MTS960_MODE_TIMER:
                    self.hvac_action = MtsClimate.HVACAction.FAN
                else:
                    self.hvac_action = self.MTS_WORKING_TO_HVAC_ACTION.get(self._mts_working, MtsClimate.HVACAction.OFF)
                self._update_output_power_state(True)
            else:
                self.hvac_action = MtsClimate.HVACAction.IDLE
                self._update_output_power_state(False)
            ModeStat=mc.SensorModeStateEnum.UNKNOW
            ModeStatAttributs=None
            if self._mts_mode == mc.MTS960_MODE_TIMER:
                if self._mts_mode_timer is not None and self._mts_mode_timer != mc.SensorModeStateEnum.UNKNOW:
                    ModeStat=self._mts_mode_timer
                    ModeStatAttributs=self._mts_mode_timer_attributs
                else:
                    ModeStat=None
            else:
                self._mts_mode_timer=mc.SensorModeStateEnum.UNKNOW
                self._mts_mode_timer_attributs=None
                if self._mts_mode == mc.MTS960_MODE_HEAT_COOL:
                    if self._mts_working == mc.MTS960_WORKING_HEAT:
                        ModeStat=mc.SensorModeStateEnum.HEATING
                    elif self._mts_working == mc.MTS960_WORKING_COOL:
                        ModeStat=mc.SensorModeStateEnum.COOLING
                elif self._mts_mode == mc.MTS960_MODE_SCHEDULE:
                    if self._mts_working == mc.MTS960_WORKING_HEAT:
                        ModeStat=mc.SensorModeStateEnum.SCHEDULING_HEAT
                    elif self._mts_working == mc.MTS960_WORKING_COOL:
                        ModeStat=mc.SensorModeStateEnum.SCHEDULING_COOL
            if ModeStat is not None:
                self._update_mode_state(ModeStat,ModeStatAttributs)
        else:
            self.hvac_mode = MtsClimate.HVACMode.OFF
            self.hvac_action = MtsClimate.HVACAction.OFF
            self._update_output_power_state(False)
            self._update_mode_state(mc.SensorModeStateEnum.OFF)


        super().flush_state()

    async def async_set_preset_mode(self, preset_mode: str):
        await self.async_request_mode(
            self._mts_mode, reverse_lookup(self.MTS_WORKING_TO_PRESET_MAP, preset_mode)
        )

    async def async_set_hvac_mode(self, hvac_mode: MtsClimate.HVACMode):
        # here special handling is applied to hvac_mode == AUTO,
        # trying to preserve the previous mts_mode if it was already
        # among the AUTO(s) else mapping to a 'closest' one (see the lambdas
        # in HVAC_MODE_TO_MTS_MODE).
        if hvac_mode == MtsClimate.HVACMode.FAN_ONLY:
           raise Unauthorized
        elif hvac_mode == MtsClimate.HVACMode.OFF:
            await self.async_request_onoff(0)
            return

        mode = self.HVAC_MODE_TO_MTS_MODE[hvac_mode](self._mts_mode)
        if mode == mc.MTS960_MODE_HEAT_COOL:
            await self._async_request_modeb(
                {
                    mc.KEY_CHANNEL: self.channel,
                    mc.KEY_ONOFF: mc.MTS960_ONOFF_ON,
                    mc.KEY_MODE: mode,
                    mc.KEY_WORKING: self.HVAC_MODE_TO_MTS_WORKING.get(hvac_mode, lambda mts_working: mts_working)(self._mts_working),
                }
            )
        else:
            await self._async_request_modeb(
                {
                    mc.KEY_CHANNEL: self.channel,
                    mc.KEY_ONOFF: mc.MTS960_ONOFF_ON,
                    mc.KEY_MODE: mode,
                }
            )

    async def async_set_temperature(self, **kwargs):
        await self._async_request_modeb(
            {
                mc.KEY_CHANNEL: self.channel,
                mc.KEY_ONOFF: mc.MTS960_ONOFF_ON,
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
        return self._mts_onoff and self._mts_mode == mc.MTS960_MODE_SCHEDULE

    # interface: self
    async def _async_request_modeb(self, p_modeb: dict):
        if response := await self.manager.async_request_ack(
            mc.NS_APPLIANCE_CONTROL_THERMOSTAT_MODEB,
            mc.METHOD_SET,
            {mc.KEY_MODEB: [p_modeb]},
        ):
            try:
                payload = response[mc.KEY_PAYLOAD][mc.KEY_MODEB]
                if isinstance(payload, list):
                    if payload:
                        self._parse_modeB(payload[0])
                    else:
                        self._parse_modeB(self._mts_payload | p_modeb)
                elif isinstance(payload, dict):
                    self._parse_modeB(self._mts_payload | p_modeb | payload)
            except (KeyError, IndexError):
                # optimistic update
                self._parse_modeB(self._mts_payload | p_modeb)

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

    # message handlers
    def _parse_timer(self, payload: dict):
        """
       {'channel': 0, 'type': 1, 'down': {'duration': 1, 'end': 1718724107, 'onoff': 2}} ==> Count down Off
       {'channel': 0, 'type': 1, 'down': {'duration': 1, 'end': 1718724107, 'onoff': 1}} ==> Count down On
       {'channel': 0, 'type': 2, 'cycle': {'offDuration': 15, 'state': 1, 'end': 1718725103, 'onDuration': 15} } ==> cycle Current On
       {'channel': 0, 'type': 2, 'cycle': {'offDuration': 15, 'state': 2, 'end': 1718725103, 'onDuration': 15} } ==> cycle Current Off
        """
        new_mts_mode_timer=mc.SensorModeStateEnum.UNKNOW
        new_mts_mode_timer_attr=None
        if payload[mc.KEY_TYPE] == mc.MTS960_TIMER_TYPE_COUNTDOWN:
            if payload[mc.KEY_DOWN][mc.KEY_ONOFF] == mc.MTS960_ONOFF_ON:
                new_mts_mode_timer=mc.SensorModeStateEnum.TIMER_COUNTDOWN_ON
            if payload[mc.KEY_DOWN][mc.KEY_ONOFF] == mc.MTS960_ONOFF_OFF:
                new_mts_mode_timer=mc.SensorModeStateEnum.TIMER_COUNTDOWN_OFF
            if new_mts_mode_timer!=mc.SensorModeStateEnum.UNKNOW:
                new_mts_mode_timer_attr= { "Timer Type": new_mts_mode_timer.value,
                                           "Duration": f"{payload[mc.KEY_DOWN][mc.KEY_DURATION]} min",
                                           "End": self.manager.get_device_datetime(payload[mc.KEY_DOWN][mc.KEY_END])}
        elif payload[mc.KEY_TYPE] == mc.MTS960_TIMER_TYPE_CYCLE:
            new_mts_mode_timer=mc.SensorModeStateEnum.TIMER_CYCLE
            new_mts_mode_timer_attr= { "Timer Type": new_mts_mode_timer.value,
                                       "Current Output Power": "On" if payload[mc.KEY_CYCLE][mc.KEY_STATE] == mc.MTS960_ONOFF_ON else "Off",
                                       "Duration On": f"{payload[mc.KEY_CYCLE][mc.KEY_ONDURATION]} min",
                                       "Duration Off": f"{payload[mc.KEY_CYCLE][mc.KEY_OFFDURATION]} min",
                                       "Next Cycle": self.manager.get_device_datetime(payload[mc.KEY_CYCLE][mc.KEY_END])}
        if new_mts_mode_timer != self._mts_mode_timer or self._mts_mode_timer_attributs != new_mts_mode_timer_attr:
            self._mts_mode_timer=new_mts_mode_timer
            self._mts_mode_timer_attributs=new_mts_mode_timer_attr
            self.flush_state()


    def _parse_modeB(self, payload: dict):
        """
    if new_mts_mode_timer != self._mts_mode_timer:
        self._mts_mode_timer=new_mts_mode_timer
        self.flush_state()

        {
            "channel": 0,
        if new_mts_mode_timer != self._mts_mode_timer:
            self._mts_mode_timer=new_mts_mode_timer
            self.flush_state()

            "mode": 2,
            "targetTemp": 2000,
        if new_mts_mode_timer != self._mts_mode_timer:
            self._mts_mode_timer=new_mts_mode_timer
            self.flush_state()

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
        if self._mts_payload == payload:
            return
        self._mts_payload = payload
        if mc.KEY_MODE in payload:
            self._mts_mode = payload[mc.KEY_MODE]
        if mc.KEY_ONOFF in payload:
            self._mts_onoff = payload[mc.KEY_ONOFF]
        if mc.KEY_STATE in payload:
            self._mts_active = payload[mc.KEY_STATE]
        if mc.KEY_WORKING in payload:
            self._mts_working = payload[mc.KEY_WORKING]
        if mc.KEY_CURRENTTEMP in payload:
            self._update_current_temperature(
                payload[mc.KEY_CURRENTTEMP] / self.device_scale
            )
        if mc.KEY_TARGETTEMP in payload:
            self.target_temperature = (payload[mc.KEY_TARGETTEMP] / self.device_scale) if self._mts_mode != mc.MTS960_MODE_TIMER else None

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
        super().__init__(climate, manage_key_section=True)
