import typing


from ..helpers.namespaces import NamespaceHandler
from ..merossclient import const as mc, namespaces as mn
from ..number import MLConfigNumber
from ..select import MLConfigSelect
from ..sensor import MLNumericSensor

if typing.TYPE_CHECKING:
    from ..meross_device import MerossDevice
    from ..meross_entity import MerossEntity
    from ..sensor import MLNumericSensorArgs


class PresenceConfigBase(MerossEntity if typing.TYPE_CHECKING else object):
    """Mixin style base class for all of the entities managed in Appliance.Control.Presence.Config"""

    manager: "MerossDevice"

    ns = mn.Appliance_Control_Presence_Config

    key_value_root: str

    async def async_request_value(self, device_value):
        ns = self.ns
        return await self.manager.async_request_ack(
            ns.name,
            mc.METHOD_SET,
            {
                ns.key: [
                    {
                        ns.key_channel: self.channel,
                        self.key_value_root: {self.key_value: device_value},
                    }
                ]
            },
        )


class PresenceConfigNumberBase(PresenceConfigBase, MLConfigNumber):
    """Base class for config values represented as Number entities in HA."""


class PresenceConfigSelectBase(PresenceConfigBase, MLConfigSelect):
    """Base class for config values represented as Select entities in HA."""


class PresenceConfigModeBase(PresenceConfigSelectBase):

    key_value_root = mc.KEY_MODE

    # TODO: configure real labels
    # This map would actually be shared between workMode and testMode though
    OPTIONS_MAP = {
        0: "0",
        1: "1",
        2: "2",
    }

    def __init__(self, manager: "MerossDevice", channel: object, key: str):
        self.key_value = key
        super().__init__(manager, channel, f"presence_config_mode_{key}")


class PresenceConfigNoBodyTime(PresenceConfigNumberBase):

    key_value_root = mc.KEY_NOBODYTIME
    key_value = mc.KEY_TIME

    # HA core entity attributes:
    native_max_value = 3600  # 1 hour ?
    native_min_value = 1
    native_step = 1

    def __init__(self, manager: "MerossDevice", channel: object):
        super().__init__(
            manager,
            channel,
            f"presence_config_noBodyTime_time",
            MLConfigNumber.DEVICE_CLASS_DURATION,  # defaults to seconds which is the native device unit
        )


class PresenceConfigDistance(PresenceConfigNumberBase):

    key_value_root = mc.KEY_DISTANCE
    key_value = mc.KEY_VALUE

    # HA core entity attributes:
    native_max_value = 12
    native_min_value = 0.1
    native_step = 0.1

    def __init__(self, manager: "MerossDevice", channel: object):
        super().__init__(
            manager,
            channel,
            f"presence_config_distance_value",
            MLConfigNumber.DeviceClass.DISTANCE,
            device_scale=1000,
            native_unit_of_measurement=MLConfigNumber.hac.UnitOfLength.METERS,
        )


class PresenceConfigSensitivity(PresenceConfigSelectBase):

    key_value_root = mc.KEY_SENSITIVITY
    key_value = mc.KEY_LEVEL

    # TODO: configure real labels
    OPTIONS_MAP = {
        0: "0",
        1: "1",
        2: "2",
    }

    def __init__(self, manager: "MerossDevice", channel: object):
        super().__init__(
            manager,
            channel,
            f"presence_config_sensitivity_level",
        )


class PresenceConfigMthX(PresenceConfigNumberBase):
    key_value_root = mc.KEY_MTHX
    # HA core entity attributes:
    native_max_value = 1000
    native_min_value = 1
    native_step = 1

    def __init__(self, manager: "MerossDevice", channel: object, key: str):
        self.key_value = key
        super().__init__(
            manager,
            channel,
            f"presence_config_mthx_{key}",
            None,
        )


class PresenceConfigMode(PresenceConfigModeBase):

    _entities: tuple[PresenceConfigBase, ...]

    __slots__ = ("_entities",)

    def __init__(self, manager: "MerossDevice", channel: object):
        super().__init__(manager, channel, mc.KEY_WORKMODE)
        self._entities = (
            self,
            PresenceConfigModeBase(manager, channel, mc.KEY_TESTMODE),
            PresenceConfigNoBodyTime(manager, channel),
            PresenceConfigDistance(manager, channel),
            PresenceConfigSensitivity(manager, channel),
            PresenceConfigMthX(manager, channel, mc.KEY_MTH1),
            PresenceConfigMthX(manager, channel, mc.KEY_MTH2),
            PresenceConfigMthX(manager, channel, mc.KEY_MTH3),
        )
        manager.register_parser_entity(self)

    async def async_shutdown(self):
        await super().async_shutdown()
        self._entities = None  # type: ignore

    def _parse_config(self, payload: dict):
        """
        {
            "channel": 0,
            "mode": {"workMode": 1,"testMode": 2},
            "noBodyTime": {"time": 15},
            "distance": {"value": 8100},
            "sensitivity": {"level": 2},
            "mthx": {"mth1": 120,"mth2": 72,"mth3": 72}
        }
        """
        for entity in self._entities:
            entity.update_device_value(payload[entity.key_value_root][entity.key_value])


def namespace_init_presence_config(device: "MerossDevice"):
    NamespaceHandler(
        device, mn.Appliance_Control_Presence_Config
    ).register_entity_class(PresenceConfigMode)
    PresenceConfigMode(device, 0)  # this will auto register itself in handler


class MLPresenceSensor(MLNumericSensor):
    """ms600 presence sensor."""

    manager: "MerossDevice"

    __slots__ = (
        "sensor_distance",
        "sensor_times",
    )

    def __init__(
        self,
        manager: "MerossDevice",
        channel: object | None,
        entitykey: str | None,
        **kwargs: "typing.Unpack[MLNumericSensorArgs]",
    ):
        super().__init__(manager, channel, entitykey, None, **kwargs)
        self.sensor_distance = MLNumericSensor(
            manager,
            channel,
            f"{entitykey}_distance",
            MLNumericSensor.DeviceClass.DISTANCE,
            device_scale=1000,
            native_unit_of_measurement=MLNumericSensor.hac.UnitOfLength.METERS,
            suggested_display_precision=2,
        )
        self.sensor_times = MLNumericSensor(manager, channel, f"{entitykey}_times")

    async def async_shutdown(self):
        await super().async_shutdown()
        self.sensor_times: MLNumericSensor = None  # type: ignore
        self.sensor_distance: MLNumericSensor = None  # type: ignore

    def _parse(self, payload: dict):
        """
        {"times": 0, "distance": 760, "value": 2, "timestamp": 1725907895}
        """
        self.update_device_value(payload[mc.KEY_VALUE])
        self.sensor_distance.update_device_value(payload[mc.KEY_DISTANCE])
        self.sensor_times.update_device_value(payload[mc.KEY_TIMES])
