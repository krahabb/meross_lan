from dataclasses import dataclass
import typing

from homeassistant.components import sensor

from . import const as mlc, meross_entity as me
from .helpers.namespaces import (
    EntityNamespaceHandler,
    EntityNamespaceMixin,
    NamespaceHandler,
)
from .merossclient import const as mc, json_dumps, namespaces as mn

if typing.TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

    from .helpers.manager import EntityManager
    from .meross_device import MerossDevice

    # optional arguments for MLEnumSensor init
    class MLEnumSensorArgs(me.MerossEntityArgs):
        native_value: typing.NotRequired[sensor.StateType]

    # optional arguments for MLNumericSensor init
    class MLNumericSensorArgs(me.MerossNumericEntityArgs):
        state_class: typing.NotRequired[sensor.SensorStateClass]


async def async_setup_entry(
    hass: "HomeAssistant", config_entry: "ConfigEntry", async_add_devices
):
    me.platform_setup_entry(hass, config_entry, async_add_devices, sensor.DOMAIN)


class MLEnumSensor(me.MerossEntity, sensor.SensorEntity):
    """Specialization for sensor with ENUM device_class which allows to store
    anything as opposed to numeric sensor types which have units and so."""

    PLATFORM = sensor.DOMAIN

    # HA core entity attributes:
    native_value: "sensor.StateType"
    native_unit_of_measurement: None = None

    __slots__ = ("native_value",)

    def __init__(
        self,
        manager: "EntityManager",
        channel: object | None,
        entitykey: str | None,
        **kwargs: "typing.Unpack[MLEnumSensorArgs]",
    ):
        self.native_value = kwargs.pop("native_value", None)
        super().__init__(
            manager, channel, entitykey, sensor.SensorDeviceClass.ENUM, **kwargs
        )

    def set_unavailable(self):
        self.native_value = None
        super().set_unavailable()

    def update_native_value(self, native_value: sensor.StateType):
        if self.native_value != native_value:
            self.native_value = native_value
            self.flush_state()
            return True


class MLNumericSensor(me.MerossNumericEntity, sensor.SensorEntity):
    PLATFORM = sensor.DOMAIN
    DeviceClass = sensor.SensorDeviceClass
    StateClass = sensor.SensorStateClass

    DEVICECLASS_TO_UNIT_MAP = {
        DeviceClass.POWER: me.MerossEntity.hac.UnitOfPower.WATT,
        DeviceClass.CURRENT: me.MerossEntity.hac.UnitOfElectricCurrent.AMPERE,
        DeviceClass.VOLTAGE: me.MerossEntity.hac.UnitOfElectricPotential.VOLT,
        DeviceClass.ENERGY: me.MerossEntity.hac.UnitOfEnergy.WATT_HOUR,
        DeviceClass.TEMPERATURE: me.MerossEntity.hac.UnitOfTemperature.CELSIUS,
        DeviceClass.HUMIDITY: me.MerossEntity.hac.PERCENTAGE,
        DeviceClass.BATTERY: me.MerossEntity.hac.PERCENTAGE,
        DeviceClass.ILLUMINANCE: me.MerossEntity.hac.LIGHT_LUX,
    }

    # we basically default Sensor.state_class to SensorStateClass.MEASUREMENT
    # except these device classes
    DEVICECLASS_TO_STATECLASS_MAP: dict[DeviceClass | None, StateClass] = {
        None: StateClass.MEASUREMENT,
        DeviceClass.ENERGY: StateClass.TOTAL_INCREASING,
    }

    # HA core entity attributes:
    state_class: StateClass

    __slots__ = ("state_class",)

    def __init__(
        self,
        manager: "EntityManager",
        channel: object | None,
        entitykey: str | None,
        device_class: DeviceClass | None = None,
        **kwargs: "typing.Unpack[MLNumericSensorArgs]",
    ):
        assert device_class is not sensor.SensorDeviceClass.ENUM
        self.state_class = kwargs.pop(
            "state_class", None
        ) or self.DEVICECLASS_TO_STATECLASS_MAP.get(
            device_class, MLNumericSensor.StateClass.MEASUREMENT
        )

        super().__init__(
            manager,
            channel,
            entitykey,
            device_class,
            **kwargs,
        )

    @staticmethod
    def build_for_device(
        device: "MerossDevice",
        device_class: "MLNumericSensor.DeviceClass",
        **kwargs: "typing.Unpack[MLNumericSensorArgs]",
    ):
        return MLNumericSensor(
            device,
            None,
            str(device_class),
            device_class,
            **kwargs,
        )


