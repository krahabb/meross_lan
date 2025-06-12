from typing import TYPE_CHECKING

from ...binary_sensor import MLBinarySensor
from ...climate import MtsClimate, MtsTemperatureNumber
from ...helpers.entity import MEListChannelMixin
from ...helpers.namespaces import POLLING_STRATEGY_CONF, NamespaceHandler, mc, mlc, mn
from ...merossclient.protocol.namespaces import thermostat as mn_t
from ...number import MLConfigNumber
from ...select import MLConfigSelect
from ...sensor import MLEnumSensor, MLTemperatureSensor
from ...switch import MLSwitch

if TYPE_CHECKING:
    from typing import Any, Callable, ClassVar, Unpack

    from ...calendar import MtsSchedule
    from ...helpers.device import Device


class MtsWarningSensor(MLEnumSensor):

    __slots__ = ("translation_key",)

    def __init__(
        self,
        number_temperature: "MtsRichTemperatureNumber",
        native_value: str | int | float | None,
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

    # HA core entity attributes:
    entity_category = MLSwitch.EntityCategory.CONFIG

    __slot__ = ("number_temperature",)

    def __init__(
        self,
        number_temperature: "MtsRichTemperatureNumber",
        device_value,
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
        self.number_temperature: "MtsRichTemperatureNumber" = None  # type: ignore

    def update_onoff(self, onoff):
        if self.is_on != onoff:
            self.is_on = onoff
            self.flush_state()
            self.number_temperature.available = onoff
            self.number_temperature.flush_state()


class MtsRichTemperatureNumber(MtsTemperatureNumber):
    """
    Slightly enriched MtsTemperatureNumber to generalize  a lot of Thermostat namespaces
    which usually carry a temperature value together with some added entities (typically a switch
    to enable the feature and a 'warning sensor')
    typical examples are :
    "calibration": {"channel": 0, "value": 0, "min": -80, "max": 80, "lmTime": 1697010767}
    "deadZone":
    "frost": {"channel": 0, "onoff": 1, "value": 500, "min": 500, "max": 1500, "warning": 0}
    "overheat": {"warning": 0, "value": 335, "onoff": 1, "min": 200, "max": 700,
        "lmTime": 1674121910, "currentTemp": 355, "channel": 0}
    """

    manager: "Device"
    entitykey: str
    key_value = mc.KEY_VALUE

    __slots__ = (
        "sensor_warning",
        "switch",
        "native_max_value",
        "native_min_value",
        "native_step",
    )

    def __init__(
        self,
        climate: "MtsThermostatClimate",
        **kwargs: "Unpack[MtsTemperatureNumber.Args]",
    ):
        super().__init__(climate, self.__class__.ns.key, **kwargs)
        manager = self.manager
        # preset entity platforms since these might be instantiated later
        manager.platforms.setdefault(MtsConfigSwitch.PLATFORM)
        manager.platforms.setdefault(MLEnumSensor.PLATFORM)
        self.sensor_warning: "MtsWarningSensor" = None  # type: ignore
        self.switch: "MtsConfigSwitch" = None  # type: ignore
        manager.register_parser_entity(self)

    async def async_shutdown(self):
        await super().async_shutdown()
        self.switch: "MtsConfigSwitch" = None  # type: ignore
        self.sensor_warning: "MtsWarningSensor" = None  # type: ignore

    def _parse(self, payload: dict):
        """
        {"channel": 0, "value": 0, "min": -80, "max": 80, "lmTime": 1697010767}
        """
        if mc.KEY_MIN in payload:
            self.native_min_value = payload[mc.KEY_MIN] / self.device_scale
        if mc.KEY_MAX in payload:
            self.native_max_value = payload[mc.KEY_MAX] / self.device_scale
        if mc.KEY_ONOFF in payload:
            onoff = payload[mc.KEY_ONOFF]
            try:
                # we don't use 'update_onoff' since it would (double) flush
                # our availability
                switch = self.switch
                if switch.is_on != onoff:
                    switch.is_on = onoff
                    switch.flush_state()
            except AttributeError:
                self.switch = MtsConfigSwitch(self, device_value=onoff)
            self.available = onoff
        self.update_device_value(payload[self.key_value])

        if mc.KEY_WARNING in payload:
            try:
                self.sensor_warning.update_native_value(payload[mc.KEY_WARNING])
            except AttributeError:
                self.sensor_warning = MtsWarningSensor(self, payload[mc.KEY_WARNING])


class MtsDeadZoneNumber(MtsRichTemperatureNumber):
    """
    adjust "dead zone" i.e. the threshold for the temperature control
    for mts200 and mts960 or whatever carries the Appliance.Control.Thermostat.DeadZone
    The min/max values are different between the two devices but the deadZone
    payload will carry the values and so set them
    """

    ns = mn_t.Appliance_Control_Thermostat_DeadZone

    def __init__(self, climate: "MtsThermostatClimate"):
        self.native_max_value = 3.5
        self.native_min_value = 0.5
        self.native_step = 0.1
        super().__init__(climate)


class MtsFrostNumber(MtsRichTemperatureNumber):
    """
    Manages Appliance.Control.Thermostat.Frost:
    {"channel": 0, "onoff": 1, "value": 500, "min": 500, "max": 1500, "warning": 0}
    """

    ns = mn_t.Appliance_Control_Thermostat_Frost

    def __init__(self, climate: "MtsThermostatClimate"):
        self.native_max_value = 15
        self.native_min_value = 5
        self.native_step = climate.target_temperature_step
        super().__init__(climate)


class MtsOverheatNumber(MtsRichTemperatureNumber):
    """
    Configure overheat protection.
    Manages Appliance.Control.Thermostat.Overheat:
    {"warning": 0, "value": 335, "onoff": 1, "min": 200, "max": 700,
        "lmTime": 1674121910, "currentTemp": 355, "channel": 0}
    """

    ns = mn_t.Appliance_Control_Thermostat_Overheat

    __slots__ = ("sensor_external_temperature",)

    def __init__(self, climate: "MtsThermostatClimate"):
        self.native_max_value = 70
        self.native_min_value = 20
        self.native_step = climate.target_temperature_step
        super().__init__(climate, name="Overheat threshold")
        self.sensor_external_temperature = MLTemperatureSensor(
            self.manager, self.channel, "external sensor"
        )

    async def async_shutdown(self):
        self.sensor_external_temperature: MLTemperatureSensor = None  # type: ignore
        return await super().async_shutdown()

    def _parse(self, payload: dict):
        """{"warning": 0, "value": 335, "onoff": 1, "min": 200, "max": 700,
        "lmTime": 1674121910, "currentTemp": 355, "channel": 0}"""
        super()._parse(payload)
        if mc.KEY_CURRENTTEMP in payload:
            self.sensor_external_temperature.update_native_value(
                payload[mc.KEY_CURRENTTEMP] / self.device_scale
            )


class MtsWindowOpened(MLBinarySensor):
    """specialized binary sensor for Thermostat.WindowOpened entity used in Mts200-Mts960(maybe)."""

    ns = mn_t.Appliance_Control_Thermostat_WindowOpened
    key_value = mc.KEY_STATUS

    def __init__(self, climate: "MtsThermostatClimate"):
        super().__init__(
            climate.manager,
            climate.channel,
            mc.KEY_WINDOWOPENED,
            MLBinarySensor.DeviceClass.WINDOW,
        )
        climate.manager.register_parser_entity(self)


class MtsExternalSensorSwitch(MEListChannelMixin, MLSwitch):
    """sensor mode: use internal(0) vs external(1) sensor as temperature loopback."""

    ns = mn_t.Appliance_Control_Thermostat_Sensor
    key_value = mc.KEY_MODE

    # HA core entity attributes:
    entity_category = MLSwitch.EntityCategory.CONFIG

    def __init__(self, climate: "MtsThermostatClimate"):
        super().__init__(
            climate.manager,
            climate.channel,
            "external sensor mode",
            MLSwitch.DeviceClass.SWITCH,
        )
        climate.manager.register_parser_entity(self)


class MtsTempUnit(MEListChannelMixin, MLConfigSelect):

    ns = mn.Appliance_Control_TempUnit
    key_value = mn.Appliance_Control_TempUnit.key  # 'tempUnit'

    OPTIONS_MAP = {
        mc.TEMPUNIT_CELSIUS: MLConfigSelect.hac.UnitOfTemperature.CELSIUS,
        mc.TEMPUNIT_FAHRENHEIT: MLConfigSelect.hac.UnitOfTemperature.FAHRENHEIT,
    }

    manager: "Device"

    def __init__(self, climate: "MtsThermostatClimate"):
        super().__init__(climate.manager, climate.channel, self.key_value)
        climate.manager.register_parser_entity(self)


class MLScreenBrightnessNumber(MLConfigNumber):
    manager: "Device"

    ns = mn.Appliance_Control_Screen_Brightness

    # HA core entity attributes:
    icon: str = "mdi:brightness-percent"
    native_max_value = 100
    native_min_value = 0
    native_step = 12.5

    def __init__(self, manager: "Device", key: str):
        self.key_value = key
        super().__init__(
            manager,
            0,
            f"screenbrightness_{key}",
            native_unit_of_measurement=MLConfigNumber.hac.PERCENTAGE,
            name=f"Screen brightness ({key})",
        )

    async def async_set_native_value(self, value: float):
        """Override base async_set_native_value since it would round
        the value to an int (common device native type)."""
        if await self.async_request_value(value):
            self.update_device_value(value)


OPTIONAL_NAMESPACES_INITIALIZERS: set["mn.Namespace"] = {
    mn_t.Appliance_Control_Thermostat_CtlRange,
    mn_t.Appliance_Control_Thermostat_HoldAction,
    mn_t.Appliance_Control_Thermostat_SummerMode,
    mn_t.Appliance_Control_Thermostat_Timer,
}
"""These namespaces handlers will forward message parsing to the climate entity"""

OPTIONAL_ENTITIES_INITIALIZERS: dict[str, "Callable[[MtsThermostatClimate], Any]"] = {
    # mn.Appliance_Control_TempUnit.name: MtsTempUnit,
    mn_t.Appliance_Control_Thermostat_DeadZone.name: MtsDeadZoneNumber,
    mn_t.Appliance_Control_Thermostat_Frost.name: MtsFrostNumber,
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
        manager: "Device"

    class AdjustNumber(MtsRichTemperatureNumber):
        """
        Adjust temperature readings for mts200 and mts960.
        Manages Appliance.Control.Thermostat.Calibration:
        {"channel": 0, "value": 0, "min": -80, "max": 80, "lmTime": 1697010767}
        """

        ns = mn_t.Appliance_Control_Thermostat_Calibration

        def __init__(self, climate: "MtsThermostatClimate"):
            self.native_max_value = 8
            self.native_min_value = -8
            self.native_step = 0.1
            super().__init__(climate, name="Calibration")

    def __init__(self, manager: "Device", channel):
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

    def get_ns_adjust(self):
        return self.manager.namespace_handlers[
            mn_t.Appliance_Control_Thermostat_Calibration.name
        ]


POLLING_STRATEGY_CONF |= {
    mn_t.Appliance_Control_Thermostat_Calibration: (
        mlc.PARAM_CONFIG_UPDATE_PERIOD,
        mlc.PARAM_CLOUDMQTT_UPDATE_PERIOD,
        mlc.PARAM_HEADER_SIZE,
        80,
        NamespaceHandler.async_poll_lazy,
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
        NamespaceHandler.async_poll_lazy,
    ),
    mn_t.Appliance_Control_Thermostat_Frost: (
        mlc.PARAM_SENSOR_SLOW_UPDATE_PERIOD,
        mlc.PARAM_SENSOR_SLOW_UPDATE_CLOUD_PERIOD,
        mlc.PARAM_HEADER_SIZE,
        80,
        NamespaceHandler.async_poll_lazy,
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
        NamespaceHandler.async_poll_lazy,
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
        0,
        mlc.PARAM_HEADER_SIZE,
        550,
        NamespaceHandler.async_poll_lazy,
    ),
    mn_t.Appliance_Control_Thermostat_ScheduleB: (
        mlc.PARAM_CONFIG_UPDATE_PERIOD,
        0,
        mlc.PARAM_HEADER_SIZE,
        550,
        NamespaceHandler.async_poll_lazy,
    ),
    mn_t.Appliance_Control_Thermostat_Sensor: (
        mlc.PARAM_SENSOR_SLOW_UPDATE_PERIOD,
        mlc.PARAM_SENSOR_SLOW_UPDATE_CLOUD_PERIOD,
        mlc.PARAM_HEADER_SIZE,
        40,
        NamespaceHandler.async_poll_lazy,
    ),
}
