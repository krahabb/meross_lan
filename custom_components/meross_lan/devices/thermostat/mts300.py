from typing import TYPE_CHECKING, override

from homeassistant.components.climate import const as hacc

from . import (
    EnumParser,
    MtsThermostatClimate,
    NumberParser,
    SelectParser,
    SensorParser,
    mc,
    mlc,
    mn,
    mn_t,
)
from ...helpers import reverse_lookup
from ...switch import EmulatedSwitch

if TYPE_CHECKING:
    from typing import ClassVar, Final

    from ...helpers.device import Device
    from ...merossclient.protocol import types as mt


class Mts300Climate(MtsThermostatClimate):
    """Climate entity for MTS300 devices"""

    class AdjustNumber(MtsThermostatClimate.AdjustNumber):

        class AdjustHumidityNumber(NumberParser):
            init_entity_key = "humidity_calibration"
            init_key_value = NumberParser.SimpleKeyValue(mc.KEY_HUMIVALUE)
            init_device_scale = 10

            _attr_device_class = NumberParser.DeviceClass.HUMIDITY
            _attr_native_max_value = 5
            _attr_native_min_value = -5
            _attr_native_step = 0.1

        if TYPE_CHECKING:
            """{"channel":0,"value":150,"min":-450,"max":450,"humiValue":-60}"""
            number_calibration_humi: AdjustHumidityNumber

        _attr_native_max_value = 4.5
        _attr_native_min_value = -4.5
        _attr_native_step = 0.1

        __slots__ = ("number_calibration_humi",)

        def __call__(self, payload: "mt.thermostat.Calibration_C", /):
            try:
                humidity = payload[mc.KEY_HUMIVALUE]  # type: ignore
                self.number_calibration_humi.update_device_value(humidity)
            except AttributeError:
                self.number_calibration_humi = self.__class__.AdjustHumidityNumber(
                    self, ns=self.ns, ns_value=humidity
                )
            except KeyError:  # missing humiValue
                pass
            MtsThermostatClimate.AdjustNumber.__call__(self, payload)

    class SensorAssociationSelect(SelectParser):
        """
        Configures internal/external sensor association for temperature readings in mts300.
        """

        init_ns = mn.Appliance_Config_Sensor_Association
        init_key_value = SelectParser.NestedKeyValue(mc.KEY_TEMP, init_ns.slug_end)
        init_entity_key = f"{init_ns.slug}__{init_key_value}"

        _attr_entity_category = SelectParser.EntityCategory.DIAGNOSTIC
        _attr_name = "Sensor Association"

        """ TODO: get a description of possible options and implement either translations or constant symbols
        so that we can change also the entity category to CONFIG
        """
        init_options_map = {
            2: "Internal sensor",  # almost sure
        }

    if TYPE_CHECKING:
        # overrides
        ns_value: mt.thermostat.ModeC_C

        HVAC_MODE_TO_MODE_MAP: ClassVar
        ENTITY_ARGS: Final[dict[str, EnumParser.Args]]
        _mts_work: int | None

        # HA core entity attributes:
        target_temperature_high: float | None
        target_temperature_low: float | None

        # entities
        sensor_current_humidity: SensorParser
        number_fan_hold: NumberParser
        switch_fan_hold: EmulatedSwitch
        select_temp_association: SensorAssociationSelect

    # MtsClimate class attributes
    temperature_scale = mc.MTS300_TEMP_SCALE
    SCHEDULE_NS = mn_t.Appliance_Control_Thermostat_ScheduleB
    # TODO: customize parsing of native payload since we have 2 temperatures
    MTS_MODE_TO_PRESET_MAP = {
        mc.MTS300_WORK_MANUAL: MtsThermostatClimate.Preset.CUSTOM,
        mc.MTS300_WORK_SCHEDULE: MtsThermostatClimate.Preset.AUTO,
    }

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
    ENTITY_ARGS = {
        "hdStatus": {
            "entity_key": "(de)humidifier_status",
            "translation_key": "mts300_hdstatus",
            "entity_category": EnumParser.EntityCategory.DIAGNOSTIC,
        },
        "hStatus": {
            "entity_key": "heating_status",
            "translation_key": "mts300_status",
            "entity_category": EnumParser.EntityCategory.DIAGNOSTIC,
        },
        "cStatus": {
            "entity_key": "cooling_status",
            "translation_key": "mts300_status",
            "entity_category": EnumParser.EntityCategory.DIAGNOSTIC,
        },
        "fStatus": {
            "entity_key": "fan_speed",
            "translation_key": "mts300_status",
        },
        "aStatus": {
            "entity_key": "auxiliary_status",
            "translation_key": "mts300_status",
            "entity_category": EnumParser.EntityCategory.DIAGNOSTIC,
        },
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
        "sensor_current_humidity",
        "number_fan_hold",
        "switch_fan_hold",
        "select_temp_association",
    ) + tuple(f"sensor_{_key}" for _key in ENTITY_ARGS)

    def __init__(self, id, device: "Device", /, **kwargs):
        MtsThermostatClimate.__init__(self, id, device, **kwargs)
        device.register_parser_ex(
            self,
            mn.Appliance_Config_Sensor_Association,
            mn_t.Appliance_Control_Thermostat_System,
        )
        self.fan_mode = None
        self.fan_modes = self._attr_fan_modes
        self.target_temperature_high = None
        self.target_temperature_low = None
        self._mts_work = None
        for _key, _args in Mts300Climate.ENTITY_ARGS.items():
            setattr(self, f"sensor_{_key}", EnumParser(self, **_args))
        self.sensor_current_humidity = SensorParser(
            self,
            **(SensorParser.HUMIDITY_ARGS | {"entity_registry_enabled_default": False}),
        )

        self.number_fan_hold = NumberParser(
            self,
            entity_key="fan_hold_time",
            ns=self.ns,
            key_value=NumberParser.NestedKeyValue(mc.KEY_FAN, "hTime"),
            device_class=NumberParser.DEVICE_CLASS_DURATION,
            native_unit_of_measurement=mlc.hac.UnitOfTime.MINUTES,
            device_scale=1,
        )
        self.switch_fan_hold = EmulatedSwitch(
            self,
            entity_key="fan_hold_enable",
        )
        self.switch_fan_hold.async_turn_on = self._async_turn_on_switch_fan_hold
        self.switch_fan_hold.async_turn_off = self._async_turn_off_switch_fan_hold

    def shutdown(self):
        MtsThermostatClimate.shutdown(self)
        del self.switch_fan_hold
        del self.number_fan_hold
        for _key in Mts300Climate.ENTITY_ARGS:
            delattr(self, f"sensor_{_key}")

    # interface: MtsClimate
    def set_unavailable(self):
        self.fan_mode = None
        self.target_temperature_high = None
        self.target_temperature_low = None
        self._mts_work = None
        return MtsThermostatClimate.set_unavailable(self)

    async def async_set_hvac_mode(self, hvac_mode: MtsThermostatClimate.HVACMode):
        await self.async_request_parse_ex(
            {mc.KEY_MODE: self.HVAC_MODE_TO_MODE_MAP[hvac_mode]}
        )

    async def async_set_temperature(self, **kwargs):
        format_temp = lambda t: round(t * self.temperature_scale)

        try:
            mode = self.HVAC_MODE_TO_MODE_MAP[kwargs[self.ATTR_HVAC_MODE]]
        except KeyError:
            mode = self._mts_mode
        target_temp = kwargs.get(self.ATTR_TEMPERATURE)
        target_temp_low = kwargs.get(self.ATTR_TARGET_TEMP_LOW)
        target_temp_high = kwargs.get(self.ATTR_TARGET_TEMP_HIGH)

        # Make sure the combination of arguments passed is sane
        if target_temp and mode == MtsThermostatClimate.HVACMode.HEAT_COOL:
            raise ValueError(
                "set_temperature cannot accept a single temperature parameter in 'heat_cool' mode"
            )

        modeC_args = {
            "mode": mode,
            "work": mc.MTS300_WORK_MANUAL,
            "targetTemp": {},
        }

        if mode == mc.MTS300_MODE_HEAT:
            target_temp_low = target_temp_low or target_temp
        if mode == mc.MTS300_MODE_COOL:
            target_temp_high = target_temp_high or target_temp

        if target_temp_high:
            modeC_args["targetTemp"]["cold"] = format_temp(target_temp_high)
        if target_temp_low:
            modeC_args["targetTemp"]["heat"] = format_temp(target_temp_low)

        await self.async_request_parse_ex(modeC_args)

    async def async_set_fan_mode(self, fan_mode: str, /):
        fan_speed = self.FAN_MODE_TO_FAN_SPEED_MAP[fan_mode]
        # actually we assume: (fan_speed != 0) <-> (fMode == mc.MTS300_FAN_MODE_ON)
        await self.async_request_parse_ex(
            {
                mc.KEY_FAN: {
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
    async def async_request_preset(self, mode: int, /):
        # in Mts300 we'll map 'presets' to the 'work' parameter
        await self.async_request_parse_ex({"work": mode})

    @override
    async def async_request_onoff(self, onoff: int, /):
        await self.async_request_parse_ex(
            {
                mc.KEY_MODE: (
                    (self._mts_mode or mc.MTS300_MODE_AUTO)
                    if onoff
                    else mc.MTS300_MODE_OFF
                )
            }
        )

    @override
    def is_mts_scheduled(self, /):
        return self._mts_onoff and self._mts_work == mc.MTS300_WORK_SCHEDULE

    @override
    def __call__(self, payload: "mt.thermostat.ModeC_C", /):
        if self.ns_value == payload:
            return
        self.ns_value = payload
        try:
            self._mts_work = payload["work"]
            self.preset_mode = self.MTS_MODE_TO_PRESET_MAP.get(self._mts_work)
            try:
                # get current input sensor: 2 should be internal sensor though
                temp_association = self.select_temp_association.ns_value
            except AttributeError:
                temp_association = 0
            # currentTemp is always the controlled temperature input and might come from either
            # internal or external sensor based on temp_association. sensorTemp is internal sensor always.
            # It looks like currentTemp is rounded up to 1°C while sensorTemp should be at least 0.5°C resolution.
            # and this should explain https://github.com/krahabb/meross_lan/issues/592
            self._update_current_temperature(
                payload["currentTemp"]
                if temp_association != 2
                else payload["sensorTemp"]
            )

            targetTemp = payload["targetTemp"]
            self.target_temperature_high = targetTemp["cold"] / self.temperature_scale
            self.target_temperature_low = targetTemp["heat"] / self.temperature_scale
            more = payload["more"]
            self.sensor_current_humidity.update_device_value(more["humi"])
            self.current_humidity = self.sensor_current_humidity.native_value
            for _key in Mts300Climate.ENTITY_ARGS:
                getattr(self, f"sensor_{_key}").update_native_value(more[_key])

            fan = payload["fan"]
            self.fan_mode = reverse_lookup(self.FAN_MODE_TO_FAN_SPEED_MAP, fan["speed"])
            fan_hold_time = fan["hTime"]
            if fan_hold_time == mc.MTS300_FAN_HOLD_DISABLED:
                # this doesn't update ns_value so that it is saved and
                # eventually reused when switch_fan_hold toggles on
                self.number_fan_hold.update_native_value(None)
                self.switch_fan_hold.update_boolean_value(False)
            else:
                if not self.number_fan_hold.update_device_value(fan_hold_time):
                    # might happen when we toggle-on switch_fan_hold
                    self.number_fan_hold.update_native_value(fan_hold_time)
                self.switch_fan_hold.update_boolean_value(True)

            match mode := payload["mode"]:
                case mc.MTS300_MODE_OFF:
                    self._mts_onoff = 0
                    # don't set _mts_mode so we remember last one
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

    # interface: self
    def _parse_association(self, payload: dict, /):
        try:
            self.select_temp_association(payload)
        except AttributeError:
            # FIXME: use swap_parser ?
            self.select_temp_association = Mts300Climate.SensorAssociationSelect(self)
            self.select_temp_association(payload)

    def _parse_system(self, payload: dict, /):
        pass

    async def _async_turn_on_switch_fan_hold(self, **kwargs):
        h_time = self.number_fan_hold.ns_value
        await self.async_request_parse_ex(
            {mc.KEY_FAN: {"hTime": 60 if h_time is None else h_time}}
        )

    async def _async_turn_off_switch_fan_hold(self, **kwargs):
        await self.async_request_parse_ex(
            {mc.KEY_FAN: {"hTime": mc.MTS300_FAN_HOLD_DISABLED}}
        )
