from typing import TYPE_CHECKING

from homeassistant.components import sensor

from . import const as mlc
from .helpers import entity as me
from .helpers.namespaces import EntityNamespaceMixin, NamespaceHandler, mc, mn
from .merossclient.protocol.message import json_dumps

if TYPE_CHECKING:
    from typing import ClassVar, Final, Never, NotRequired, Unpack

    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

    from .helpers.device import Device
    from .helpers.manager import EntityManager


async def async_setup_entry(
    hass: "HomeAssistant", config_entry: "ConfigEntry", async_add_devices
):
    me.platform_setup_entry(hass, config_entry, async_add_devices, sensor.DOMAIN)


class MLEnumSensor(me.MLEntity, sensor.SensorEntity):
    """Specialization for sensor with ENUM device_class which allows to store
    anything as opposed to numeric sensor types which have units and so."""

    if TYPE_CHECKING:

        class Args(me.MLEntity.Args):
            native_value: NotRequired[sensor.StateType]
            device_class: NotRequired[Never]

        @classmethod
        def ENTITY_DEF(
            cls,
            entitykey: str | None = None,
            **kwargs: "Unpack[MLEnumSensor.Args]",
        ) -> "MLEnumSensor.EntityDef[MLEnumSensor]":  # type: ignore[override]
            pass

        _attr_device_class: Final[sensor.SensorDeviceClass]
        native_value: sensor.StateType

    PLATFORM = sensor.DOMAIN

    # HA core entity attributes:
    _attr_device_class = sensor.SensorDeviceClass.ENUM

    __slots__ = ("native_value",)

    def __init__(
        self,
        manager: "EntityManager",
        channel: object | None,
        entitykey: str | None,
        **kwargs: "Unpack[Args]",
    ):
        self.native_value = kwargs.pop("native_value", None)
        super().__init__(manager, channel, entitykey, **kwargs)

    def set_unavailable(self):
        self.native_value = None
        super().set_unavailable()

    def update_device_value(self, device_value):
        if self.native_value != device_value:
            self.native_value = device_value
            self.flush_state()
            return True

    update_native_value = update_device_value


class MLNumericSensor(me.MLNumericEntity, sensor.SensorEntity):

    if TYPE_CHECKING:

        class Args(me.MLNumericEntity.Args):
            device_class: NotRequired[sensor.SensorDeviceClass | None]
            state_class: NotRequired[sensor.SensorStateClass]
            suggested_display_precision: NotRequired[int]

        @classmethod
        def ENTITY_DEF(
            cls,
            entitykey: str | None = None,
            **kwargs: "Unpack[MLNumericSensor.Args]",
        ) -> "MLNumericSensor.EntityDef[MLNumericSensor]":  # type: ignore[override]
            pass

        # HA core entity attributes:
        _attr_device_class: ClassVar[sensor.SensorDeviceClass | None]
        _attr_suggested_display_precision: ClassVar[int | None]
        suggested_display_precision: int | None

    PLATFORM = sensor.DOMAIN
    DeviceClass = sensor.SensorDeviceClass
    StateClass = sensor.SensorStateClass

    # HA core compatibility layer for NumberDeviceClass.TEMPERATURE_DELTA (HA core 2025.10 misses that)
    DEVICE_CLASS_TEMPERATURE_DELTA = getattr(
        DeviceClass, "TEMPERATURE_DELTA", DeviceClass.TEMPERATURE
    )

    DEVICECLASS_TO_UNIT_MAP = {
        DeviceClass.POWER: me.MLEntity.hac.UnitOfPower.WATT,
        DeviceClass.CURRENT: me.MLEntity.hac.UnitOfElectricCurrent.AMPERE,
        DeviceClass.VOLTAGE: me.MLEntity.hac.UnitOfElectricPotential.VOLT,
        DeviceClass.ENERGY: me.MLEntity.hac.UnitOfEnergy.WATT_HOUR,
        DeviceClass.TEMPERATURE: me.MLEntity.hac.UnitOfTemperature.CELSIUS,
        DEVICE_CLASS_TEMPERATURE_DELTA: me.MLEntity.hac.UnitOfTemperature.CELSIUS,
        DeviceClass.HUMIDITY: me.MLEntity.hac.PERCENTAGE,
        DeviceClass.BATTERY: me.MLEntity.hac.PERCENTAGE,
        DeviceClass.ILLUMINANCE: me.MLEntity.hac.LIGHT_LUX,
    }

    # we basically default Sensor.state_class to SensorStateClass.MEASUREMENT
    # except these device classes
    DEVICECLASS_TO_STATECLASS_MAP: dict[DeviceClass | None, StateClass] = {
        None: StateClass.MEASUREMENT,
        DeviceClass.ENERGY: StateClass.TOTAL_INCREASING,
    }

    # HA core entity attributes:
    state_class: StateClass
    _attr_suggested_display_precision = None

    __slots__ = (
        "state_class",
        "suggested_display_precision",
    )

    def __init__(
        self,
        manager: "EntityManager",
        channel: object | None,
        entitykey: str | None = None,
        **kwargs: "Unpack[Args]",
    ):
        self.state_class = kwargs.pop(
            "state_class", None
        ) or self.DEVICECLASS_TO_STATECLASS_MAP.get(
            kwargs.get("device_class", self._attr_device_class),
            MLNumericSensor.StateClass.MEASUREMENT,
        )
        self.suggested_display_precision = kwargs.pop(
            "suggested_display_precision", self._attr_suggested_display_precision
        )
        super().__init__(
            manager,
            channel,
            entitykey,
            **kwargs,
        )


