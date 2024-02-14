from __future__ import annotations

import typing

from homeassistant.components import sensor
from homeassistant.const import (
    PERCENTAGE,
    UnitOfElectricCurrent,
    UnitOfElectricPotential,
    UnitOfEnergy,
    UnitOfPower,
    UnitOfTemperature,
)

from . import meross_entity as me
from .const import CONF_PROTOCOL_HTTP, CONF_PROTOCOL_MQTT

if typing.TYPE_CHECKING:
    from typing import Final

    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

    from .helpers.manager import EntityManager
    from .meross_device import MerossDevice


async def async_setup_entry(
    hass: HomeAssistant, config_entry: ConfigEntry, async_add_devices
):
    me.platform_setup_entry(hass, config_entry, async_add_devices, sensor.DOMAIN)


class MLEnumSensor(me.MerossEntity, sensor.SensorEntity):
    """Specialization for sensor with ENUM device_class which allows to store
    anything as opposed to numeric sensor types which have units and so."""

    PLATFORM = sensor.DOMAIN

    # HA core entity attributes:
    native_value: me.StateType | None
    native_unit_of_measurement: None = None

    __slots__ = ("native_value",)

    def __init__(
        self,
        manager: EntityManager,
        channel: object | None,
        entitykey: str | None,
        *,
        native_value: me.StateType = None,
    ):
        self.native_value = native_value
        super().__init__(
            manager,
            channel,
            entitykey,
            sensor.SensorDeviceClass.ENUM,
            available=native_value is not None,
        )

    def set_unavailable(self):
        self.native_value = None
        super().set_unavailable()

    def update_native_value(self, native_value: me.StateType):
        if self.native_value != native_value:
            self.native_value = native_value
            self.flush_state()


class MLNumericSensor(me.MerossNumericEntity, sensor.SensorEntity):
    PLATFORM = sensor.DOMAIN
    DeviceClass = sensor.SensorDeviceClass
    StateClass = sensor.SensorStateClass

    DEVICECLASS_TO_UNIT_MAP: dict[DeviceClass, str] = {
        DeviceClass.POWER: UnitOfPower.WATT,
        DeviceClass.CURRENT: UnitOfElectricCurrent.AMPERE,
        DeviceClass.VOLTAGE: UnitOfElectricPotential.VOLT,
        DeviceClass.ENERGY: UnitOfEnergy.WATT_HOUR,
        DeviceClass.TEMPERATURE: UnitOfTemperature.CELSIUS,
        DeviceClass.HUMIDITY: PERCENTAGE,
        DeviceClass.BATTERY: PERCENTAGE,
        DeviceClass.POWER_FACTOR: PERCENTAGE,
    }

    # we basically default Sensor.state_class to SensorStateClass.MEASUREMENT
    # except these device classes
    DEVICECLASS_TO_STATECLASS_MAP: dict[DeviceClass, StateClass] = {
        DeviceClass.ENERGY: StateClass.TOTAL_INCREASING,
    }

    # HA core entity attributes:
    state_class: StateClass

    __slots__ = ("state_class",)

    def __init__(
        self,
        manager: EntityManager,
        channel: object | None,
        entitykey: str | None,
        device_class: DeviceClass | None = None,
        *,
        device_value: int | None = None,
    ):
        assert device_class and device_class is not sensor.SensorDeviceClass.ENUM
        self.state_class = self.DEVICECLASS_TO_STATECLASS_MAP.get(
            device_class, MLNumericSensor.StateClass.MEASUREMENT
        )
        super().__init__(
            manager, channel, entitykey, device_class, device_value=device_value
        )

    @staticmethod
    def build_for_device(
        device: MerossDevice, device_class: MLNumericSensor.DeviceClass
    ):
        return MLNumericSensor(device, None, str(device_class), device_class)


class MLDiagnosticSensor(MLEnumSensor):

    is_diagnostic: Final = True

    # HA core entity attributes:
    entity_category = MLNumericSensor.EntityCategory.DIAGNOSTIC


class ProtocolSensor(MLEnumSensor):
    STATE_DISCONNECTED = "disconnected"
    STATE_ACTIVE = "active"
    STATE_INACTIVE = "inactive"
    ATTR_HTTP = CONF_PROTOCOL_HTTP
    ATTR_MQTT = CONF_PROTOCOL_MQTT
    ATTR_MQTT_BROKER = "mqtt_broker"

    manager: MerossDevice

    # HA core entity attributes:
    _attr_available = True
    entity_category = me.EntityCategory.DIAGNOSTIC
    entity_registry_enabled_default = False
    native_value: str
    options: list[str] = [STATE_DISCONNECTED, CONF_PROTOCOL_MQTT, CONF_PROTOCOL_HTTP]

    @staticmethod
    def _get_attr_state(value):
        return ProtocolSensor.STATE_ACTIVE if value else ProtocolSensor.STATE_INACTIVE

    def __init__(
        self,
        manager: MerossDevice,
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
