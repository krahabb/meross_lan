from typing import TYPE_CHECKING, override

from ...binary_sensor import MLBinarySensor
from ...climate import MtsClimate
from ...helpers.entity import MEListChannelMixin
from ...helpers.namespaces import POLLING_STRATEGY_CONF, NamespaceHandler, mc, mlc, mn
from ...merossclient.protocol.namespaces import thermostat as mn_t
from ...number import MLConfigNumber
from ...select import MLConfigSelect
from ...sensor import MLEnumSensor, MLTemperatureSensor
from ...switch import MLSwitch

if TYPE_CHECKING:
    from typing import Any, Callable, ClassVar, Final, Unpack

    from ...calendar import MtsSchedule
    from ...helpers.device import Device
    from ...merossclient.protocol.types import thermostat as mt_t


class MtsWarningSensor(MLEnumSensor):

    __slots__ = ("translation_key",)

    def __init__(
        self,
        number_temperature: "MtsCommonTemperatureExtNumber",
        native_value: str | int | float | None,
        /,
    ):
        entitykey = f"{number_temperature.entitykey}_warning"
        super().__init__(
            number_temperature.manager,
            number_temperature.channel,
            entitykey,
            native_value=native_value,
            translation_key=f"mts_{entitykey}",
        )


class MtsConfigSwitch(MEListChannelMixin, MLSwitch):

    number_temperature: "MtsCommonTemperatureExtNumber"

    # HA core entity attributes:
    entity_category = MLSwitch.EntityCategory.CONFIG

    __slot__ = ("number_temperature",)

    def __init__(
        self, number_temperature: "MtsCommonTemperatureExtNumber", device_value, /
    ):
        self.number_temperature = number_temperature
        self.ns = number_temperature.ns
        super().__init__(
            number_temperature.manager,
            number_temperature.channel,
            f"{number_temperature.entitykey}_switch",
            MLSwitch.DeviceClass.SWITCH,
            device_value=device_value,
            name=(f"{number_temperature.entitykey} Alarm").capitalize(),
        )

    async def async_shutdown(self):
        await super().async_shutdown()
        self.number_temperature = None  # type: ignore

    def update_onoff(self, onoff: bool, /):
        if self.is_on != onoff:
            self.is_on = onoff
            self.flush_state()
            number_temperature = self.number_temperature
            if number_temperature.available != onoff:
                number_temperature.available = onoff
                number_temperature.flush_state()


class MtsCommonTemperatureNumber(MLConfigNumber):

    if TYPE_CHECKING:
        manager: Device
        entitykey: str

    key_value = mc.KEY_VALUE

    __slots__ = (
        "native_max_value",
        "native_min_value",
        "native_step",
    )

    def __init__(
        self,
        climate: "MtsThermostatClimate",
        device_class: MLConfigNumber.DeviceClass = MLConfigNumber.DeviceClass.TEMPERATURE,
        /,
    ):
        super().__init__(
            climate.manager,
            climate.channel,
            self.__class__.ns.slug_end,
            device_class,
            device_scale=climate.device_scale,
        )
        self.manager.register_parser_entity(self)

    def _parse(self, payload: "mt_t.CommonTemperature_C", /):
        try:
            self.native_max_value = payload[mc.KEY_MAX] / self.device_scale
            self.native_min_value = payload[mc.KEY_MIN] / self.device_scale
        except KeyError as e:
            self.log_exception(self.DEBUG, e, "_parse", timeout=14400)
        self.update_device_value(payload[self.key_value])


