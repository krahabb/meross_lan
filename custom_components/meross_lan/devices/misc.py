"""
Miscellaneous namespace handlers and devices/entities.
This unit is a collection of rarely used small components where having
a dedicated unit for each of them would increase the number of small modules.
"""

from typing import TYPE_CHECKING, override

from .. import const as mlc
from ..merossclient.device.handler import MappingParser, NamespaceHandler
from ..merossclient.protocol import const as mc, namespaces as mn
from ..merossclient.protocol.namespaces import thermostat as mn_t
from ..number import NumberParser
from ..select import SelectParser
from ..sensor import SensorParser

if TYPE_CHECKING:
    from typing import ClassVar, Final, Mapping, NotRequired, Unpack

    from ..helpers.device import Device
    from ..merossclient.device.handler import NamespaceHandler
    from ..merossclient.protocol import types as mt
    from .thermostat.mts200 import Mts200Climate


class DeviceCfgParser(MappingParser):

    POLLING_CONFIG_DEFAULT = mlc.POLLING_CONFIG_CONFIGURATION
    init_ns = mn.Appliance_Config_DeviceCfg

    # These are extracted from ms130 payload but they look like generic enough
    # to be available through a wide set of devices.
    KEY_CALIBRATECFG = "calibrateCfg"
    KEY_TIMECFG = "timeCfg"
    KEY_AM = "am"
    KEY_UNITCFG = "unitCfg"
    KEY_UNITTYPE = "unitType"  # This doesn't appear in ms130 but it seems to be available in mst devices

    init_parser_defs = {
        KEY_CALIBRATECFG: {
            mc.KEY_HUMI: NumberParser.DEF(
                entity_key=f"{mn.Appliance_Config_DeviceCfg.slug}__{KEY_CALIBRATECFG}_{mc.KEY_HUMI}",
                key_value=NumberParser.NestedKeyValue(KEY_CALIBRATECFG, mc.KEY_HUMI),
                device_scale=10,
                device_class=NumberParser.DeviceClass.HUMIDITY,
                name="Humidity Calibration",
                native_min_value=-20,
                native_max_value=20,
                native_step=1,
            ),
            mc.KEY_TEMP: NumberParser.DEF(
                entity_key=f"{mn.Appliance_Config_DeviceCfg.slug}__{KEY_CALIBRATECFG}_{mc.KEY_TEMP}",
                key_value=NumberParser.NestedKeyValue(KEY_CALIBRATECFG, mc.KEY_TEMP),
                device_scale=100,
                device_class=NumberParser.DeviceClass.TEMPERATURE,
                name="Temperature Calibration",
                native_min_value=-5,
                native_max_value=5,
                native_step=0.1,
            ),
        },
        KEY_TIMECFG: {
            KEY_AM: SelectParser.DEF(
                entity_key=f"{mn.Appliance_Config_DeviceCfg.slug}__{KEY_TIMECFG}_{KEY_AM}",
                key_value=SelectParser.NestedKeyValue(KEY_TIMECFG, KEY_AM),
                options_map={1: "12 h", 2: "24 h"},
                name="Time Format",
            )
        },
        KEY_UNITCFG: {
            mc.KEY_TEMPUNIT: SelectParser.DEF(
                entity_key=f"{mn.Appliance_Config_DeviceCfg.slug}__{KEY_UNITCFG}_{mc.KEY_TEMPUNIT}",
                key_value=SelectParser.NestedKeyValue(KEY_UNITCFG, mc.KEY_TEMPUNIT),
                options_map={1: "Celsius", 2: "Fahrenheit"},
                name="Temperature Unit",
            ),
            KEY_UNITTYPE: SelectParser.DEF(
                entity_key=f"{mn.Appliance_Config_DeviceCfg.slug}__{KEY_UNITCFG}_{KEY_UNITTYPE}",
                key_value=SelectParser.NestedKeyValue(KEY_UNITCFG, KEY_UNITTYPE),
                options_map={1: "US Customary", 2: "Metric"},
                name="Unit Type",
            ),
        },
    }

    @classmethod
    def namespace_init(cls, ns: mn.Namespace, device: "Device", /):
        # This ns is actually globally configured by Device.async_init with
        # this 'generic' parser config but specialized devices will likely
        # install their own instances of parsers/configs. This is especially needed because
        # this ns requires polling with actual indexes and this might vary.
        # The default here is just a 'placeholder' (still it could provide some
        # basic entities for some devices)
        NamespaceHandler(ns, device, parser_class=cls, channels=())


