from typing import TYPE_CHECKING, override

from ...binary_sensor import BinarySensorParser
from ...climate import MtsClimate
from ...helpers.namespaces import NamespaceHandler, mc, mlc, mn
from ...merossclient.protocol.namespaces import thermostat as mn_t
from ...number import NumberParser
from ...select import SelectParser
from ...sensor import EnumParser, SensorParser
from ...switch import SwitchParser

if TYPE_CHECKING:
    from typing import Any, Callable, ClassVar, Final, Unpack

    from ...helpers.device import Device
    from ...helpers.entity import ParserEntity
    from ...merossclient.device.handler import NamespaceHandler as _NamespaceHandler
    from ...merossclient.protocol import types as mt
    from ...merossclient.protocol.namespaces import Namespace
    from ...merossclient.protocol.types import JsonDict


class ScreenBrightnessNumber(NumberParser):

    init_device_scale = 1.0
    # HA core entity attributes:
    _attr_icon = "mdi:brightness-percent"
    _attr_native_unit_of_measurement = mlc.hac.PERCENTAGE
    _attr_native_max_value = 100
    _attr_native_min_value = 0
    _attr_native_step = 12.5


class ScreenBrightnessNamespaceHandler(NamespaceHandler):
    """
    This ns only appears with thermostats so far.. so we put it here but it could
    nevertheless live in its own module (or in 'misc' maybe)
    """

    POLLING_CONFIG_DEFAULT = NamespaceHandler.POLLING_CONFIG_CONFIGURATION

    def __init__(self, ns: "mn.Namespace", device: "Device", /):
        NamespaceHandler.__init__(self, ns, device)
        index = mn.IndexType.channel(0)
        self.register_parsers(
            *(
                ScreenBrightnessNumber(
                    0,
                    device,
                    entity_key=f"screenbrightness_{key}",
                    name=f"Screen brightness ({key})",
                    ns=ns,
                    index=index,
                    key_value=key,
                )
                for key in (mc.KEY_OPERATION, mc.KEY_STANDBY)
            ),
        )


class MtsCommonTemperatureNumber(NumberParser):

    if TYPE_CHECKING:
        parent: Final[Device]  # type: ignore[override]

    init_key_value = mc.KEY_VALUE

    _attr_device_class = NumberParser.DeviceClass.TEMPERATURE

    def __init__(self, id, device: "Device", /, **kwargs):
        NumberParser.__init__(
            self,
            id,
            device,
            **kwargs,
            entity_key=kwargs["ns"].slug_end,  # TODO: generalize entity_key defaulting?
            device_scale=device.entities[id].temperature_scale,  # type: ignore (access MtsThermostatClimate.temperature_scale)
        )

    @override
    def _parse(self, payload: "mt.thermostat.CommonTemperature_C", /):
        try:
            self.native_max_value = payload[mc.KEY_MAX] / self.device_scale
            self.native_min_value = payload[mc.KEY_MIN] / self.device_scale
        except KeyError as e:
            self.log_exception(self.DEBUG, e, "_parse", timeout=14400)
        self.update_device_value(payload[self.key_value])


class MtsCommonTemperatureExtNumber(MtsCommonTemperatureNumber):

    if TYPE_CHECKING:
        sensor_warning: EnumParser
        switch: SwitchParser

    __slots__ = (
        "sensor_warning",
        "switch",
    )

    @override
    def _parse(self, payload: "mt.thermostat.CommonTemperatureExt_C", /):
        try:
            warning = payload[mc.KEY_WARNING]
            self.sensor_warning.update_device_value(warning)
        except AttributeError:
            entity_key = f"{self.entity_key}_warning"
            self.sensor_warning = self.parent.add_entity(
                EnumParser(
                    payload[mc.KEY_CHANNEL],
                    self.parent,
                    entity_key=entity_key,
                    key_value=mc.KEY_WARNING,
                    device_value=warning,
                    translation_key=f"mts_{entity_key}",
                )
            )
        except KeyError:
            pass
        try:
            # 'pre' set 'available' here so that _switch_state_callback
            # doesn't flush before this number has been parsed in full
            self.available = bool(payload[mc.KEY_ONOFF])
            self.switch.update_boolean_value(self.available)
        except AttributeError:
            self.switch = self.parent.add_entity(
                SwitchParser(
                    payload[mc.KEY_CHANNEL],
                    self.parent,
                    entity_key=f"{self.entity_key}_switch",
                    ns=self.ns,
                    index=self.index,
                    is_on=self.available,
                    name=(f"{self.entity_key} Alarm").capitalize(),
                )
            )
            self.switch.register_state_callback(self._switch_state_callback)
        except KeyError:
            pass
        MtsCommonTemperatureNumber._parse(self, payload)

    def _switch_state_callback(self):
        available = bool(self.switch.is_on)
        if self.available != available:
            self.available = available
            self.flush_state()