class MLHumiditySensor(MLNumericSensor):
    """Specialization for Humidity sensor.
    - device_scale defaults to 10 which is actually the only scale seen so far.
    - suggested_display_precision defaults to 1
    """

    _attr_device_scale = 10
    # HA core entity attributes:
    _attr_device_class = sensor.SensorDeviceClass.HUMIDITY
    _attr_suggested_display_precision = 1

    def __init__(
        self,
        manager: "EntityManager",
        channel: object | None,
        entitykey: str | None = None,
        **kwargs: "Unpack[MLNumericSensor.Args]",
    ):
        super().__init__(
            manager,
            channel,
            entitykey or mc.KEY_HUMIDITY,
            **kwargs,
        )


class MLTemperatureSensor(MLNumericSensor):
    """Specialization for Temperature sensor.
    - device_scale defaults to 1 (from base class definition) and is likely to be overriden.
    - suggested_display_precision defaults to 1
    """

    # HA core entity attributes:
    _attr_device_class = sensor.SensorDeviceClass.TEMPERATURE
    _attr_suggested_display_precision = 1

    def __init__(
        self,
        manager: "EntityManager",
        channel: object | None,
        entitykey: str | None = None,
        **kwargs: "Unpack[MLNumericSensor.Args]",
    ):
        super().__init__(
            manager,
            channel,
            entitykey or mc.KEY_TEMPERATURE,
            **kwargs,
        )


class MLLightSensor(MLNumericSensor):
    """Specialization for sensor reporting light illuminance (lux)."""

    _attr_device_scale = 1
    # HA core entity attributes:
    _attr_device_class = sensor.SensorDeviceClass.ILLUMINANCE
    _attr_suggested_display_precision = 0

    def __init__(
        self,
        manager: "EntityManager",
        channel: object | None,
        entitykey: str | None = None,
        **kwargs: "Unpack[MLNumericSensor.Args]",
    ):
        super().__init__(
            manager,
            channel,
            entitykey or mc.KEY_LIGHT,
            **kwargs,
        )


class MLDiagnosticSensor(MLEnumSensor):

    if TYPE_CHECKING:
        is_diagnostic: Final

    is_diagnostic = True

    # HA core entity attributes:
    entity_category = MLNumericSensor.EntityCategory.DIAGNOSTIC

    def _parse(self, payload: dict):
        """
        This implementation aims at diagnostic sensors installed in 'well-known'
        namespace handlers to manage 'unexpected' channels when they eventually
        pop-up and we (still) have no clue why these channels are pushed (See #428)
        """
        self.update_native_value(json_dumps(payload))