@dataclass(frozen=True, slots=True)
class MLNumericSensorDef:
    """Descriptor class used when populating maps used to dynamically instantiate (sensor)
    entities based on their appearance in a payload key."""

    type: "type[MLNumericSensor]"
    args: "MLNumericSensorArgs"


class MLHumiditySensor(MLNumericSensor):
    """Specialization for Humidity sensor.
    - device_scale defaults to 10 which is actually the only scale seen so far.
    - suggested_display_precision defaults to 0
    """

    _attr_device_scale = 10
    # HA core entity attributes:
    _attr_suggested_display_precision = 0

    def __init__(
        self,
        manager: "EntityManager",
        channel: object | None,
        entitykey: str = "humidity",
        **kwargs: "typing.Unpack[MLNumericSensorArgs]",
    ):
        kwargs.setdefault("name", entitykey.capitalize())
        super().__init__(
            manager,
            channel,
            entitykey,
            sensor.SensorDeviceClass.HUMIDITY,
            **kwargs,
        )


class MLTemperatureSensor(MLNumericSensor):
    """Specialization for Temperature sensor.
    - device_scale defaults to 1 (from base class definition) and is likely to be overriden.
    - suggested_display_precision defaults to 1
    """

    # HA core entity attributes:
    _attr_suggested_display_precision = 1

    def __init__(
        self,
        manager: "EntityManager",
        channel: object | None,
        entitykey: str = "temperature",
        **kwargs: "typing.Unpack[MLNumericSensorArgs]",
    ):
        kwargs.setdefault("name", entitykey.capitalize())
        super().__init__(
            manager,
            channel,
            entitykey,
            sensor.SensorDeviceClass.TEMPERATURE,
            **kwargs,
        )


class MLLightSensor(MLNumericSensor):
    """Specialization for sensor reporting light illuminance (lux)."""

    _attr_device_scale = 1
    # HA core entity attributes:
    _attr_suggested_display_precision = 0

    def __init__(
        self,
        manager: "EntityManager",
        channel: object | None,
        entitykey: str = "light",
        **kwargs: "typing.Unpack[MLNumericSensorArgs]",
    ):
        kwargs.setdefault("name", entitykey.capitalize())
        super().__init__(
            manager,
            channel,
            entitykey,
            sensor.SensorDeviceClass.ILLUMINANCE,
            **kwargs,
        )