class MtsDeadZoneNumber(MtsCommonTemperatureNumber):
    """
    adjust "dead zone" i.e. the threshold for the temperature control
    for mts200 and mts960 or whatever carries the Appliance.Control.Thermostat.DeadZone
    The min/max values are different between the two devices but the deadZone
    payload will carry the values and so set them
    """

    _attr_device_class = NumberParser.DEVICE_CLASS_TEMPERATURE_DELTA
    _attr_native_max_value = 3.5
    _attr_native_min_value = 0.5
    _attr_native_step = 0.1


class MtsFrostNumber(MtsCommonTemperatureExtNumber):

    _attr_native_max_value = 15
    _attr_native_min_value = 5
    _attr_native_step = MtsClimate.TARGET_TEMPERATURE_STEP


class MtsOverheatNumber(MtsCommonTemperatureExtNumber):

    if TYPE_CHECKING:
        sensor_external_temperature: SensorParser

    __slots__ = ("sensor_external_temperature",)

    _attr_native_max_value = 70
    _attr_native_min_value = 20
    _attr_native_step = MtsClimate.TARGET_TEMPERATURE_STEP

    @override
    def _parse(self, payload: "mt.thermostat.Overheat_C", /):
        try:
            current_temp = payload[mc.KEY_CURRENTTEMP]
            self.sensor_external_temperature.update_device_value(current_temp)
        except AttributeError:
            self.sensor_external_temperature = self.parent.add_entity(
                SensorParser.Temperature(
                    payload[mc.KEY_CHANNEL],
                    self.parent,
                    entity_key="external sensor",
                    device_value=current_temp,
                    device_scale=self.device_scale,
                )
            )
        except KeyError:
            pass
        MtsCommonTemperatureExtNumber._parse(self, payload)


class MtsSummerMode(SwitchParser):
    """
    Summer mode switch for mts200.
    This is a simple on/off switch but the device expects it to be sent as part of the Mode namespace
    so we need a custom parser to manage it.
    """

    init_key_value = mc.KEY_MODE
    init_value_on = mc.MTS200_SUMMERMODE_COOL
    init_value_off = mc.MTS200_SUMMERMODE_HEAT
    init_entity_key = (
        f"{mn_t.Appliance_Control_Thermostat_SummerMode.slug}__{init_key_value}"
    )

    _attr_name = "Summer mode"

    @override
    def flush_state(self):
        SwitchParser.flush_state(self)
        climate: "MtsThermostatClimate" = self.parent.entities[self.index.value]  # type: ignore
        if self.is_on:
            climate.hvac_modes = [
                MtsThermostatClimate.HVACMode.OFF,
                MtsThermostatClimate.HVACMode.COOL,
            ]
        else:
            climate.hvac_modes = [
                MtsThermostatClimate.HVACMode.OFF,
                MtsThermostatClimate.HVACMode.HEAT,
            ]
        climate.flush_state()


class MtsWindowOpened(BinarySensorParser):
    # Specialized binary sensor for Thermostat.WindowOpened entity used in Mts200-Mts960(maybe).

    init_entity_key = mc.KEY_WINDOWOPENED
    init_key_value = mc.KEY_STATUS

    _attr_device_class = BinarySensorParser.DeviceClass.WINDOW


class MtsExternalSensorSwitch(SwitchParser):
    # External sensor mode: use internal(0) vs external(1) sensor as temperature loopback.

    init_entity_key = "external sensor mode"
    init_key_value = mc.KEY_MODE