class Mts200HumiSensor(SensorParser):

    def __init__(self, *args, **kwargs):
        SensorParser.__init__(self, *args, **kwargs)
        try:
            # This is almost 100% sure since key 'humi' in Sensor.Latest
            # is likely just the mts200 reporting the current humidity, but we can never be sure
            climate: "Mts200Climate" = self.parent.ns_handlers[  # type: ignore
                mn_t.Appliance_Control_Thermostat_Mode
            ].parsers[self.index]

            def _flush_climate_state():
                climate.current_humidity = self.native_value
                climate.schedule_flush_state()

            self.register_state_callback(_flush_climate_state)
            # on self construction we're already parsing an update so
            # we need to forward to climate because self.flush_state() is not being called
            _flush_climate_state()
        except KeyError:
            # not an mts200?
            pass


class SensorLatestParser(MappingParser):

    if TYPE_CHECKING:
        parent: Final[Device]  # type: ignore[override]
        init_parser_defs: ClassVar[Mapping[str, type[SensorParser]]]
        parser_defs: Mapping[str, type[SensorParser]]

    POLLING_CONFIG_DEFAULT = mlc.POLLING_CONFIG_FASTSENSOR

    init_parser_defs = {
        mc.KEY_HUMI: Mts200HumiSensor.DEF(
            **SensorParser.HUMIDITY_ARGS
            | {
                "entity_key": f"sensor_{mc.KEY_HUMI}",
                "key_value": SensorParser.SimpleKeyValue(mc.KEY_HUMI),
            }
        ),
        mc.KEY_TEMP: SensorParser.DEF(
            **SensorParser.TEMPERATURE_ARGS
            | {
                "entity_key": f"sensor_{mc.KEY_TEMP}",
                "key_value": SensorParser.SimpleKeyValue(mc.KEY_TEMP),
                "device_scale": 100,
            }
        ),
    }

    @override
    def __call__(self, payload: "mt.sensor.Latest", /):
        MappingParser.__call__(self, payload[mc.KEY_VALUE][0])


class SensorLatestXParser(MappingParser):
    """Generalized structured parsing for Appliance.Control.Sensor.LatestX.
    This ns requires to be polled by the correct key list in the request payload
    and this striclty depends on the device type.
    The approach here is to just initialize the handler and configure it with
    a MappingParser (SensorLatestXParser) as parser_class so that any channel
    indexed payload is then forwarded to an isntance of this class.
    The instance itself is able to automatically setup some common types of entities/parsers
    but we need anyway to configure the correct request payload. The inizialization is then done in 2 steps:
    1) the handler is initialized with SensorLatestXParser as parser_class
    2) later specific device initialization will refine polling configuration and parsers config/layout
    """

    if TYPE_CHECKING:
        parent: Final[Device]  # type: ignore[override]
        init_parser_defs: ClassVar[Mapping[str, type[SensorParser]]]
        parser_defs: Mapping[str, type[SensorParser]]
        parsers: Final[dict[str, SensorParser]]  # type: ignore[override]

        class Args(MappingParser.Args):
            parsers: NotRequired[dict[str, SensorParser]]

        def __init__(self, *args, **kwargs: Unpack[Args]): ...  # pragma: no cover

    POLLING_CONFIG_DEFAULT = mlc.POLLING_CONFIG_FASTSENSOR
    init_ns = mn.Appliance_Control_Sensor_LatestX

    # many of these defs are guesses and the actual composition
    # of parsers is explicitly preset when constructing a 'known' device SensorLatestXParser
    # See ms130 and ms600.
    init_parser_defs = {
        mc.KEY_HUMI: SensorParser.DEF(**SensorParser.HUMIDITY_ARGS),
        mc.KEY_LIGHT: SensorParser.DEF(**SensorParser.LIGHT_ARGS),
        mc.KEY_TEMP: SensorParser.DEF(
            **(SensorParser.TEMPERATURE_ARGS | {"device_scale": 100})
        ),
    }

    @classmethod
    @override
    def namespace_init(cls, ns: mn.Namespace, device: "Device", /):
        # TODO: move config default to either the parser class or to the ns grammar
        # so we can remove this override
        return NamespaceHandler(ns, device, parser_class=cls, channels=())

    @override
    def _namespace_registered(self, handler_registration):
        super()._namespace_registered(handler_registration)
        self.handler_ns.polling_request_payload.append(
            self.handler_ns.polling_request_payload.pop()
            | {mc.KEY_DATA: [*self.parsers.keys()]}
        )

    @override
    def __call__(self, payload: "mt.sensor.LatestX", /):
        for key, value in payload[mc.KEY_DATA].items():
            try:
                self.parsers[key](value[0])
            except KeyError:
                if key in self.parsers:
                    raise
                self.parsers[key] = self.parser_defs.get(key, SensorParser)(
                    self.index.value,  # FIXME: use a 'sibling' construction semantic
                    self.parent,
                    entity_key=f"sensor_{key}",
                    index=self.index,
                    ns_value=value[0][mc.KEY_VALUE],
                )

                """
                # TODO: add the data key to out polling request
                for channel_payload in self.polling_request_payload:
                    if channel_payload[mc.KEY_CHANNEL] == channel:
                        channel_payload[mc.KEY_DATA].append(data_key)
                        break
                """
