import typing

from .. import meross_entity as me
from ..binary_sensor import MLBinarySensor
from ..helpers.namespaces import NamespaceHandler
from ..merossclient import const as mc, namespaces as mn
from ..number import MLConfigNumber, MtsTemperatureNumber
from ..sensor import MLEnumSensor, MLTemperatureSensor
from ..switch import MLSwitch
from .mts200 import Mts200Climate
from .mts960 import Mts960Climate

if typing.TYPE_CHECKING:
    from ..meross_device import DigestInitReturnType, DigestParseFunc, MerossDevice
    from ..number import MLConfigNumberArgs

    MtsThermostatClimate = Mts200Climate | Mts960Climate


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


class MtsConfigSwitch(me.MEListChannelMixin, MLSwitch):

    # HA core entity attributes:
    entity_category = me.EntityCategory.CONFIG

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

    manager: "MerossDevice"
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
        **kwargs: "typing.Unpack[MLConfigNumberArgs]",
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


class MtsCalibrationNumber(MtsRichTemperatureNumber):
    """
    Adjust temperature readings for mts200 and mts960.
    Manages Appliance.Control.Thermostat.Calibration:
    {"channel": 0, "value": 0, "min": -80, "max": 80, "lmTime": 1697010767}
    """

    ns = mn.Appliance_Control_Thermostat_Calibration

    def __init__(self, climate: "MtsThermostatClimate"):
        self.native_max_value = 8
        self.native_min_value = -8
        self.native_step = 0.1
        super().__init__(climate, name="Calibration")


class MtsDeadZoneNumber(MtsRichTemperatureNumber):
    """
    adjust "dead zone" i.e. the threshold for the temperature control
    for mts200 and mts960 or whatever carries the Appliance.Control.Thermostat.DeadZone
    The min/max values are different between the two devices but the deadZone
    payload will carry the values and so set them
    """

    ns = mn.Appliance_Control_Thermostat_DeadZone

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

    ns = mn.Appliance_Control_Thermostat_Frost

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

    ns = mn.Appliance_Control_Thermostat_Overheat

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

    ns = mn.Appliance_Control_Thermostat_WindowOpened
    key_value = mc.KEY_STATUS

    def __init__(self, climate: "MtsThermostatClimate"):
        super().__init__(
            climate.manager,
            climate.channel,
            mc.KEY_WINDOWOPENED,
            MLBinarySensor.DeviceClass.WINDOW,
        )
        climate.manager.register_parser_entity(self)


class MtsExternalSensorSwitch(me.MEListChannelMixin, MLSwitch):
    """sensor mode: use internal(0) vs external(1) sensor as temperature loopback."""

    ns = mn.Appliance_Control_Thermostat_Sensor
    key_value = mc.KEY_MODE

    # HA core entity attributes:
    entity_category = me.EntityCategory.CONFIG

    def __init__(self, climate: "MtsThermostatClimate"):
        super().__init__(
            climate.manager,
            climate.channel,
            "external sensor mode",
            MLSwitch.DeviceClass.SWITCH,
        )
        climate.manager.register_parser_entity(self)


CLIMATE_INITIALIZERS: dict[str, type["MtsThermostatClimate"]] = {
    mc.KEY_MODE: Mts200Climate,
    mc.KEY_MODEB: Mts960Climate,
}
"""Core (climate) entities to initialize in _init_thermostat"""

DIGEST_KEY_TO_NAMESPACE: dict[str, mn.Namespace] = {
    mc.KEY_MODE: mn.Appliance_Control_Thermostat_Mode,
    mc.KEY_MODEB: mn.Appliance_Control_Thermostat_ModeB,
    mc.KEY_SUMMERMODE: mn.Appliance_Control_Thermostat_SummerMode,
    mc.KEY_WINDOWOPENED: mn.Appliance_Control_Thermostat_WindowOpened,
}
"""Maps the digest key to the associated namespace handler (used in _parse_thermostat)"""

OPTIONAL_NAMESPACES_INITIALIZERS = {
    mn.Appliance_Control_Thermostat_CtlRange,
    mn.Appliance_Control_Thermostat_HoldAction,
    mn.Appliance_Control_Thermostat_SummerMode,
    mn.Appliance_Control_Thermostat_Timer,
}
"""These namespaces handlers will forward message parsing to the climate entity"""

