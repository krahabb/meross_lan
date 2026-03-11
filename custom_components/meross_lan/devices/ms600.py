from typing import TYPE_CHECKING, override

from ..binary_sensor import BinarySensor
from ..const import hac
from ..helpers.namespaces import mc, mn
from ..number import NumberParser
from ..select import SelectParser
from ..sensor import SensorParser

if TYPE_CHECKING:
    from typing import Final, Unpack

    from ..helpers.device import BaseDevice
    from ..helpers.entity import ChannelType


class PresenceConfigBase(SelectParser.NamespaceGroupValue):
    """Mixin style base class for all of the entities managed in Appliance.Control.Presence.Config"""

    ns = mn.Appliance_Control_Presence_Config

    # HA core entity attributes:
    _attr_entity_category = SelectParser.EntityCategory.CONFIG

    # TODO: generalize entitykey generation


class PresenceConfigNumberBase(PresenceConfigBase, NumberParser):
    """Base class for config values represented as Number entities in HA."""


class PresenceConfigSelectBase(PresenceConfigBase, SelectParser):
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

    def __init__(self, channel: "ChannelType | None", parent: "BaseDevice", key: str):
        self.key_value = key
        PresenceConfigSelectBase.__init__(
            self, channel, parent, entity_key=f"presence_config_mode_{key}", name=key
        )


class PresenceConfigNoBodyTime(PresenceConfigNumberBase):

    ENTITY_KEY = "presence_config_noBodyTime_time"

    key_group = mc.KEY_NOBODYTIME
    key_value = mc.KEY_TIME

    # HA core entity attributes:
    _attr_device_class = NumberParser.DEVICE_CLASS_DURATION
    _attr_name = mc.KEY_NOBODYTIME
    _attr_native_max_value = 3600  # 1 hour ?
    _attr_native_min_value = 1
    _attr_native_step = 1


class PresenceConfigDistance(PresenceConfigNumberBase):

    ENTITY_KEY = "presence_config_distance_value"

    key_group = mc.KEY_DISTANCE
    key_value = mc.KEY_VALUE

    _attr_name = mc.KEY_DISTANCE
    _attr_device_scale = 1000

    # HA core entity attributes:
    _attr_device_class = NumberParser.DeviceClass.DISTANCE
    _attr_native_unit_of_measurement = hac.UnitOfLength.METERS
    _attr_native_max_value = 12
    _attr_native_min_value = 0.1
    _attr_native_step = 0.1


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
    _attr_native_max_value = 1000
    _attr_native_min_value = 1
    _attr_native_step = 1

    def __init__(
        self, channel: "ChannelType | None", parent: "BaseDevice", key: str, /
    ):
        PresenceConfigNumberBase.__init__(
            self,
            channel,
            parent,
            entity_key=f"presence_config_mthx_{key}",
            name=key,
            key_value=key,
        )


class PresenceConfigMode(PresenceConfigModeBase):

    def __init__(self, channel: "ChannelType | None", device: "BaseDevice", /):
        PresenceConfigModeBase.__init__(self, channel, device, mc.KEY_WORKMODE)
        device.get_handler(mn.Appliance_Control_Presence_Config).register_parsers(
            self,
            PresenceConfigModeBase(channel, device, mc.KEY_TESTMODE),
            PresenceConfigNoBodyTime(channel, device),
            PresenceConfigDistance(channel, device),
            PresenceConfigSensitivity(channel, device),
            PresenceConfigMthX(channel, device, mc.KEY_MTH1),
            PresenceConfigMthX(channel, device, mc.KEY_MTH2),
            PresenceConfigMthX(channel, device, mc.KEY_MTH3),
        )


class PresenceSensor(SensorParser):
    """ms600 presence sensor."""

    if TYPE_CHECKING:
        # manager: "Device" pass
        pass

    ENTITY_KEY = "sensor_presence"

    _attr_name = "Presence"

    __slots__ = (
        "sensor_distance",
        "binary_sensor_motion",
        "sensor_times",
    )

    def __init__(
        self,
        channel: "ChannelType | None",
        device: "BaseDevice",
        /,
        **kwargs: "Unpack[SensorParser.Args]",
    ):
        SensorParser.__init__(self, channel, device, **kwargs)
        self.sensor_distance = SensorParser(
            channel,
            device,
            entity_key=f"{self.entitykey}_distance",
            device_scale=1000,
            device_class=SensorParser.DeviceClass.DISTANCE,
            native_unit_of_measurement=hac.UnitOfLength.METERS,
            suggested_display_precision=2,
            name="Presence distance",
        )
        self.binary_sensor_motion = BinarySensor(
            channel,
            device,
            entity_key=f"{self.entitykey}_motion",
            device_class=BinarySensor.DeviceClass.MOTION,
        )
        self.sensor_times = SensorParser(
            channel,
            device,
            entity_key=f"{self.entitykey}_times",
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