class MtsCommonTemperatureExtNumber(MtsCommonTemperatureNumber):

    if TYPE_CHECKING:
        sensor_warning: MtsWarningSensor
        switch: MtsConfigSwitch

    __slots__ = (
        "sensor_warning",
        "switch",
    )

    def __init__(self, climate: "MtsThermostatClimate", /):
        MtsCommonTemperatureNumber.__init__(self, climate)
        manager = self.manager
        # preset entity platforms since these might be instantiated later
        manager.platforms.setdefault(MtsConfigSwitch.PLATFORM)
        manager.platforms.setdefault(MLEnumSensor.PLATFORM)
        self.sensor_warning = None  # type: ignore
        self.switch = None  # type: ignore

    async def async_shutdown(self):
        await super().async_shutdown()
        self.switch = None  # type: ignore
        self.sensor_warning = None  # type: ignore

    def _parse(self, payload: "mt_t.CommonTemperatureExt_C", /):
        try:
            warning = payload[mc.KEY_WARNING]
            self.sensor_warning.update_native_value(warning)
        except AttributeError:
            self.sensor_warning = MtsWarningSensor(self, warning)
        except KeyError:
            pass
        try:
            self.available = bool(payload[mc.KEY_ONOFF])
            self.switch.update_onoff(self.available)
        except AttributeError:
            self.switch = MtsConfigSwitch(self, self.available)
        except KeyError:
            pass
        super()._parse(payload)


class MtsDeadZoneNumber(MtsCommonTemperatureNumber):
    """
    adjust "dead zone" i.e. the threshold for the temperature control
    for mts200 and mts960 or whatever carries the Appliance.Control.Thermostat.DeadZone
    The min/max values are different between the two devices but the deadZone
    payload will carry the values and so set them
    """

    ns = mn_t.Appliance_Control_Thermostat_DeadZone

    def __init__(self, climate: "MtsThermostatClimate", /):
        self.native_max_value = 3.5
        self.native_min_value = 0.5
        self.native_step = 0.1
        super().__init__(climate, MLConfigNumber.DEVICE_CLASS_TEMPERATURE_DELTA)


class MtsFrostNumber(MtsCommonTemperatureExtNumber):

    ns = mn_t.Appliance_Control_Thermostat_Frost

    def __init__(self, climate: "MtsThermostatClimate", /):
        self.native_max_value = 15
        self.native_min_value = 5
        self.native_step = climate.target_temperature_step
        MtsCommonTemperatureExtNumber.__init__(self, climate)


class MtsOverheatNumber(MtsCommonTemperatureExtNumber):

    if TYPE_CHECKING:
        sensor_external_temperature: MLTemperatureSensor

    ns = mn_t.Appliance_Control_Thermostat_Overheat

    __slots__ = ("sensor_external_temperature",)

    def __init__(self, climate: "MtsThermostatClimate", /):
        self.native_max_value = 70
        self.native_min_value = 20
        self.native_step = climate.target_temperature_step
        MtsCommonTemperatureExtNumber.__init__(self, climate)

    async def async_shutdown(self):
        await super().async_shutdown()
        self.sensor_external_temperature = None  # type: ignore

    def _parse(self, payload: "mt_t.Overheat_C", /):
        try:
            current_temp = payload[mc.KEY_CURRENTTEMP]
            self.sensor_external_temperature.update_device_value(current_temp)
        except AttributeError:
            self.sensor_external_temperature = MLTemperatureSensor(
                self.manager,
                self.channel,
                "external sensor",
                device_value=current_temp,
                device_scale=self.device_scale,
            )
        except KeyError:
            pass
        super()._parse(payload)


class MtsWindowOpened(MLBinarySensor):
    # Specialized binary sensor for Thermostat.WindowOpened entity used in Mts200-Mts960(maybe).

    ns = mn_t.Appliance_Control_Thermostat_WindowOpened
    key_value = mc.KEY_STATUS

    def __init__(self, climate: "MtsThermostatClimate", /):
        super().__init__(
            climate.manager,
            climate.channel,
            mc.KEY_WINDOWOPENED,
            MLBinarySensor.DeviceClass.WINDOW,
        )
        climate.manager.register_parser_entity(self)


