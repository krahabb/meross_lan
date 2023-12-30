from __future__ import annotations

from datetime import datetime
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
from .helpers import StrEnum

if typing.TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

    from .helpers import EntityManager
    from .meross_device import MerossDevice

SensorStateClass = sensor.SensorStateClass
try:
    SensorDeviceClass = sensor.SensorDeviceClass  # type: ignore
except Exception:

    class SensorDeviceClass(StrEnum):
        BATTERY = "battery"
        CURRENT = "current"
        ENERGY = "energy"
        ENUM = "enum"
        HUMIDITY = "humidity"
        POWER = "power"
        TEMPERATURE = "temperature"
        VOLTAGE = "voltage"


async def async_setup_entry(
    hass: HomeAssistant, config_entry: ConfigEntry, async_add_devices
):
    me.platform_setup_entry(hass, config_entry, async_add_devices, sensor.DOMAIN)


DEVICECLASS_TO_UNIT_MAP: dict[SensorDeviceClass | None, str] = {
    SensorDeviceClass.POWER: UnitOfPower.WATT,
    SensorDeviceClass.CURRENT: UnitOfElectricCurrent.AMPERE,
    SensorDeviceClass.VOLTAGE: UnitOfElectricPotential.VOLT,
    SensorDeviceClass.ENERGY: UnitOfEnergy.WATT_HOUR,
    SensorDeviceClass.TEMPERATURE: UnitOfTemperature.CELSIUS,
    SensorDeviceClass.HUMIDITY: PERCENTAGE,
    SensorDeviceClass.BATTERY: PERCENTAGE,
}

# we basically default Sensor.state_class to SensorStateClass.MEASUREMENT
# except these device classes
DEVICECLASS_TO_STATECLASS_MAP: dict[
    SensorDeviceClass | None, SensorStateClass | None
] = {
    SensorDeviceClass.ENUM: None,
    SensorDeviceClass.ENERGY: SensorStateClass.TOTAL_INCREASING,
}

NativeValueCallbackType = typing.Callable[[], me.StateType]


class MLSensor(me.MerossEntity, sensor.SensorEntity):
    PLATFORM = sensor.DOMAIN
    DeviceClass = SensorDeviceClass
    StateClass = SensorStateClass

    _attr_native_unit_of_measurement: str | None
    _attr_state: int | float | None
    _attr_state_class: SensorStateClass | None

    __slots__ = (
        "_attr_native_unit_of_measurement",
        "_attr_state_class",
    )

    def __init__(
        self,
        manager: EntityManager,
        channel: object | None,
        entitykey: str | None,
        device_class: SensorDeviceClass | None,
    ):
        super().__init__(manager, channel, entitykey, device_class)
        self._attr_native_unit_of_measurement = DEVICECLASS_TO_UNIT_MAP.get(
            device_class
        )
        self._attr_state_class = DEVICECLASS_TO_STATECLASS_MAP.get(
            device_class, SensorStateClass.MEASUREMENT
        )

    @staticmethod
    def build_for_device(device: MerossDevice, device_class: SensorDeviceClass):
        return MLSensor(device, None, str(device_class), device_class)

    @property
    def last_reset(self) -> datetime | None:
        return None

    @property
    def native_unit_of_measurement(self):
        return self._attr_native_unit_of_measurement

    @property
    def native_value(self):
        return self._attr_state

    @property
    def state_class(self):
        return self._attr_state_class


class ProtocolSensor(MLSensor):
    STATE_DISCONNECTED = "disconnected"
    STATE_ACTIVE = "active"
    STATE_INACTIVE = "inactive"
    ATTR_HTTP = CONF_PROTOCOL_HTTP
    ATTR_MQTT = CONF_PROTOCOL_MQTT
    ATTR_MQTT_BROKER = "mqtt_broker"

    manager: MerossDevice

    _attr_entity_category = me.EntityCategory.DIAGNOSTIC
    _attr_state: str
    _attr_options = [STATE_DISCONNECTED, CONF_PROTOCOL_MQTT, CONF_PROTOCOL_HTTP]

    @staticmethod
    def _get_attr_state(value):
        return ProtocolSensor.STATE_ACTIVE if value else ProtocolSensor.STATE_INACTIVE

    def __init__(
        self,
        manager: MerossDevice,
    ):
        self._attr_extra_state_attributes = {}
        super().__init__(manager, None, "sensor_protocol", self.DeviceClass.ENUM)
        self._attr_state = ProtocolSensor.STATE_DISCONNECTED

    @property
    def available(self):
        return True

    @property
    def entity_registry_enabled_default(self):
        return False

    @property
    def options(self) -> list[str] | None:
        return self._attr_options

    def set_unavailable(self):
        self._attr_state = ProtocolSensor.STATE_DISCONNECTED
        if self.manager._mqtt_connection:
            self._attr_extra_state_attributes = {
                self.ATTR_MQTT_BROKER: self._get_attr_state(
                    self.manager._mqtt_connected
                )
            }
        else:
            self._attr_extra_state_attributes = {}
        if self._hass_connected:
            self._async_write_ha_state()

    def update_connected(self):
        manager = self.manager
        self._attr_state = manager.curr_protocol
        if manager.conf_protocol is not manager.curr_protocol:
            # this is to identify when conf_protocol is CONF_PROTOCOL_AUTO
            # if conf_protocol is fixed we'll not set these attrs (redundant)
            self._attr_extra_state_attributes[self.ATTR_HTTP] = self._get_attr_state(
                manager._http_active
            )
            self._attr_extra_state_attributes[self.ATTR_MQTT] = self._get_attr_state(
                manager._mqtt_active
            )
            self._attr_extra_state_attributes[
                self.ATTR_MQTT_BROKER
            ] = self._get_attr_state(manager._mqtt_connected)
        if self._hass_connected:
            self._async_write_ha_state()

    # these smart updates are meant to only flush attrs
    # when they are already present..i.e. meaning the device
    # conf_protocol is CONF_PROTOCOL_AUTO
    # call them 'before' connecting the device so they'll not flush
    # and the full state will be flushed by the update_connected call
    # and call them 'after' any eventual disconnection for the same reason

    def update_attr(self, attrname: str, attr_state):
        if attrname in self._attr_extra_state_attributes:
            self._attr_extra_state_attributes[attrname] = self._get_attr_state(
                attr_state
            )
            if self._hass_connected:
                self._async_write_ha_state()

    def update_attr_active(self, attrname: str):
        if attrname in self._attr_extra_state_attributes:
            self._attr_extra_state_attributes[attrname] = self.STATE_ACTIVE
            if self._hass_connected:
                self._async_write_ha_state()

    def update_attr_inactive(self, attrname: str):
        if attrname in self._attr_extra_state_attributes:
            self._attr_extra_state_attributes[attrname] = self.STATE_INACTIVE
            if self._hass_connected:
                self._async_write_ha_state()

    def update_attrs_inactive(self, *attrnames):
        flush = False
        for attrname in attrnames:
            if self._attr_extra_state_attributes.get(attrname) is self.STATE_ACTIVE:
                self._attr_extra_state_attributes[attrname] = self.STATE_INACTIVE
                flush = True
        if flush and self._hass_connected:
            self._async_write_ha_state()
