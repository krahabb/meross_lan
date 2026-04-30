from typing import TYPE_CHECKING, override

from .. import const as mlc
from ..binary_sensor import BinarySensorEntity
from ..helpers.entity import ValueParser
from ..merossclient.device.handler import MappingParser
from ..merossclient.protocol import const as mc, namespaces as mn
from ..number import NumberParser
from ..select import SelectParser
from ..sensor import SensorParser
from .misc import SensorLatestXParser

if TYPE_CHECKING:
    from typing import Final, Self, Unpack

    from ..helpers.device import Device


class PresenceConfigMode(SelectParser):

    # TODO: configure real labels
    # This map would actually be shared between workMode and testMode though
    init_options_map = {
        0: "0",
        1: "1",
        2: "2",
    }


class PresenceSensor(SensorParser):
    """ms600 presence sensor."""

    init_entity_key = f"sensor_{mc.KEY_PRESENCE}"  # backward compatibility
    _attr_name = "Presence"

    __slots__ = (
        "sensor_distance",
        "binary_sensor_motion",
        "sensor_times",
    )

    def __init__(
        self,
        id,
        device: "Device",
        /,
        **kwargs: "Unpack[SensorParser.Args]",
    ):
        SensorParser.__init__(self, id, device, **kwargs)
        self.sensor_distance = SensorParser(
            self,
            entity_key=f"{self.entity_key}_distance",
            device_scale=1000,
            device_class=SensorParser.DeviceClass.DISTANCE,
            native_unit_of_measurement=mlc.hac.UnitOfLength.METERS,
            suggested_display_precision=2,
            name="Presence distance",
        )
        self.binary_sensor_motion = BinarySensorEntity(
            self,
            entity_key=f"{self.entity_key}_motion",
            device_class=BinarySensorEntity.DeviceClass.MOTION,
        )
        self.sensor_times = SensorParser(
            self,
            entity_key=f"{self.entity_key}_times",
            name="Presence times",
        )

    @override
    def __call__(self, payload: dict, /):
        """
        {"times": 0, "distance": 760, "value": 2, "timestamp": 1725907895}
        """
        self.update_device_value(payload[mc.KEY_VALUE])
        self.sensor_distance.update_device_value(payload[mc.KEY_DISTANCE])
        self.binary_sensor_motion.update_boolean_value(payload[mc.KEY_VALUE] == 2)
        self.sensor_times.update_device_value(payload[mc.KEY_TIMES])


class PresenceConfigParser(MappingParser):

    POLLING_CONFIG_DEFAULT = mlc.POLLING_CONFIG_CONFIGURATION

    init_parser_defs = {
        mc.KEY_MODE: {
            mc.KEY_WORKMODE: PresenceConfigMode.DEF(
                entity_key=f"presence_config_{mc.KEY_MODE}_{mc.KEY_WORKMODE}",
                name=mc.KEY_WORKMODE,
            ),
            mc.KEY_TESTMODE: PresenceConfigMode.DEF(
                entity_key=f"presence_config_{mc.KEY_MODE}_{mc.KEY_TESTMODE}",
                name=mc.KEY_TESTMODE,
            ),
        },
        mc.KEY_NOBODYTIME: {
            mc.KEY_TIME: NumberParser.DEF(
                entity_key=f"presence_config_{mc.KEY_NOBODYTIME}_{mc.KEY_TIME}",
                name=mc.KEY_NOBODYTIME,
                device_class=NumberParser.DEVICE_CLASS_DURATION,
                native_max_value=3600,  # 1 hour ?
                native_min_value=1,
                native_step=1,
            ),
        },
        mc.KEY_DISTANCE: {
            mc.KEY_VALUE: NumberParser.DEF(
                entity_key=f"presence_config_{mc.KEY_DISTANCE}_{mc.KEY_VALUE}",
                device_scale=1000,
                name=mc.KEY_DISTANCE,
                device_class=NumberParser.DeviceClass.DISTANCE,
                native_unit_of_measurement=mlc.hac.UnitOfLength.METERS,
                native_max_value=12,
                native_min_value=0.1,
                native_step=0.1,
            ),
        },
        mc.KEY_SENSITIVITY: {
            mc.KEY_LEVEL: SelectParser.DEF(
                entity_key=f"presence_config_{mc.KEY_SENSITIVITY}_{mc.KEY_LEVEL}",
                options_map={  # TODO: configure real labels
                    0: "0",
                    1: "1",
                    2: "2",
                },
                name=mc.KEY_SENSITIVITY,
            ),
        },
        mc.KEY_MTHX: {
            mc.KEY_MTH1: NumberParser.DEF(
                entity_key=f"presence_config_{mc.KEY_MTHX}_{mc.KEY_MTH1}",
                name=mc.KEY_MTH1,
                native_max_value=1000,
                native_min_value=1,
                native_step=1,
            ),
            mc.KEY_MTH2: NumberParser.DEF(
                entity_key=f"presence_config_{mc.KEY_MTHX}_{mc.KEY_MTH2}",
                name=mc.KEY_MTH2,
                native_max_value=1000,
                native_min_value=1,
                native_step=1,
            ),
            mc.KEY_MTH3: NumberParser.DEF(
                entity_key=f"presence_config_{mc.KEY_MTHX}_{mc.KEY_MTH3}",
                name=mc.KEY_MTH3,
                native_max_value=1000,
                native_min_value=1,
                native_step=1,
            ),
        },
    }

    @classmethod
    @override
    def namespace_init(cls, ns: mn.Namespace, device: "Device", /):
        super().namespace_init(ns, device)

        index = mn.IndexType.channel.get(0)
        sensor_presence = PresenceSensor(
            0,
            device,
            entity_key=f"sensor_{mc.KEY_PRESENCE}",
            index=index,
            device_info=device.get_device_entry_info(0),
        )

        device.ns_handlers[mn.Appliance_Control_Sensor_LatestX].register_parser(
            SensorLatestXParser(
                0,
                device,
                index=index,
                parsers={
                    mc.KEY_PRESENCE: sensor_presence,
                    mc.KEY_LIGHT: SensorParser(
                        sensor_presence,
                        **(
                            SensorParser.LIGHT_ARGS
                            | {"entity_key": f"sensor_{mc.KEY_LIGHT}"}
                        ),
                    ),
                },
            )
        )
