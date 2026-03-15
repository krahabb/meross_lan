from functools import cached_property
from typing import TYPE_CHECKING, override

from homeassistant.components import sensor

from . import const as mlc
from .helpers import entity as mle
from .merossclient.client import Transport
from .merossclient.protocol import const as mc, namespaces as mn
from .merossclient.protocol.message import json_dumps

if TYPE_CHECKING:
    from typing import (
        Callable,
        ClassVar,
        Final,
        Never,
        NotRequired,
        Protocol,
        Self,
        Unpack,
    )

    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

    from .helpers.device import BaseDevice, Device
    from .helpers.entity import ChannelType
    from .helpers.manager import EntityManager
    from .helpers.mqtt_profile import MQTTConnection
    from .merossclient.client import AbstractClient


async def async_setup_entry(
    hass: "HomeAssistant", config_entry: "ConfigEntry", async_add_devices
):
    mle.Entity.platform_setup_entry(
        hass, config_entry, async_add_devices, sensor.DOMAIN
    )


class SensorEntity(mle.NumericEntity, sensor.SensorEntity):
    """Base wrapper around HA core SensorEntity."""

    if TYPE_CHECKING:

        DEVICE_CLASS_TEMPERATURE_DELTA: Final[sensor.SensorDeviceClass]
        # HA core entity attributes:
        _attr_device_class: ClassVar[sensor.SensorDeviceClass | None]
        _attr_suggested_display_precision: ClassVar[int | None]
        device_class: sensor.SensorDeviceClass | None

        class Args(mle.NumericEntity.Args):
            device_class: NotRequired[sensor.SensorDeviceClass | None]  # Override
            state_class: NotRequired[sensor.SensorStateClass | None]
            suggested_display_precision: NotRequired[int]

        def __init__(
            self,
            channel: ChannelType | None,
            parent: EntityManager,
            /,
            **kwargs: Unpack[Args],
        ): ...

    PLATFORM = sensor.DOMAIN
    HA_ENTITY_ATTRIBUTES = mle.NumericEntity.HA_ENTITY_ATTRIBUTES + (
        "state_class",
        "suggested_display_precision",
    )
    DeviceClass = sensor.SensorDeviceClass
    StateClass = sensor.SensorStateClass

    # HA core compatibility layer for NumberDeviceClass.TEMPERATURE_DELTA (HA core 2025.10 misses that)
    DEVICE_CLASS_TEMPERATURE_DELTA = getattr(
        DeviceClass, "TEMPERATURE_DELTA", DeviceClass.TEMPERATURE
    )

    DEVICECLASS_TO_UNIT_MAP = {
        DeviceClass.POWER: mlc.hac.UnitOfPower.WATT,
        DeviceClass.CURRENT: mlc.hac.UnitOfElectricCurrent.AMPERE,
        DeviceClass.VOLTAGE: mlc.hac.UnitOfElectricPotential.VOLT,
        DeviceClass.ENERGY: mlc.hac.UnitOfEnergy.WATT_HOUR,
        DeviceClass.TEMPERATURE: mlc.hac.UnitOfTemperature.CELSIUS,
        DEVICE_CLASS_TEMPERATURE_DELTA: mlc.hac.UnitOfTemperature.CELSIUS,
        DeviceClass.HUMIDITY: mlc.hac.PERCENTAGE,
        DeviceClass.BATTERY: mlc.hac.PERCENTAGE,
        DeviceClass.ILLUMINANCE: mlc.hac.LIGHT_LUX,
        DeviceClass.ENUM: None,
    }

    # we basically default Sensor.state_class to SensorStateClass.MEASUREMENT
    # except these device classes
    DEVICECLASS_TO_STATECLASS_MAP: dict[DeviceClass | None, StateClass | None] = {
        DeviceClass.ENERGY: StateClass.TOTAL_INCREASING,
        DeviceClass.ENUM: None,
    }

    @cached_property
    def state_class(self):
        try:
            return self._attr_state_class
        except AttributeError:
            pass
        try:
            return self.DEVICECLASS_TO_STATECLASS_MAP[self.device_class]
        except KeyError:
            return sensor.SensorStateClass.MEASUREMENT if self.device_class else None