class MtsExternalSensorSwitch(MEListChannelMixin, MLSwitch):
    # External sensor mode: use internal(0) vs external(1) sensor as temperature loopback.

    ns = mn_t.Appliance_Control_Thermostat_Sensor
    key_value = mc.KEY_MODE

    # HA core entity attributes:
    entity_category = MLSwitch.EntityCategory.CONFIG

    def __init__(self, climate: "MtsThermostatClimate", /):
        super().__init__(
            climate.manager,
            climate.channel,
            "external sensor mode",
            MLSwitch.DeviceClass.SWITCH,
        )
        climate.manager.register_parser_entity(self)


class MtsHoldAction(MLConfigSelect):

    if TYPE_CHECKING:
        manager: "Device"
        number_time: MLConfigNumber

    ns = mn_t.Appliance_Control_Thermostat_HoldAction
    key_value = mc.KEY_MODE

    OPTIONS_MAP = {
        mc.MTS_HOLDACTION_PERMANENT: "permanent",
        mc.MTS_HOLDACTION_NEXT_SCHEDULE: "next_schedule",
        mc.MTS_HOLDACTION_TIMER: "timer",
    }

    __slots__ = ("number_time",)

    def __init__(self, climate: "MtsThermostatClimate", /):
        super().__init__(climate.manager, climate.channel, "hold_action")
        climate.manager.register_parser_entity(self)
        self.number_time = MLConfigNumber(
            climate.manager,
            climate.channel,
            "hold_action_time",
            MLConfigNumber.DEVICE_CLASS_DURATION,
            device_scale=1,
            native_unit_of_measurement=MLConfigNumber.hac.UnitOfTime.MINUTES,
        )
        self.number_time.async_request_value = self._async_request_value_number_time

    async def async_shutdown(self):
        await super().async_shutdown()
        self.number_time = None  # type: ignore

    @override
    async def async_request_value(self, device_value, /):
        return await self._async_request_holdAction(
            device_value, self.number_time.device_value or 0
        )

    # interface: self
    def _parse_holdAction(self, payload: "mt_t.HoldAction_C", /):
        self.update_device_value(payload[mc.KEY_MODE])
        try:
            _time = payload[mc.KEY_TIME]  # type: ignore
            self.number_time.update_device_value(_time)
        except KeyError:
            pass

    async def _async_request_holdAction(self, mode, time, /):
        ns = self.ns
        return await self.manager.async_request_ack(
            ns.name,
            mc.METHOD_SET,
            {
                ns.key: [
                    {
                        ns.key_channel: self.channel,
                        mc.KEY_MODE: mode,
                        mc.KEY_TIME: time,
                    }
                ]
            },
        )

    async def _async_request_value_number_time(self, device_value, /):
        if response := await self._async_request_holdAction(
            mc.MTS_HOLDACTION_TIMER, device_value
        ):
            self.update_device_value(mc.MTS_HOLDACTION_TIMER)
        return response


class MtsTempUnit(MEListChannelMixin, MLConfigSelect):

    ns = mn.Appliance_Control_TempUnit
    key_value = mc.KEY_TEMPUNIT

    OPTIONS_MAP = {
        mc.TEMPUNIT_CELSIUS: MLConfigSelect.hac.UnitOfTemperature.CELSIUS,
        mc.TEMPUNIT_FAHRENHEIT: MLConfigSelect.hac.UnitOfTemperature.FAHRENHEIT,
    }

    manager: "Device"

    def __init__(self, climate: "MtsThermostatClimate", /):
        super().__init__(climate.manager, climate.channel, "display_temperature_unit")
        climate.manager.register_parser_entity(self)


class MLScreenBrightnessNumber(MLConfigNumber):
    manager: "Device"

    ns = mn.Appliance_Control_Screen_Brightness

    # HA core entity attributes:
    icon: str = "mdi:brightness-percent"
    native_max_value = 100
    native_min_value = 0
    native_step = 12.5

    def __init__(self, manager: "Device", key: str, /):
        self.key_value = key
        super().__init__(
            manager,
            0,
            f"screenbrightness_{key}",
            native_unit_of_measurement=MLConfigNumber.hac.PERCENTAGE,
            name=f"Screen brightness ({key})",
        )

    async def async_set_native_value(self, value: float, /):
        """Override base async_set_native_value since it would round
        the value to an int (common device native type)."""
        if await self.async_request_value(value):
            self.update_device_value(value)


