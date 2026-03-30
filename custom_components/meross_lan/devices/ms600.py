from typing import TYPE_CHECKING, override

from ..binary_sensor import BinarySensorEntity
from ..const import hac
from ..helpers.entity import ValueParser
from ..helpers.namespaces import mc, mn
from ..number import NumberParser
from ..select import SelectParser
from ..sensor import SensorParser

if TYPE_CHECKING:
    from typing import Final, Self, Unpack

    from ..helpers.device import Device
    from ..helpers.entity import ChannelType


class PresenceConfigBase(ValueParser.NamespaceGroupValue, ValueParser):
    """Mixin style base class for all of the entities managed in Appliance.Control.Presence.Config"""

    if TYPE_CHECKING:

        class Args(ValueParser.NamespaceGroupValue.Args, ValueParser.Args):
            pass

    # HA core entity attributes:
    _attr_entity_category = SelectParser.EntityCategory.CONFIG


class PresenceConfigNumber(PresenceConfigBase, NumberParser):
    """Base class for config values represented as Number entities in HA."""

    if TYPE_CHECKING:

        class Args(PresenceConfigBase.Args, NumberParser.Args):
            pass

        @classmethod
        def ENTITY_DEF(cls, **kwargs: "Unpack[Args]") -> type["Self"]: ...


class PresenceConfigSelect(PresenceConfigBase, SelectParser):
    """Base class for config values represented as Select entities in HA."""

    if TYPE_CHECKING:

        class Args(PresenceConfigBase.Args, SelectParser.Args):
            pass

        @classmethod
        def ENTITY_DEF(cls, **kwargs: "Unpack[Args]") -> type["Self"]: ...


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
    init_key_group = mc.KEY_MTHX
    # HA core entity attributes:
    _attr_native_max_value = 1000
    _attr_native_min_value = 1
    _attr_native_step = 1


ENTITY_DEFS = (
    PresenceConfigMode.ENTITY_DEF(
        entity_key=f"presence_config_{mc.KEY_MODE}_{mc.KEY_WORKMODE}",
        key_group=mc.KEY_MODE,
        key_value=mc.KEY_WORKMODE,
        name=mc.KEY_WORKMODE,
    ),
    PresenceConfigMode.ENTITY_DEF(
        entity_key=f"presence_config_{mc.KEY_MODE}_{mc.KEY_TESTMODE}",
        key_group=mc.KEY_MODE,
        key_value=mc.KEY_TESTMODE,
        name=mc.KEY_TESTMODE,
    ),
    PresenceConfigNumber.ENTITY_DEF(
        entity_key=f"presence_config_{mc.KEY_NOBODYTIME}_{mc.KEY_TIME}",
        key_group=mc.KEY_NOBODYTIME,
        key_value=mc.KEY_TIME,
        name=mc.KEY_NOBODYTIME,
        device_class=NumberParser.DEVICE_CLASS_DURATION,
        native_max_value=3600,  # 1 hour ?
        native_min_value=1,
        native_step=1,
    ),
    PresenceConfigNumber.ENTITY_DEF(
        entity_key=f"presence_config_{mc.KEY_DISTANCE}_{mc.KEY_VALUE}",
        key_group=mc.KEY_DISTANCE,
        key_value=mc.KEY_VALUE,
        device_scale=1000,
        name=mc.KEY_DISTANCE,
        device_class=NumberParser.DeviceClass.DISTANCE,
        native_unit_of_measurement=hac.UnitOfLength.METERS,
        native_max_value=12,
        native_min_value=0.1,
        native_step=0.1,
    ),
    PresenceConfigSensitivity.ENTITY_DEF(
        entity_key=f"presence_config_{mc.KEY_SENSITIVITY}_{mc.KEY_LEVEL}",
        key_group=mc.KEY_SENSITIVITY,
        key_value=mc.KEY_LEVEL,
        name=mc.KEY_SENSITIVITY,
    ),
    PresenceConfigMthX.ENTITY_DEF(
        entity_key=f"presence_config_{mc.KEY_MTHX}_{mc.KEY_MTH1}",
        key_value=mc.KEY_MTH1,
        name=mc.KEY_MTH1,
    ),
    PresenceConfigMthX.ENTITY_DEF(
        entity_key=f"presence_config_{mc.KEY_MTHX}_{mc.KEY_MTH2}",
        key_value=mc.KEY_MTH2,
        name=mc.KEY_MTH2,
    ),
    PresenceConfigMthX.ENTITY_DEF(
        entity_key=f"presence_config_{mc.KEY_MTHX}_{mc.KEY_MTH3}",
        key_value=mc.KEY_MTH3,
        name=mc.KEY_MTH3,
    ),
)


def namespace_init_presence_config(ns: mn.Namespace, device: "Device", /):
    """Helper to register a specialized entity class to the proper namespace.
    This is going to be used on Device initialization for various entities sharing
    common semantics in namespace parsing/handling."""
    device._create_handler(ns).register_parsers(
        *(
            entity_def(
                0,
                device,
                ns=ns,
            )
            for entity_def in ENTITY_DEFS
        )
    )


class PresenceSensor(SensorParser):
    """ms600 presence sensor."""

    init_entity_key = "sensor_presence"

    _attr_name = "Presence"

    __slots__ = (
        "sensor_distance",
        "binary_sensor_motion",
        "sensor_times",
    )

    def __init__(
        self,
        channel: "ChannelType | None",
        device: "Device",
        /,
        **kwargs: "Unpack[SensorParser.Args]",
    ):
        SensorParser.__init__(self, channel, device, **kwargs)
        self.sensor_distance = SensorParser(
            channel,
            device,
            entity_key=f"{self.entity_key}_distance",
            device_scale=1000,
            device_class=SensorParser.DeviceClass.DISTANCE,
            native_unit_of_measurement=hac.UnitOfLength.METERS,
            suggested_display_precision=2,
            name="Presence distance",
        )
        self.binary_sensor_motion = BinarySensorEntity(
            channel,
            device,
            entity_key=f"{self.entity_key}_motion",
            device_class=BinarySensorEntity.DeviceClass.MOTION,
        )
        self.sensor_times = SensorParser(
            channel,
            device,
            entity_key=f"{self.entity_key}_times",
            name="Presence times",
        )

    @override
    def _parse(self, payload: dict, /):
        """
        {"times": 0, "distance": 760, "value": 2, "timestamp": 1725907895}
        """
        self.ns_payload = payload
        self.update_device_value(payload[mc.KEY_VALUE])
        self.sensor_distance.update_device_value(payload[mc.KEY_DISTANCE])
        self.binary_sensor_motion.update_boolean_value(payload[mc.KEY_VALUE] == 2)
        self.sensor_times.update_device_value(payload[mc.KEY_TIMES])