class MtsHoldAction(SelectParser):

    if TYPE_CHECKING:
        number_time: NumberParser

    init_entity_key = "hold action"
    init_key_value = mc.KEY_MODE
    init_options_map = {
        mc.MTS_HOLDACTION_PERMANENT: "permanent",
        mc.MTS_HOLDACTION_NEXT_SCHEDULE: "next_schedule",
        mc.MTS_HOLDACTION_TIMER: "timer",
    }

    __slots__ = ("number_time",)

    # interface: self
    def _parse(self, payload: "mt.thermostat.HoldAction_C", /):
        self.update_device_value(payload[self.key_value])
        try:
            time = payload[mc.KEY_TIME]  # type: ignore
            self.number_time.update_device_value(time)
        except AttributeError:
            self.number_time = self.parent.add_entity(
                NumberParser(
                    payload[mc.KEY_CHANNEL],
                    self.parent,
                    entity_key="hold_action_time",
                    device_scale=1,
                    device_value=time,
                    device_class=NumberParser.DEVICE_CLASS_DURATION,
                    native_unit_of_measurement=mlc.hac.UnitOfTime.MINUTES,
                )
            )
            self.number_time.async_request_value = self._async_request_value_number_time
        except KeyError:
            pass

    async def _async_request_value_number_time(self, device_value, /):
        await self.async_request_parse(
            {
                self.key_value: mc.MTS_HOLDACTION_TIMER,
                mc.KEY_TIME: device_value,
            }
        )


class MtsTempUnit(SelectParser):

    init_entity_key = "display_temperature_unit"
    init_key_value = mc.KEY_TEMPUNIT
    init_options_map = {
        mc.TEMPUNIT_CELSIUS: mlc.hac.UnitOfTemperature.CELSIUS,
        mc.TEMPUNIT_FAHRENHEIT: mlc.hac.UnitOfTemperature.FAHRENHEIT,
    }


class MtsThermostatClimate(MtsClimate):
    """
    Thin base class for device thermostats i.e. mtsXXXX line of devices (i.e. no hub subdevs).
    These could share a common layer based on behaviors from Appliance.Control.Thermostat.*
    namespaces.
    """

    class AdjustNumber(MtsCommonTemperatureNumber):
        """
        Adjust temperature readings for mts200 and mts960.
        Manages Appliance.Control.Thermostat.Calibration:
        {"channel":0,"value":0 "min":-80,"max":80,"lmTime":1697010767} - mts200
        {"channel":0,"value":-270,"min":-2000,"max":2000} - mts960
        """

        init_ns = mn_t.Appliance_Control_Thermostat_Calibration

        _attr_device_class = NumberParser.DEVICE_CLASS_TEMPERATURE_DELTA
        _attr_native_max_value = 8
        _attr_native_min_value = -8
        _attr_native_step = 0.1


NamespaceHandler.POLLING_CONFIG_MAP.update(
    {
        mn.Appliance_Control_TempUnit: NamespaceHandler.POLLING_CONFIG_CONFIGURATION,
        mn_t.Appliance_Control_Thermostat_Calibration: NamespaceHandler.POLLING_CONFIG_CONFIGURATION,
        mn_t.Appliance_Control_Thermostat_CtlRange: NamespaceHandler.POLLING_CONFIG_ONCE,
        mn_t.Appliance_Control_Thermostat_DeadZone: NamespaceHandler.POLLING_CONFIG_CONFIGURATION,
        mn_t.Appliance_Control_Thermostat_Frost: NamespaceHandler.POLLING_CONFIG_SLOWSENSOR,
        mn_t.Appliance_Control_Thermostat_HoldAction: NamespaceHandler.POLLING_CONFIG_CONFIGURATION,
        mn_t.Appliance_Control_Thermostat_Overheat: NamespaceHandler.POLLING_CONFIG_SLOWSENSOR,
        mn_t.Appliance_Control_Thermostat_Schedule: NamespaceHandler.POLLING_CONFIG_CONFIGURATION,
        mn_t.Appliance_Control_Thermostat_ScheduleB: NamespaceHandler.POLLING_CONFIG_CONFIGURATION,
        mn_t.Appliance_Control_Thermostat_Sensor: NamespaceHandler.POLLING_CONFIG_SLOWSENSOR,
    }
)