class EnumParser(mle.ValueParser, SensorEntity):
    """Specialized class for enum sensors bound to a namespace parser."""

    if TYPE_CHECKING:

        _attr_device_class: Final[sensor.SensorDeviceClass]
        _attr_state_class: Final[None]
        native_value: sensor.StateType

        # class Args(mle.Entity.Args):
        class Args(SensorEntity.Args, mle.ValueParser.Args):
            native_value: NotRequired[sensor.StateType]
            device_class: NotRequired[Never]

        def __init__(
            self,
            channel: ChannelType | None,
            parent: BaseDevice,
            /,
            **kwargs: Unpack[Args],
        ): ...

        @classmethod
        def ENTITY_DEF(cls, **kwargs: Unpack[Args]) -> type[Self]: ...

        def update_native_value(
            self, native_value: sensor.StateType, /
        ) -> bool | None: ...

    # HA core entity attributes:
    _attr_device_class = sensor.SensorDeviceClass.ENUM
    _attr_state_class = None

    @override
    def update_device_value(self, device_value: sensor.StateType, /):
        if self.device_value != device_value:
            self.device_value = device_value
            self.native_value = device_value
            self.flush_state()
            return True

    update_native_value = update_device_value


class SensorParser(mle.NumericParser, SensorEntity):
    """Specialized class for numeric sensors bound to a namespace parser."""

    if TYPE_CHECKING:

        class Args(SensorEntity.Args, mle.NumericParser.Args):
            pass

        class Initializer(Protocol):
            def __call__(
                self,
                channel: ChannelType | None,
                parent: BaseDevice,
                /,
                **kwargs: Unpack["SensorParser.Args"],
            ) -> "SensorParser": ...

        def __init__(
            self,
            channel: ChannelType | None,
            parent: BaseDevice,
            /,
            **kwargs: Unpack[Args],
        ): ...

        @classmethod
        def ENTITY_DEF(cls, **kwargs: Unpack[Args]) -> type[Self]: ...

    HUMIDITY_ARGS: "Args" = {
        "entity_key": mc.KEY_HUMIDITY,
        "device_scale": 10,  # almost always valid
        "device_class": SensorEntity.DeviceClass.HUMIDITY,
        "suggested_display_precision": 1,
    }

    @classmethod
    def Humidity(
        cls,
        channel: "ChannelType | None",
        manager: "BaseDevice",
        **kwargs: "Unpack[Args]",
    ) -> "Self":
        return cls(channel, manager, **(cls.HUMIDITY_ARGS | kwargs))

    LIGHT_ARGS: "Args" = {
        "entity_key": mc.KEY_LIGHT,
        "device_class": SensorEntity.DeviceClass.ILLUMINANCE,
        "suggested_display_precision": 0,
    }

    @classmethod
    def Light(
        cls,
        channel: "ChannelType | None",
        manager: "BaseDevice",
        **kwargs: "Unpack[Args]",
    ) -> "Self":
        return cls(channel, manager, **(cls.LIGHT_ARGS | kwargs))

    TEMPERATURE_ARGS: "Args" = {
        "entity_key": mc.KEY_TEMPERATURE,
        "device_scale": 10,  # just a default - sometimes 100 or 1000
        "device_class": SensorEntity.DeviceClass.TEMPERATURE,
        "suggested_display_precision": 1,
    }

    @classmethod
    def Temperature(
        cls,
        channel: "ChannelType | None",
        manager: "BaseDevice",
        **kwargs: "Unpack[Args]",
    ) -> "Self":
        return cls(channel, manager, **(cls.TEMPERATURE_ARGS | kwargs))


class DiagnosticSensor(SensorEntity):

    if TYPE_CHECKING:
        is_diagnostic: Final
        native_value: sensor.StateType

        class Args(mle.Entity.Args):
            native_value: NotRequired[sensor.StateType]

        def __init__(
            self,
            channel: ChannelType | None,
            parent: EntityManager,
            /,
            **kwargs: Unpack[Args],
        ): ...

        @classmethod
        def ENTITY_DEF(cls, **kwargs: Unpack[Args]) -> type[Self]: ...

        def update_native_value(
            self, native_value: sensor.StateType, /
        ) -> bool | None: ...

    is_diagnostic = True

    # HA core entity attributes:
    _attr_entity_category = SensorParser.EntityCategory.DIAGNOSTIC
    _attr_state_class = None


class DiagnosticParser(mle.ValueParser, DiagnosticSensor):
    """
    This is a specialization of DiagnosticSensor which is also a ParserEntity, so that it can be
    easily registered in NamespaceHandler to parse the whole payload of an unexpected namespace and
    store it as-is in the state of this sensor.
    """

    @override
    def _parse(self, payload: dict):
        """
        This implementation aims at diagnostic sensors installed in 'well-known'
        namespace handlers to manage 'unexpected' channels when they eventually
        pop-up and we (still) have no clue why these channels are pushed (See #428)
        """
        self.update_device_value(json_dumps(payload))


