from typing import TYPE_CHECKING

from ..binary_sensor import MLBinarySensor
from ..const import hac
from ..helpers import entity as me
from ..helpers.namespaces import mc, mn
from ..number import MLConfigNumber
from ..select import MLConfigSelect
from ..sensor import MLNumericSensor

if TYPE_CHECKING:
    from typing import Final, Unpack

    from ..helpers.device import Device


class PresenceConfigBase(me.MEGroupListChannelMixin):
    """Mixin style base class for all of the entities managed in Appliance.Control.Presence.Config"""

    if TYPE_CHECKING:
        ns: Final
        entity_category: Final

    ns = mn.Appliance_Control_Presence_Config

    # HA core entity attributes:
    entity_category = me.MLEntity.EntityCategory.CONFIG

    # TODO: generalize entitykey generation


class PresenceConfigNumberBase(PresenceConfigBase, MLConfigNumber):
    """Base class for config values represented as Number entities in HA."""


class PresenceConfigSelectBase(PresenceConfigBase, MLConfigSelect):
    """Base class for config values represented as Select entities in HA."""


class PresenceConfigModeBase(PresenceConfigSelectBase):

    key_group = mc.KEY_MODE

    # TODO: configure real labels
    # This map would actually be shared between workMode and testMode though
    OPTIONS_MAP = {
        0: "0",
        1: "1",
        2: "2",
    }

    def __init__(self, channel: "me.ChannelType", manager: "Device", key: str):
        self.key_value = key
        PresenceConfigSelectBase.__init__(
            self, channel, manager, entity_key=f"presence_config_mode_{key}", name=key
        )


class PresenceConfigNoBodyTime(PresenceConfigNumberBase):

    ENTITY_KEY = "presence_config_noBodyTime_time"

    key_group = mc.KEY_NOBODYTIME
    key_value = mc.KEY_TIME

    # HA core entity attributes:
    _attr_name = mc.KEY_NOBODYTIME
    _attr_device_class = MLConfigNumber.DEVICE_CLASS_DURATION
    native_max_value = 3600  # 1 hour ?
    native_min_value = 1
    native_step = 1


class PresenceConfigDistance(PresenceConfigNumberBase):

    ENTITY_KEY = "presence_config_distance_value"

    key_group = mc.KEY_DISTANCE
    key_value = mc.KEY_VALUE

    _attr_name = mc.KEY_DISTANCE
    _attr_device_scale = 1000

    # HA core entity attributes:
    _attr_device_class = MLConfigNumber.DeviceClass.DISTANCE
    _attr_native_unit_of_measurement = hac.UnitOfLength.METERS
    native_max_value = 12
    native_min_value = 0.1
    native_step = 0.1


class PresenceConfigSensitivity(PresenceConfigSelectBase):

    ENTITY_KEY = "presence_config_sensitivity_level"

    key_group = mc.KEY_SENSITIVITY
    key_value = mc.KEY_LEVEL

    _attr_name = mc.KEY_SENSITIVITY
    # TODO: configure real labels
    OPTIONS_MAP = {
        0: "0",
        1: "1",
        2: "2",
    }


class PresenceConfigMthX(PresenceConfigNumberBase):
    key_group = mc.KEY_MTHX
    # HA core entity attributes:
    native_max_value = 1000
    native_min_value = 1
    native_step = 1

    def __init__(self, channel: "me.ChannelType", manager: "Device", key: str, /):
        self.key_value = key
        PresenceConfigNumberBase.__init__(
            self, channel, manager, entity_key=f"presence_config_mthx_{key}", name=key
        )


class PresenceConfigMode(PresenceConfigModeBase):

    _entities: tuple[PresenceConfigBase, ...]

    def __init__(self, channel: "me.ChannelType", manager: "Device", /):
        PresenceConfigModeBase.__init__(self, channel, manager, mc.KEY_WORKMODE)
        manager.get_handler(mn.Appliance_Control_Presence_Config).register_parsers(
            self,
            PresenceConfigModeBase(channel, manager, mc.KEY_TESTMODE),
            PresenceConfigNoBodyTime(channel, manager),
            PresenceConfigDistance(channel, manager),
            PresenceConfigSensitivity(channel, manager),
            PresenceConfigMthX(channel, manager, mc.KEY_MTH1),
            PresenceConfigMthX(channel, manager, mc.KEY_MTH2),
            PresenceConfigMthX(channel, manager, mc.KEY_MTH3),
        )


class MLPresenceSensor(MLNumericSensor):
    """ms600 presence sensor."""

    if TYPE_CHECKING:
        manager: "Device"

    ENTITY_KEY = "sensor_presence"

    _attr_name = "Presence"

    __slots__ = (
        "sensor_distance",
        "binary_sensor_motion",
        "sensor_times",
    )

    def __init__(
        self,
        channel: "me.ChannelType",
        manager: "Device",
        /,
        **kwargs: "Unpack[MLNumericSensor.Args]",
    ):
        MLNumericSensor.__init__(self, channel, manager, **kwargs)
        self.sensor_distance = MLNumericSensor(
            channel,
            manager,
            entity_key=f"{self.entitykey}_distance",
            device_scale=1000,
            device_class=MLNumericSensor.DeviceClass.DISTANCE,
            native_unit_of_measurement=hac.UnitOfLength.METERS,
            suggested_display_precision=2,
            name="Presence distance",
        )
        self.binary_sensor_motion = MLBinarySensor(
            channel,
            manager,
            entity_key=f"{self.entitykey}_motion",
            device_class=MLBinarySensor.DeviceClass.MOTION,
        )
        self.sensor_times = MLNumericSensor(
            channel,
            manager,
            entity_key=f"{self.entitykey}_times",
            name="Presence times",
        )

    def _parse(self, payload: dict, /):
        """
        {"times": 0, "distance": 760, "value": 2, "timestamp": 1725907895}
        """
        self.update_device_value(payload[mc.KEY_VALUE])
        self.sensor_distance.update_device_value(payload[mc.KEY_DISTANCE])
        self.binary_sensor_motion.update_native_value(payload[mc.KEY_VALUE] == 2)
        self.sensor_times.update_device_value(payload[mc.KEY_TIMES])