OPTIONAL_ENTITIES_INITIALIZERS: dict[
    str, typing.Callable[["MtsThermostatClimate"], typing.Any]
] = {
    mn.Appliance_Control_Thermostat_DeadZone.name: MtsDeadZoneNumber,
    mn.Appliance_Control_Thermostat_Frost.name: MtsFrostNumber,
    mn.Appliance_Control_Thermostat_Overheat.name: MtsOverheatNumber,
    mn.Appliance_Control_Thermostat_Sensor.name: MtsExternalSensorSwitch,
    mn.Appliance_Control_Thermostat_WindowOpened.name: MtsWindowOpened,
}
"""Additional entities (linked to the climate one) in case their ns is supported/available"""

# "Mode", "ModeB","SummerMode","WindowOpened" are carried in digest so we don't poll them
# We're using PollingStrategy for namespaces actually confirmed (by trace/diagnostics)
# to be PUSHED when over MQTT. The rest are either 'never seen' or 'not pushed'


def digest_init_thermostat(
    device: "MerossDevice", digest: dict
) -> "DigestInitReturnType":

    ability = device.descriptor.ability

    digest_handlers: dict[str, "DigestParseFunc"] = {}
    digest_pollers: set["NamespaceHandler"] = set()

    for ns_key, ns_digest in digest.items():

        try:
            ns = DIGEST_KEY_TO_NAMESPACE[ns_key]
        except KeyError:
            # ns_key is still not mapped in DIGEST_KEY_TO_NAMESPACE
            for namespace in ability.keys():
                ns = mn.NAMESPACES[namespace]
                if ns.is_thermostat and (ns.key == ns_key):
                    DIGEST_KEY_TO_NAMESPACE[ns_key] = ns
                    break
            else:
                # ns_key is really unknown..
                digest_handlers[ns_key] = device.digest_parse_empty
                continue

        handler = device.get_handler(ns)
        digest_handlers[ns_key] = handler.parse_list
        digest_pollers.add(handler)

        if climate_class := CLIMATE_INITIALIZERS.get(ns_key):
            for channel_digest in ns_digest:
                channel = channel_digest[mc.KEY_CHANNEL]
                climate = climate_class(device, channel, MtsCalibrationNumber)
                device.register_parser_entity(climate)
                device.register_parser_entity(climate.schedule)
                for optional_ns in OPTIONAL_NAMESPACES_INITIALIZERS:
                    if optional_ns.name in ability:
                        device.register_parser(climate, optional_ns)

                for namespace, entity_class in OPTIONAL_ENTITIES_INITIALIZERS.items():
                    if namespace in ability:
                        entity_class(climate)

    def digest_parse(digest: dict):
        """
        MTS200 typically carries:
        {
            "mode": [...],
            "summerMode": [],
            "windowOpened": []
        }
        MTS960 typically carries:
        {
            "modeB": [...]
        }
        """
        for ns_key, ns_digest in digest.items():
            digest_handlers[ns_key](ns_digest)

    return digest_parse, digest_pollers


class MLScreenBrightnessNumber(MLConfigNumber):
    manager: "MerossDevice"

    ns = mn.Appliance_Control_Screen_Brightness

    # HA core entity attributes:
    icon: str = "mdi:brightness-percent"
    native_max_value = 100
    native_min_value = 0
    native_step = 12.5

    def __init__(self, manager: "MerossDevice", key: str):
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


class ScreenBrightnessNamespaceHandler(NamespaceHandler):

    __slots__ = (
        "number_brightness_operation",
        "number_brightness_standby",
    )

    def __init__(self, device: "MerossDevice"):
        NamespaceHandler.__init__(
            self,
            device,
            mn.Appliance_Control_Screen_Brightness,
            handler=self._handle_Appliance_Control_Screen_Brightness,
        )
        self.polling_request_add_channel(0)
        self.number_brightness_operation = MLScreenBrightnessNumber(
            device, mc.KEY_OPERATION
        )
        self.number_brightness_standby = MLScreenBrightnessNumber(
            device, mc.KEY_STANDBY
        )

    def _handle_Appliance_Control_Screen_Brightness(self, header: dict, payload: dict):
        for p_channel in payload[mc.KEY_BRIGHTNESS]:
            if p_channel[mc.KEY_CHANNEL] == 0:
                self.number_brightness_operation.update_device_value(
                    p_channel[mc.KEY_OPERATION]
                )
                self.number_brightness_standby.update_device_value(
                    p_channel[mc.KEY_STANDBY]
                )
                break