class MLDiagnosticSensor(MLEnumSensor):

    is_diagnostic: typing.Final = True

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
    ATTR_HTTP = mlc.CONF_PROTOCOL_HTTP
    ATTR_MQTT = mlc.CONF_PROTOCOL_MQTT
    ATTR_MQTT_BROKER = "mqtt_broker"

    manager: "MerossDevice"

    # HA core entity attributes:
    entity_category = me.EntityCategory.DIAGNOSTIC
    entity_registry_enabled_default = False
    native_value: str
    options: list[str] = [
        STATE_DISCONNECTED,
        mlc.CONF_PROTOCOL_MQTT,
        mlc.CONF_PROTOCOL_HTTP,
    ]

    @staticmethod
    def _get_attr_state(value):
        return ProtocolSensor.STATE_ACTIVE if value else ProtocolSensor.STATE_INACTIVE

    def __init__(
        self,
        manager: "MerossDevice",
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

    def update_attr(self, attrname: str, attr_state):
        attrs = self.extra_state_attributes
        if attrname in attrs:
            attrs[attrname] = self._get_attr_state(attr_state)
            self.flush_state()

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

    ns = mn.Appliance_System_Runtime

    # HA core entity attributes:
    entity_category = me.EntityCategory.DIAGNOSTIC
    icon = "mdi:wifi"

    def __init__(self, manager: "MerossDevice"):
        super().__init__(
            manager,
            None,
            mlc.SIGNALSTRENGTH_ID,
            None,
            native_unit_of_measurement=me.MerossEntity.hac.PERCENTAGE,
        )
        EntityNamespaceHandler(self)

    def _handle(self, header: dict, payload: dict):
        self.update_native_value(payload[mc.KEY_RUNTIME][mc.KEY_SIGNAL])


class MLFilterMaintenanceSensor(MLNumericSensor):

    ns = mn.Appliance_Control_FilterMaintenance
    key_value = mc.KEY_LIFE

    # HA core entity attributes:
    entity_category = me.EntityCategory.DIAGNOSTIC

    def __init__(self, manager: "MerossDevice", channel):
        super().__init__(
            manager,
            channel,
            mc.KEY_FILTER,
            None,
            native_unit_of_measurement=me.MerossEntity.hac.PERCENTAGE,
        )
        manager.register_parser_entity(self)


class FilterMaintenanceNamespaceHandler(NamespaceHandler):

    def __init__(self, device: "MerossDevice"):
        NamespaceHandler.__init__(
            self,
            device,
            mn.Appliance_Control_FilterMaintenance,
        )
        MLFilterMaintenanceSensor(device, 0)


class ConsumptionHSensor(MLNumericSensor):

    manager: "MerossDevice"
    ns = mn.Appliance_Control_ConsumptionH

    _attr_suggested_display_precision = 0

    __slots__ = ()

    def __init__(self, manager: "MerossDevice", channel: object | None):
        super().__init__(
            manager,
            channel,
            mc.KEY_CONSUMPTIONH,
            self.DeviceClass.ENERGY,
            name="Consumption",
        )
        manager.register_parser_entity(self)

    def _parse_consumptionH(self, payload: dict):
        """
        {"channel": 1, "total": 958, "data": [{"timestamp": 1721548740, "value": 0}]}
        """
        self.update_device_value(payload[mc.KEY_TOTAL])


class ConsumptionHNamespaceHandler(NamespaceHandler):
    """
    This namespace carries hourly statistics (over last 24 ours?) of energy consumption
    Appearing in: mts200 - em06 (Refoss) - mop320
    This ns looks tricky since for mts200, the query (payload GET) needs the channel
    index while for em06 this isn't necessary (empty query replies full sensor set statistics).
    Actual coding, according to what mts200 expects might work badly on em06 (since the query
    code setup will use our knowledge of which channels are available and this is not enforced
    on em06).
    Also, we need to come up with a reasonable euristic on which channels are available
    mts200: 1 (channel 0)
    mop320: 3 (channel 0 - 1 - 2) even tho it only has 2 metering channels (0 looks toggling both)
    em06: 6 channels (but the query works without setting any)
    """

    def __init__(self, device: "MerossDevice"):
        NamespaceHandler.__init__(
            self,
            device,
            mn.Appliance_Control_ConsumptionH,
        )
        # Current approach is to build a sensor for any appearing channel index
        # in digest. This in turns will not directly build the EM06 sensors
        # but they should come when polling.
        self.register_entity_class(
            ConsumptionHSensor, initially_disabled=False, build_from_digest=True
        )

    def _polling_request_init(self, request_payload_type: mn.RequestPayloadType):
        # TODO: move this device type 'patching' to some 'smart' Namespace grammar
        if self.device.descriptor.type.startswith(mc.TYPE_EM06):
            super()._polling_request_init(mn.RequestPayloadType.DICT)
        else:
            super()._polling_request_init(request_payload_type)