class ProtocolSensor(SensorEntity):

    if TYPE_CHECKING:
        parent: Final[Device]  # type: ignore[override]
        native_value: str

    init_entity_key = "sensor_protocol"

    STATE_DISCONNECTED = "disconnected"
    STATE_ACTIVE = "active"
    STATE_INACTIVE = "inactive"
    ATTR_MQTT_BROKER = "mqtt_broker"

    # HA core entity attributes:
    _attr_available = True
    _attr_device_class = sensor.SensorDeviceClass.ENUM
    _attr_entity_category = SensorEntity.EntityCategory.DIAGNOSTIC
    _attr_entity_registry_enabled_default = False
    _attr_state_class = None

    options: list[str] = [
        STATE_DISCONNECTED,
        Transport.BLUETOOTH,
        Transport.HTTP,
        Transport.MQTT,
    ]

    def __init__(self, parent: "Device"):
        self.extra_state_attributes = {}
        super().__init__(None, parent, native_value=ProtocolSensor.STATE_DISCONNECTED)  # type: ignore

    def set_available(self):
        self.native_value = self.parent.transport
        self.flush_state()

    def set_unavailable(self):
        self.native_value = self.STATE_DISCONNECTED
        self.flush_state()

    # callbacks from Device._clients connect/disconnect events
    def on_client_add(self, client: "AbstractClient", /):
        client.connect_broadcast.add(self.on_client_connect)
        client.disconnect_broadcast.add(self.on_client_disconnect)
        self.extra_state_attributes[client.TRANSPORT] = (
            ProtocolSensor.STATE_ACTIVE
            if client and client.is_connected
            else ProtocolSensor.STATE_INACTIVE
        )

        if client.TRANSPORT is Transport.MQTT:
            connection: "MQTTConnection" = client.connection  # type: ignore
            connection.connect_broadcast.add(self.on_broker_connect)
            connection.disconnect_broadcast.add(self.on_broker_disconnect)
            self.extra_state_attributes[self.ATTR_MQTT_BROKER] = (
                ProtocolSensor.STATE_ACTIVE
                if connection and connection.is_connected
                else ProtocolSensor.STATE_INACTIVE
            )
            if sensor := connection.sensor_connection:
                sensor.update_devices()
        self.schedule_flush_state()

    def on_client_remove(self, client: "AbstractClient", /):
        client.connect_broadcast.remove(self.on_client_connect)
        client.disconnect_broadcast.remove(self.on_client_disconnect)
        self.extra_state_attributes.pop(client.TRANSPORT)
        if client.TRANSPORT is Transport.MQTT:
            connection: "MQTTConnection" = client.connection  # type: ignore
            connection.connect_broadcast.remove(self.on_broker_connect)
            connection.disconnect_broadcast.remove(self.on_broker_disconnect)
            self.extra_state_attributes.pop(self.ATTR_MQTT_BROKER)
            if sensor := connection.sensor_connection:
                sensor.update_devices()
        self.schedule_flush_state()

    def on_client_connect(self, client: "AbstractClient", /):
        self.extra_state_attributes[client.TRANSPORT] = self.STATE_ACTIVE
        self.schedule_flush_state()

    def on_client_disconnect(self, client: "AbstractClient", /):
        self.extra_state_attributes[client.TRANSPORT] = self.STATE_INACTIVE
        self.schedule_flush_state()

    def on_broker_connect(self, connection: "MQTTConnection", /):
        self.extra_state_attributes[self.ATTR_MQTT_BROKER] = self.STATE_ACTIVE
        self.schedule_flush_state()

    def on_broker_disconnect(self, connection: "MQTTConnection", /):
        self.extra_state_attributes[self.ATTR_MQTT_BROKER] = self.STATE_INACTIVE
        self.schedule_flush_state()


class SignalStrengthSensor(mle.EntityNamespaceMixin, SensorParser):

    POLLING_CONFIG_DEFAULT = mle.EntityNamespaceMixin.POLLING_CONFIG_SLOWSENSOR_NS

    init_entity_key = "signal_strength"
    init_key_value = mc.KEY_SIGNAL
    # HA core entity attributes:
    _attr_entity_category = SensorParser.EntityCategory.DIAGNOSTIC
    _attr_native_unit_of_measurement = mlc.hac.PERCENTAGE
    _attr_icon = "mdi:wifi"


class FilterMaintenanceSensor(SensorParser):

    NS_CHANNELS = SensorParser.NS_CHANNELS_SINGLE
    init_entity_key = mc.KEY_FILTER
    init_key_value = mc.KEY_LIFE

    # HA core entity attributes:
    _attr_entity_category = SensorParser.EntityCategory.DIAGNOSTIC
    _attr_native_unit_of_measurement = mlc.hac.PERCENTAGE