OPTIONAL_NAMESPACES_INITIALIZERS: set["mn.Namespace"] = {
    mn_t.Appliance_Control_Thermostat_CtlRange,  # mts960
    mn_t.Appliance_Control_Thermostat_SummerMode,  # mts200
    mn_t.Appliance_Control_Thermostat_System,  # mts300
    mn_t.Appliance_Control_Thermostat_Timer,  # mts960
    mn.Appliance_Config_Sensor_Association,  # mts300
}
"""These namespaces handlers will forward message parsing to the climate entity"""

OPTIONAL_ENTITIES_INITIALIZERS: dict[str, "Callable[[MtsThermostatClimate], Any]"] = {
    mn.Appliance_Control_TempUnit.name: MtsTempUnit,
    mn_t.Appliance_Control_Thermostat_DeadZone.name: MtsDeadZoneNumber,
    mn_t.Appliance_Control_Thermostat_Frost.name: MtsFrostNumber,
    mn_t.Appliance_Control_Thermostat_HoldAction.name: MtsHoldAction,
    mn_t.Appliance_Control_Thermostat_Overheat.name: MtsOverheatNumber,
    mn_t.Appliance_Control_Thermostat_Sensor.name: MtsExternalSensorSwitch,
    mn_t.Appliance_Control_Thermostat_WindowOpened.name: MtsWindowOpened,
}
"""Additional entities (linked to the climate one) in case their ns is supported/available"""


class MtsThermostatClimate(MtsClimate):
    """
    Thin base class for device thermostats i.e. mtsXXXX line of devices (i.e. no hub subdevs).
    These could share a common layer based on behaviors from Appliance.Control.Thermostat.*
    namespaces.
    """

    if TYPE_CHECKING:
        manager: Final[Device]  # type: ignore
        channel: Final[int]  # type: ignore

    class AdjustNumber(MtsCommonTemperatureNumber):
        """
        Adjust temperature readings for mts200 and mts960.
        Manages Appliance.Control.Thermostat.Calibration:
        {"channel":0,"value":0 "min":-80,"max":80,"lmTime":1697010767} - mts200
        {"channel":0,"value":-270,"min":-2000,"max":2000} - mts960
        """

        ns = mn_t.Appliance_Control_Thermostat_Calibration

        def __init__(self, climate: "MtsThermostatClimate", /):
            self.native_max_value = 8
            self.native_min_value = -8
            self.native_step = 0.1
            super().__init__(climate, MLConfigNumber.DEVICE_CLASS_TEMPERATURE_DELTA)

    def __init__(self, manager: "Device", channel, /):
        super().__init__(manager, channel)
        manager.register_parser_entity(self)
        manager.register_parser_entity(self.schedule)
        ability = manager.descriptor.ability
        for optional_ns in OPTIONAL_NAMESPACES_INITIALIZERS:
            if optional_ns.name in ability:
                manager.register_parser(self, optional_ns)

        for namespace, entity_class in OPTIONAL_ENTITIES_INITIALIZERS.items():
            if namespace in ability:
                entity_class(self)

    def get_ns_adjust(self, /):
        return self.manager.namespace_handlers[
            mn_t.Appliance_Control_Thermostat_Calibration.name
        ]

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
        self.max_temp = payload[mc.KEY_CTLMAX] / self.device_scale
        self.min_temp = payload[mc.KEY_CTLMIN] / self.device_scale

    def _parse_summerMode(self, payload: dict, /):
        # needed to silently support registering OPTIONAL_NAMESPACES_INITIALIZERS
        pass

    def _parse_system(self, payload: dict, /):
        # needed to silently support registering OPTIONAL_NAMESPACES_INITIALIZERS
        pass

    def _parse_timer(self, payload: dict, /):
        # needed to silently support registering OPTIONAL_NAMESPACES_INITIALIZERS
        pass

    def _parse_association(self, payload: dict, /):
        # needed to silently support registering OPTIONAL_NAMESPACES_INITIALIZERS
        pass