class ProtocolSensor(me.MEAlwaysAvailableMixin, MLEnumSensor):
    STATE_DISCONNECTED = "disconnected"
    STATE_ACTIVE = "active"
    STATE_INACTIVE = "inactive"
    ATTR_BLUETOOTH = mlc.CONF_PROTOCOL_BLUETOOTH
    ATTR_HTTP = mlc.CONF_PROTOCOL_HTTP
    ATTR_MQTT = mlc.CONF_PROTOCOL_MQTT
    ATTR_MQTT_BROKER = "mqtt_broker"

    manager: "Device"

    # HA core entity attributes:
    _attr_entity_registry_enabled_default = False
    entity_category = MLEnumSensor.EntityCategory.DIAGNOSTIC
    native_value: str
    options: list[str] = [
        STATE_DISCONNECTED,
        ATTR_BLUETOOTH,
        ATTR_HTTP,
        ATTR_MQTT,
    ]

    @staticmethod
    def _get_attr_state(value):
        return ProtocolSensor.STATE_ACTIVE if value else ProtocolSensor.STATE_INACTIVE

    def __init__(
        self,
        manager: "Device",
    ):
        self.extra_state_attributes = {}
        super().__init__(
            manager,
            None,
            "sensor_protocol",
            native_value=ProtocolSensor.STATE_DISCONNECTED,
        )

    def set_available(self):
        manager = self.manager
        self.native_value = manager.curr_protocol
        attrs = self.extra_state_attributes
        _get_attr_state = self._get_attr_state
        if manager.conf_protocol is not manager.curr_protocol:
            # this is to identify when conf_protocol is CONF_PROTOCOL_AUTO
            # if conf_protocol is fixed we'll not set these attrs (redundant)
            attrs[self.ATTR_BLUETOOTH] = _get_attr_state(manager._bluetooth_active)
            attrs[self.ATTR_HTTP] = _get_attr_state(manager._http_active)
            attrs[self.ATTR_MQTT] = _get_attr_state(manager._mqtt_active)
            attrs[self.ATTR_MQTT_BROKER] = _get_attr_state(manager._mqtt_connected)
        self.flush_state()

    def set_unavailable(self):
        self.native_value = ProtocolSensor.STATE_DISCONNECTED
        if self.manager._mqtt_connection:
            self.extra_state_attributes = {
                self.ATTR_MQTT_BROKER: self._get_attr_state(
                    self.manager._mqtt_connected
                )
            }
        else:
            self.extra_state_attributes = {}
        self.flush_state()

    # these smart updates are meant to only flush attrs
    # when they are already present..i.e. meaning the device
    # conf_protocol is CONF_PROTOCOL_AUTO
    # call them 'before' connecting the device so they'll not flush
    # and the full state will be flushed by the update_connected call
    # and call them 'after' any eventual disconnection for the same reason
    def update_attr_active(self, attrname: str):
        attrs = self.extra_state_attributes
        if attrname in attrs:
            attrs[attrname] = self.STATE_ACTIVE
            self.flush_state()

    def update_attr_inactive(self, attrname: str):
        attrs = self.extra_state_attributes
        if attrname in attrs:
            attrs[attrname] = self.STATE_INACTIVE
            self.flush_state()

    def update_attrs_inactive(self, *attrnames):
        flush = False
        attrs = self.extra_state_attributes
        for attrname in attrnames:
            if attrs.get(attrname) is self.STATE_ACTIVE:
                attrs[attrname] = self.STATE_INACTIVE
                flush = True
        if flush:
            self.flush_state()


class MLSignalStrengthSensor(EntityNamespaceMixin, MLNumericSensor):

    ENTITY_KEY = "signal_strength"
    ns = mn.Appliance_System_Runtime
    key_value = mc.KEY_SIGNAL

    # HA core entity attributes:
    _attr_native_unit_of_measurement = me.MLEntity.hac.PERCENTAGE
    entity_category = MLNumericSensor.EntityCategory.DIAGNOSTIC
    icon = "mdi:wifi"


class MLFilterMaintenanceSensor(MLNumericSensor):

    ENTITY_KEY = mc.KEY_FILTER
    ns = mn.Appliance_Control_FilterMaintenance
    NS_CHANNELS = (0,)
    key_value = mc.KEY_LIFE

    # HA core entity attributes:
    _attr_native_unit_of_measurement = me.MLEntity.hac.PERCENTAGE
    entity_category = MLNumericSensor.EntityCategory.DIAGNOSTIC

    def __init__(self, manager: "Device", channel):
        MLNumericSensor.__init__(self, manager, channel)
        manager.register_parser_entity(self)
