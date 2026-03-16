from typing import TYPE_CHECKING

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

    from ...helpers.device import Device, MerossMessage
    from ...helpers.entity import ParserEntity
    from ...merossclient.device.handler import NamespaceHandler as _NamespaceHandler
    from ...merossclient.protocol.namespaces import Namespace
    from ...merossclient.protocol.types import JsonDict, thermostat as mt_t


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

    POLLING_CONFIG_DEFAULT = NamespaceHandler.POLLING_CONFIG_CONFIGURATION_NS

    def __init__(self, ns: "mn.Namespace", device: "Device", /):
        NamespaceHandler.__init__(self, ns, device)
        self.register_parsers(
            *(
                ScreenBrightnessNumber(
                    0,
                    device,
                    entity_key=f"screenbrightness_{key}",
                    name=f"Screen brightness ({key})",
                    ns=ns,
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

    def __init__(self, channel: int, device: "Device", /, **kwargs):
        NumberParser.__init__(
            self,
            channel,
            device,
            **kwargs,
            entity_key=kwargs["ns"].slug_end,  # TODO: generalize entity_key defaulting?
            device_scale=device.entities[channel].temperature_scale,  # type: ignore (access MtsThermostatClimate.temperature_scale)
        )

    def _parse(self, payload: "mt_t.CommonTemperature_C", /):
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

    def _parse(self, payload: "mt_t.CommonTemperatureExt_C", /):
        try:
            warning = payload[mc.KEY_WARNING]
            self.sensor_warning.update_device_value(warning)
        except AttributeError:
            entity_key = f"{self.entity_key}_warning"
            self.sensor_warning = EnumParser(
                self.channel,
                self.parent,
                entity_key=entity_key,
                key_value=mc.KEY_WARNING,
                device_value=warning,
                translation_key=f"mts_{entity_key}",
            )
        except KeyError:
            pass
        try:
            self.available = bool(payload[mc.KEY_ONOFF])
            self.switch.update_boolean_value(self.available)
        except AttributeError:
            self.switch = SwitchParser(
                self.channel,
                self.parent,
                entity_key=f"{self.entity_key}_switch",
                is_on=self.available,
                name=(f"{self.entity_key} Alarm").capitalize(),
                ns=self.ns,
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


# TODO: add 'ns' kwarg management to base parser so we can drop all these static class definitions
# in favor of simple kwargs sets to be used for initialization.
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

    def _parse(self, payload: "mt_t.Overheat_C", /):
        try:
            current_temp = payload[mc.KEY_CURRENTTEMP]
            self.sensor_external_temperature.update_device_value(current_temp)
        except AttributeError:
            self.sensor_external_temperature = SensorParser.Temperature(
                self.channel,
                self.parent,
                entity_key="external sensor",
                device_value=current_temp,
                device_scale=self.device_scale,
            )
        except KeyError:
            pass
        MtsCommonTemperatureExtNumber._parse(self, payload)


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
    def _parse(self, payload: "mt_t.HoldAction_C", /):
        self.update_device_value(payload[self.key_value])
        try:
            time = payload[mc.KEY_TIME]  # type: ignore
            self.number_time.update_device_value(time)
        except AttributeError:
            self.number_time = NumberParser(
                self.channel,
                self.parent,
                entity_key="hold_action_time",
                device_scale=1,
                device_value=time,
                device_class=NumberParser.DEVICE_CLASS_DURATION,
                native_unit_of_measurement=mlc.hac.UnitOfTime.MINUTES,
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

    if TYPE_CHECKING:

        OPTIONAL_NAMESPACES_INITIALIZERS: Final[tuple[mn.Namespace, ...]]
        """These namespaces handlers will forward message parsing to the climate entity"""
        OPTIONAL_ENTITIES_INITIALIZERS: Final[dict[mn.Namespace, type[ParserEntity]]]
        """Additional entities (linked to the climate one) in case their ns is supported/available"""

        # Overrides
        parent: Final[Device]  # type: ignore
        channel: Final[int]  # type: ignore

    OPTIONAL_NAMESPACES_INITIALIZERS = (
        mn_t.Appliance_Control_Thermostat_CtlRange,  # mts960
        mn_t.Appliance_Control_Thermostat_SummerMode,  # mts200
        mn_t.Appliance_Control_Thermostat_System,  # mts300
        mn_t.Appliance_Control_Thermostat_Timer,  # mts960
        mn.Appliance_Config_Sensor_Association,  # mts300
    )

    OPTIONAL_ENTITIES_INITIALIZERS = {
        mn.Appliance_Control_TempUnit: MtsTempUnit,
        mn_t.Appliance_Control_Thermostat_DeadZone: MtsDeadZoneNumber,
        mn_t.Appliance_Control_Thermostat_Frost: MtsFrostNumber,
        mn_t.Appliance_Control_Thermostat_HoldAction: MtsHoldAction,
        mn_t.Appliance_Control_Thermostat_Overheat: MtsOverheatNumber,
        mn_t.Appliance_Control_Thermostat_Sensor: MtsExternalSensorSwitch,
        mn_t.Appliance_Control_Thermostat_WindowOpened: MtsWindowOpened,
    }

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

    def __init__(self, channel: int, device: "Device", /, **kwargs):
        MtsClimate.__init__(self, channel, device, **kwargs)
        device.register_parser_ex(self, *self.OPTIONAL_NAMESPACES_INITIALIZERS)
        ability = device.descriptor.ability
        for _ns, _entity_class in self.OPTIONAL_ENTITIES_INITIALIZERS.items():
            if _ns in ability:
                device.get_handler(_ns).register_parser(
                    _entity_class(channel, device, ns=_ns)
                )

    # interface: self
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


from .mts200 import Mts200Climate
from .mts960 import Mts960Climate

# TODO: These maps should be placed in a global DIGEST<->NAMESPACE map of the Meross ecosystem
# so to remove those ns mappings distributed across classes.
CLIMATE_INITIALIZERS: dict[str, type["MtsThermostatClimate"]] = {
    mc.KEY_MODE: Mts200Climate,
    mc.KEY_MODEB: Mts960Climate,
}
"""Core (climate) entities to initialize in _init_thermostat"""

DIGEST_KEY_TO_NAMESPACE: dict[str, "Namespace"] = {
    mc.KEY_MODE: mn_t.Appliance_Control_Thermostat_Mode,
    mc.KEY_MODEB: mn_t.Appliance_Control_Thermostat_ModeB,
    mc.KEY_SUMMERMODE: mn_t.Appliance_Control_Thermostat_SummerMode,
    mc.KEY_WINDOWOPENED: mn_t.Appliance_Control_Thermostat_WindowOpened,
}
"""Maps the digest key to the associated namespace handler (used in _parse_thermostat)"""

# "Mode", "ModeB","SummerMode","WindowOpened" are carried in digest so we don't poll them
# We're using PollingStrategy for namespaces actually confirmed (by trace/diagnostics)
# to be PUSHED when over MQTT. The rest are either 'never seen' or 'not pushed'


def digest_init_thermostat(
    device: "Device", digest: "JsonDict", /
) -> "Device.DigestInitReturnType":

    ability = device.descriptor.ability

    digest_parsers: dict[str, "Device.DigestParseFunc"] = {}
    digest_pollers: set["_NamespaceHandler"] = set()

    for ns_key, ns_digest in digest.items():

        try:
            ns = DIGEST_KEY_TO_NAMESPACE[ns_key]
        except KeyError:
            # ns_key is still not mapped in DIGEST_KEY_TO_NAMESPACE
            for namespace in ability:
                ns = mn.NAMESPACES[namespace]
                if ns.is_thermostat and (ns.key == ns_key):
                    DIGEST_KEY_TO_NAMESPACE[ns_key] = ns
                    break
            else:
                # ns_key is really unknown..
                digest_parsers[ns_key] = device.digest_parse_empty
                continue

        try:
            climate_class = CLIMATE_INITIALIZERS[ns_key]
        except KeyError:
            handler = device.get_handler(ns)
        else:
            handler = NamespaceHandler(
                ns,
                device,
                parser_class=climate_class,
                channels=(_digest[mc.KEY_CHANNEL] for _digest in ns_digest),
            )
        digest_parsers[ns_key] = handler.parse_list
        digest_pollers.add(handler)

    def digest_parse_thermostat(digest: "JsonDict", /):
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
            digest_parsers[ns_key](ns_digest)

    # switches might be installed on demand while parsing
    device.platforms.setdefault(SwitchParser.PLATFORM)

    return digest_parse_thermostat, digest_pollers


NamespaceHandler.POLLING_CONFIG_MAP.update(
    {
        mn.Appliance_Control_TempUnit: NamespaceHandler.POLLING_CONFIG_CONFIGURATION_NS,
        mn_t.Appliance_Control_Thermostat_Calibration: NamespaceHandler.POLLING_CONFIG_CONFIGURATION_NS,
        mn_t.Appliance_Control_Thermostat_CtlRange: NamespaceHandler.POLLING_CONFIG_SINGLEPOLL_NS,
        mn_t.Appliance_Control_Thermostat_DeadZone: NamespaceHandler.POLLING_CONFIG_CONFIGURATION_NS,
        mn_t.Appliance_Control_Thermostat_Frost: NamespaceHandler.POLLING_CONFIG_SLOWSENSOR_NS,
        mn_t.Appliance_Control_Thermostat_HoldAction: NamespaceHandler.POLLING_CONFIG_CONFIGURATION_NS,
        mn_t.Appliance_Control_Thermostat_ModeC: NamespaceHandler.POLLING_CONFIG_STATE_NS,
        mn_t.Appliance_Control_Thermostat_Overheat: NamespaceHandler.POLLING_CONFIG_SLOWSENSOR_NS,
        mn_t.Appliance_Control_Thermostat_Timer: NamespaceHandler.POLLING_CONFIG_STATE_NS,
        mn_t.Appliance_Control_Thermostat_Schedule: NamespaceHandler.POLLING_CONFIG_CONFIGURATION_NS,
        mn_t.Appliance_Control_Thermostat_ScheduleB: NamespaceHandler.POLLING_CONFIG_CONFIGURATION_NS,
        mn_t.Appliance_Control_Thermostat_Sensor: NamespaceHandler.POLLING_CONFIG_SLOWSENSOR_NS,
    }
)
