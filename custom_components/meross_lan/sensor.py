from typing import TYPE_CHECKING, override

from homeassistant.components import sensor

from . import const as mlc
from .helpers import entity as mle
from .merossclient.client import Transport
from .merossclient.protocol import const as mc, namespaces as mn
from .merossclient.protocol.message import json_dumps

if TYPE_CHECKING:
    from typing import ClassVar, Final, Never, NotRequired, Unpack

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


class Sensor(mle.NumericEntity, sensor.SensorEntity):

    if TYPE_CHECKING:

        class Args(mle.NumericEntity.Args):
            device_class: NotRequired[sensor.SensorDeviceClass | None]
            state_class: NotRequired[sensor.SensorStateClass]
            suggested_display_precision: NotRequired[int]

        DEVICE_CLASS_TEMPERATURE_DELTA: Final[sensor.SensorDeviceClass]
        # HA core entity attributes:
        _attr_device_class: ClassVar[sensor.SensorDeviceClass | None]
        _attr_suggested_display_precision: ClassVar[int | None]
        device_class: sensor.SensorDeviceClass | None
        state_class: sensor.SensorStateClass
        suggested_display_precision: int | None

    PLATFORM = sensor.DOMAIN
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
    DEVICECLASS_TO_STATECLASS_MAP: dict[DeviceClass | None, StateClass] = {
        None: StateClass.MEASUREMENT,
        DeviceClass.ENERGY: StateClass.TOTAL_INCREASING,
    }

    # HA core entity attributes:
    _attr_suggested_display_precision = None

    __SLOTS__ = (
        "state_class",
        "suggested_display_precision",
    )

    def __init__(
        self,
        channel: "ChannelType | None",
        parent: "EntityManager",
        /,
        **kwargs: "Unpack[Args]",
    ):
        try:
            self.state_class = kwargs["state_class"]  # type: ignore
        except KeyError:
            self.state_class = self.DEVICECLASS_TO_STATECLASS_MAP.get(
                kwargs.get("device_class", self._attr_device_class),
                sensor.SensorStateClass.MEASUREMENT,
            )
        try:
            self.suggested_display_precision = kwargs["suggested_display_precision"]  # type: ignore
        except KeyError:
            self.suggested_display_precision = self._attr_suggested_display_precision
        super().__init__(channel, parent, **kwargs)


class EnumSensor(mle.ValueParser, Sensor):
    """Specialization for sensor with ENUM device_class which allows to store
    anything as opposed to numeric sensor types which have units and so."""

    if TYPE_CHECKING:

        class Args(mle.Entity.Args):
            native_value: NotRequired[sensor.StateType]
            device_class: NotRequired[Never]

        @classmethod
        def ENTITY_DEF(
            cls,
            **kwargs: "Unpack[EnumSensor.Args]",
        ) -> "EnumSensor.EntityDef[EnumSensor]":  # type: ignore[override]
            pass

        _attr_device_class: Final[sensor.SensorDeviceClass]
        native_value: sensor.StateType

        def update_native_value(
            self, native_value: sensor.StateType, /
        ) -> bool | None: ...

    # HA core entity attributes:
    _attr_device_class = sensor.SensorDeviceClass.ENUM

    def __init__(
        self,
        channel: "ChannelType | None",
        parent: "BaseDevice",
        /,
        **kwargs: "Unpack[Args]",
    ):
        super().__init__(channel, parent, **kwargs)

    @override
    def update_device_value(self, device_value: sensor.StateType, /):
        if self.device_value != device_value:
            self.device_value = device_value
            self.native_value = device_value
            self.flush_state()
            return True

    update_native_value = update_device_value


# TODO rename to ParserSensor
class NumericSensor(mle.NumericParser, Sensor):

    if TYPE_CHECKING:

        class Args(Sensor.Args, mle.NumericParser.Args):
            pass

        def __init__(
            self,
            channel: "ChannelType | None",
            parent: "BaseDevice",
            /,
            **kwargs: "Unpack[Args]",
        ): ...

        @classmethod
        def ENTITY_DEF(
            cls,
            **kwargs: "Unpack[NumericSensor.Args]",
        ) -> "NumericSensor.EntityDef[NumericSensor]":  # type: ignore[override]
            pass


class HumiditySensor(NumericSensor):
    """Specialization for Humidity sensor.
    - device_scale defaults to 10 which is actually the only scale seen so far.
    - suggested_display_precision defaults to 1
    """

    ENTITY_KEY = mc.KEY_HUMIDITY

    _attr_device_scale = 10
    # HA core entity attributes:
    _attr_device_class = sensor.SensorDeviceClass.HUMIDITY
    _attr_suggested_display_precision = 1