POLLING_STRATEGY_CONF |= {
    mn.Appliance_Control_TempUnit: (
        mlc.PARAM_CONFIG_UPDATE_PERIOD,
        mlc.PARAM_CLOUDMQTT_UPDATE_PERIOD,
        mlc.PARAM_HEADER_SIZE,
        30,
        NamespaceHandler.async_poll_smart,
    ),
    mn_t.Appliance_Control_Thermostat_Calibration: (
        mlc.PARAM_CONFIG_UPDATE_PERIOD,
        mlc.PARAM_CLOUDMQTT_UPDATE_PERIOD,
        mlc.PARAM_HEADER_SIZE,
        80,
        NamespaceHandler.async_poll_smart,
    ),
    mn_t.Appliance_Control_Thermostat_CtlRange: (
        0,
        0,
        mlc.PARAM_HEADER_SIZE,
        80,
        NamespaceHandler.async_poll_once,
    ),
    mn_t.Appliance_Control_Thermostat_DeadZone: (
        mlc.PARAM_CONFIG_UPDATE_PERIOD,
        mlc.PARAM_CLOUDMQTT_UPDATE_PERIOD,
        mlc.PARAM_HEADER_SIZE,
        80,
        NamespaceHandler.async_poll_smart,
    ),
    mn_t.Appliance_Control_Thermostat_Frost: (
        mlc.PARAM_SENSOR_SLOW_UPDATE_PERIOD,
        mlc.PARAM_SENSOR_SLOW_UPDATE_CLOUD_PERIOD,
        mlc.PARAM_HEADER_SIZE,
        80,
        NamespaceHandler.async_poll_smart,
    ),
    mn_t.Appliance_Control_Thermostat_HoldAction: (
        mlc.PARAM_CONFIG_UPDATE_PERIOD,
        mlc.PARAM_CLOUDMQTT_UPDATE_PERIOD,
        mlc.PARAM_HEADER_SIZE,
        30,
        NamespaceHandler.async_poll_smart,
    ),
    mn_t.Appliance_Control_Thermostat_ModeC: (
        0,
        0,
        mlc.PARAM_HEADER_SIZE,
        120,
        NamespaceHandler.async_poll_default,
    ),
    mn_t.Appliance_Control_Thermostat_Overheat: (
        mlc.PARAM_SENSOR_SLOW_UPDATE_PERIOD,
        mlc.PARAM_SENSOR_SLOW_UPDATE_CLOUD_PERIOD,
        mlc.PARAM_HEADER_SIZE,
        140,
        NamespaceHandler.async_poll_smart,
    ),
    mn_t.Appliance_Control_Thermostat_Timer: (
        0,
        0,
        mlc.PARAM_HEADER_SIZE,
        550,
        NamespaceHandler.async_poll_default,
    ),
    mn_t.Appliance_Control_Thermostat_Schedule: (
        mlc.PARAM_CONFIG_UPDATE_PERIOD,
        mlc.PARAM_CLOUDMQTT_UPDATE_PERIOD,
        mlc.PARAM_HEADER_SIZE,
        550,
        NamespaceHandler.async_poll_smart,
    ),
    mn_t.Appliance_Control_Thermostat_ScheduleB: (
        mlc.PARAM_CONFIG_UPDATE_PERIOD,
        mlc.PARAM_CLOUDMQTT_UPDATE_PERIOD,
        mlc.PARAM_HEADER_SIZE,
        550,
        NamespaceHandler.async_poll_smart,
    ),
    mn_t.Appliance_Control_Thermostat_Sensor: (
        mlc.PARAM_SENSOR_SLOW_UPDATE_PERIOD,
        mlc.PARAM_SENSOR_SLOW_UPDATE_CLOUD_PERIOD,
        mlc.PARAM_HEADER_SIZE,
        40,
        NamespaceHandler.async_poll_smart,
    ),
}
