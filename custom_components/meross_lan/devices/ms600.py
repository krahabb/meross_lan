from typing import TYPE_CHECKING, override

from .. import const as mlc
from ..binary_sensor import BinarySensorEntity
from ..helpers.entity import ValueParser
from ..merossclient.device.handler import NamespaceHandler
from ..merossclient.protocol import const as mc, namespaces as mn
from ..number import NumberParser
from ..select import SelectParser
from ..sensor import SensorParser
from .misc import SensorLatestXParser

if TYPE_CHECKING:
    from typing import Final, Self, Unpack

    from ..helpers.device import Device


class PresenceConfigBase(ValueParser):
    """Mixin style base class for all of the entities managed in Appliance.Control.Presence.Config"""

    if TYPE_CHECKING:

        class Args(ValueParser.Args):
            pass

    # HA core entity attributes:
    _attr_entity_category = SelectParser.EntityCategory.CONFIG


class PresenceConfigNumber(PresenceConfigBase, NumberParser):
    """Base class for config values represented as Number entities in HA."""

    if TYPE_CHECKING:

        class Args(PresenceConfigBase.Args, NumberParser.Args):
            pass

        @classmethod
        def DEF(cls, **kwargs: "Unpack[Args]") -> type["Self"]: ...


class PresenceConfigSelect(PresenceConfigBase, SelectParser):
    """Base class for config values represented as Select entities in HA."""

    if TYPE_CHECKING:

        class Args(PresenceConfigBase.Args, SelectParser.Args):
            pass

        @classmethod
        def DEF(cls, **kwargs: "Unpack[Args]") -> type["Self"]: ...


class PresenceConfigMode(PresenceConfigSelect):

    # TODO: configure real labels
    # This map would actually be shared between workMode and testMode though
    init_options_map = {
        0: "0",
        1: "1",
        2: "2",
    }


class PresenceConfigSensitivity(PresenceConfigSelect):

    # TODO: configure real labels
    init_options_map = {
        0: "0",
        1: "1",
        2: "2",
    }


class PresenceConfigMthX(PresenceConfigNumber):
    # HA core entity attributes:
    _attr_native_max_value = 1000
    _attr_native_min_value = 1
    _attr_native_step = 1


ENTITY_DEFS = (
    PresenceConfigMode.DEF(
        entity_key=f"presence_config_{mc.KEY_MODE}_{mc.KEY_WORKMODE}",
        key_value=PresenceConfigMode.NestedKeyValue(mc.KEY_MODE, mc.KEY_WORKMODE),
        name=mc.KEY_WORKMODE,
    ),
    PresenceConfigMode.DEF(
        entity_key=f"presence_config_{mc.KEY_MODE}_{mc.KEY_TESTMODE}",
        key_value=PresenceConfigMode.NestedKeyValue(mc.KEY_MODE, mc.KEY_TESTMODE),
        name=mc.KEY_TESTMODE,
    ),
    PresenceConfigNumber.DEF(
        entity_key=f"presence_config_{mc.KEY_NOBODYTIME}_{mc.KEY_TIME}",
        key_value=PresenceConfigNumber.NestedKeyValue(mc.KEY_NOBODYTIME, mc.KEY_TIME),
        name=mc.KEY_NOBODYTIME,
        device_class=NumberParser.DEVICE_CLASS_DURATION,
        native_max_value=3600,  # 1 hour ?
        native_min_value=1,
        native_step=1,
    ),
    PresenceConfigNumber.DEF(
        entity_key=f"presence_config_{mc.KEY_DISTANCE}_{mc.KEY_VALUE}",
        key_value=PresenceConfigNumber.NestedKeyValue(mc.KEY_DISTANCE, mc.KEY_VALUE),
        device_scale=1000,
        name=mc.KEY_DISTANCE,
        device_class=NumberParser.DeviceClass.DISTANCE,
        native_unit_of_measurement=mlc.hac.UnitOfLength.METERS,
        native_max_value=12,
        native_min_value=0.1,
        native_step=0.1,
    ),
    PresenceConfigSensitivity.DEF(
        entity_key=f"presence_config_{mc.KEY_SENSITIVITY}_{mc.KEY_LEVEL}",
        key_value=PresenceConfigSensitivity.NestedKeyValue(
            mc.KEY_SENSITIVITY, mc.KEY_LEVEL
        ),
        name=mc.KEY_SENSITIVITY,
    ),
    PresenceConfigMthX.DEF(
        entity_key=f"presence_config_{mc.KEY_MTHX}_{mc.KEY_MTH1}",
        key_value=PresenceConfigMthX.NestedKeyValue(mc.KEY_MTHX, mc.KEY_MTH1),
        name=mc.KEY_MTH1,
    ),
    PresenceConfigMthX.DEF(
        entity_key=f"presence_config_{mc.KEY_MTHX}_{mc.KEY_MTH2}",
        key_value=PresenceConfigMthX.NestedKeyValue(mc.KEY_MTHX, mc.KEY_MTH2),
        name=mc.KEY_MTH2,
    ),
    PresenceConfigMthX.DEF(
        entity_key=f"presence_config_{mc.KEY_MTHX}_{mc.KEY_MTH3}",
        key_value=PresenceConfigMthX.NestedKeyValue(mc.KEY_MTHX, mc.KEY_MTH3),
        name=mc.KEY_MTH3,
    ),
)


def namespace_init_presence_config(ns: mn.Namespace, device: "Device", /):
    index = mn.IndexType.channel(0)
    sensor_presence = PresenceSensor(
        0,
        device,
        entity_key=f"sensor_{mc.KEY_PRESENCE}",
        index=index,
        device_info=device.get_device_entry_info(0),
    )

    NamespaceHandler(
        ns, device, config=mlc.POLLING_CONFIG_CONFIGURATION
    ).register_parsers(
        *(entity_def(sensor_presence, ns=ns) for entity_def in ENTITY_DEFS)
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