class TemperatureSensor(NumericSensor):
    """Specialization for Temperature sensor.
    - device_scale defaults to 1 (from base class definition) and is likely to be overriden.
    - suggested_display_precision defaults to 1
    """

    ENTITY_KEY = mc.KEY_TEMPERATURE

    # HA core entity attributes:
    _attr_device_class = sensor.SensorDeviceClass.TEMPERATURE
    _attr_suggested_display_precision = 1


class LightSensor(NumericSensor):
    """Specialization for sensor reporting light illuminance (lux)."""

    ENTITY_KEY = mc.KEY_LIGHT

    _attr_device_scale = 1
    # HA core entity attributes:
    _attr_device_class = sensor.SensorDeviceClass.ILLUMINANCE
    _attr_suggested_display_precision = 0


class DiagnosticSensor(EnumSensor):

    if TYPE_CHECKING:
        is_diagnostic: Final

    is_diagnostic = True

    # HA core entity attributes:
    entity_category = NumericSensor.EntityCategory.DIAGNOSTIC

    def _parse(self, payload: dict):
        """
        This implementation aims at diagnostic sensors installed in 'well-known'
        namespace handlers to manage 'unexpected' channels when they eventually
        pop-up and we (still) have no clue why these channels are pushed (See #428)
        """
        self.update_device_value(json_dumps(payload))


class ProtocolSensor(EnumSensor):

    if TYPE_CHECKING:
        parent: Final["Device"]  # type: ignore[override]
        native_value: str

    ENTITY_KEY = "sensor_protocol"

    STATE_DISCONNECTED = "disconnected"
    STATE_ACTIVE = "active"
    STATE_INACTIVE = "inactive"
    ATTR_MQTT_BROKER = "mqtt_broker"

    # HA core entity attributes:
    _attr_available = True
    _attr_entity_registry_enabled_default = False
    entity_category = EnumSensor.EntityCategory.DIAGNOSTIC
    options: list[str] = [
        STATE_DISCONNECTED,
        Transport.BLUETOOTH,
        Transport.HTTP,
        Transport.MQTT,
    ]

    @staticmethod
    def _get_attr_state(value):
        return ProtocolSensor.STATE_ACTIVE if value else ProtocolSensor.STATE_INACTIVE

    @staticmethod
    def _get_client_attr_state(client: "AbstractClient | None"):
        return (
            ProtocolSensor.STATE_ACTIVE
            if client and client.is_connected
            else ProtocolSensor.STATE_INACTIVE
        )

    def __init__(self, parent: "Device"):
        self.extra_state_attributes = {}
        super().__init__(None, parent, native_value=ProtocolSensor.STATE_DISCONNECTED)

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
        self.extra_state_attributes[client.TRANSPORT] = self._get_client_attr_state(
            client
        )
        if client.TRANSPORT is Transport.MQTT:
            connection: "MQTTConnection" = client.connection  # type: ignore
            connection.connect_broadcast.add(self.on_broker_connect)
            connection.disconnect_broadcast.add(self.on_broker_disconnect)
            self.extra_state_attributes[self.ATTR_MQTT_BROKER] = (
                self._get_client_attr_state(connection)
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


class SignalStrengthSensor(mle.EntityNamespaceMixin, NumericSensor):

    DEFAULT_CONFIG = (
        mlc.PARAM_SENSOR_SLOW_UPDATE_PERIOD,
        mlc.PARAM_CLOUD_UPDATE_PERIOD,
        mle.EntityNamespaceMixin.async_poll_smart,
    )
    ENTITY_KEY = "signal_strength"
    ns = mn.Appliance_System_Runtime
    key_value = mc.KEY_SIGNAL
    # HA core entity attributes:
    _attr_native_unit_of_measurement = mlc.hac.PERCENTAGE
    entity_category = NumericSensor.EntityCategory.DIAGNOSTIC
    icon = "mdi:wifi"


class FilterMaintenanceSensor(NumericSensor):

    ENTITY_KEY = mc.KEY_FILTER
    ns = mn.Appliance_Control_FilterMaintenance
    NS_CHANNELS = NumericSensor.NS_CHANNELS_SINGLE
    key_value = mc.KEY_LIFE

    # HA core entity attributes:
    _attr_native_unit_of_measurement = mlc.hac.PERCENTAGE
    entity_category = NumericSensor.EntityCategory.DIAGNOSTIC

    def __init__(self, channel: int, parent: "Device", /):
        NumericSensor.__init__(self, channel, parent)
        parent.register_parser_entity(self)
