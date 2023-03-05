from __future__ import annotations
import typing
from logging import DEBUG
from time import localtime
from datetime import datetime, timedelta, timezone

from homeassistant.const import (
    # DEVICE_CLASS_POWER,
    POWER_WATT,
    # DEVICE_CLASS_CURRENT,
    # DEVICE_CLASS_VOLTAGE,
    # DEVICE_CLASS_ENERGY,
    ENERGY_WATT_HOUR,
    # DEVICE_CLASS_TEMPERATURE,
    TEMP_CELSIUS,
    # DEVICE_CLASS_HUMIDITY,
    PERCENTAGE,
    # DEVICE_CLASS_BATTERY,
    # DEVICE_CLASS_SIGNAL_STRENGTH,
)

try:
    # new in 2021.8.0 core (#52 #53)
    from homeassistant.const import (
        ELECTRIC_CURRENT_AMPERE,
        ELECTRIC_POTENTIAL_VOLT,
    )
except:  # someone still pre 2021.8.0 ?
    ELECTRIC_CURRENT_AMPERE = "A"
    ELECTRIC_POTENTIAL_VOLT = "V"

from homeassistant.components import sensor

SensorEntity = sensor.SensorEntity

from homeassistant.util.dt import now

from .merossclient import (
    const as mc,  # mEROSS cONST
    get_default_arguments,
)
from . import meross_entity as me
from .helpers import StrEnum
from .const import (
    CONF_PROTOCOL_HTTP,
    CONF_PROTOCOL_MQTT,
    PARAM_ENERGY_UPDATE_PERIOD,
    PARAM_SIGNAL_UPDATE_PERIOD,
)

if typing.TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from homeassistant.config_entries import ConfigEntry
    from .meross_device import MerossDevice
    from .meross_device_hub import MerossSubDevice


try:
    SensorDeviceClass = sensor.SensorDeviceClass  # type: ignore
except:

    class SensorDeviceClass(StrEnum):
        BATTERY = "battery"
        CURRENT = "current"
        ENERGY = "energy"
        ENUM = "enum"
        HUMIDITY = "humidity"
        POWER = "power"
        TEMPERATURE = "temperature"
        VOLTAGE = "voltage"


try:
    STATE_CLASS_MEASUREMENT = sensor.SensorStateClass.MEASUREMENT
    STATE_CLASS_TOTAL_INCREASING = sensor.SensorStateClass.TOTAL_INCREASING
except:
    try:
        STATE_CLASS_MEASUREMENT = sensor.STATE_CLASS_MEASUREMENT
    except:
        STATE_CLASS_MEASUREMENT = None
    try:
        STATE_CLASS_TOTAL_INCREASING = sensor.STATE_CLASS_TOTAL_INCREASING
    except:
        STATE_CLASS_TOTAL_INCREASING = STATE_CLASS_MEASUREMENT

CORE_HAS_NATIVE_UNIT = hasattr(SensorEntity, "native_unit_of_measurement")


async def async_setup_entry(
    hass: HomeAssistant, config_entry: ConfigEntry, async_add_devices
):
    me.platform_setup_entry(hass, config_entry, async_add_devices, sensor.DOMAIN)


CLASS_TO_UNIT_MAP = {
    SensorDeviceClass.POWER: POWER_WATT,
    SensorDeviceClass.CURRENT: ELECTRIC_CURRENT_AMPERE,
    SensorDeviceClass.VOLTAGE: ELECTRIC_POTENTIAL_VOLT,
    SensorDeviceClass.ENERGY: ENERGY_WATT_HOUR,
    SensorDeviceClass.TEMPERATURE: TEMP_CELSIUS,
    SensorDeviceClass.HUMIDITY: PERCENTAGE,
    SensorDeviceClass.BATTERY: PERCENTAGE,
}

NativeValueCallbackType = typing.Callable[[], me.StateType]


class MLSensor(me.MerossEntity, SensorEntity):  # type: ignore

    PLATFORM = sensor.DOMAIN
    DeviceClass = SensorDeviceClass

    _attr_last_reset: datetime | None = None
    _attr_native_unit_of_measurement: str | None
    _attr_state_class: str | None = STATE_CLASS_MEASUREMENT

    def __init__(
        self,
        device: MerossDevice,
        channel: object | None,
        entitykey: str | None,
        device_class: SensorDeviceClass | None,
        subdevice: MerossSubDevice | None,
    ):
        super().__init__(device, channel, entitykey, device_class, subdevice)
        self._attr_native_unit_of_measurement = CLASS_TO_UNIT_MAP.get(device_class)  # type: ignore

    @staticmethod
    def build_for_device(device: MerossDevice, device_class: SensorDeviceClass):
        return MLSensor(device, None, str(device_class), device_class, None)

    @property
    def state_class(self):
        return self._attr_state_class

    @property
    def last_reset(self):
        return self._attr_last_reset

    @property
    def native_unit_of_measurement(self):
        return self._attr_native_unit_of_measurement

    @property
    def native_value(self):
        return self._attr_state

    @property
    def unit_of_measurement(self):
        if CORE_HAS_NATIVE_UNIT:
            # let the core implementation manage unit conversions
            # in it's '@final unit_of_measurement'
            return SensorEntity.unit_of_measurement.__get__(self)
        return self._attr_native_unit_of_measurement

    @property
    def state(self):
        if CORE_HAS_NATIVE_UNIT:
            # let the core implementation manage unit conversions
            return SensorEntity.state.__get__(self)
        return self.native_value


class ProtocolSensor(MLSensor):  # type: ignore

    STATE_DISCONNECTED = 'disconnected'
    STATE_ACTIVE = 'active'
    STATE_INACTIVE = 'inactive'
    ATTR_HTTP = CONF_PROTOCOL_HTTP
    ATTR_MQTT = CONF_PROTOCOL_MQTT
    ATTR_MQTT_BROKER = 'mqtt_broker'

    _attr_entity_category = me.EntityCategory.DIAGNOSTIC
    _attr_state = STATE_DISCONNECTED
    _attr_options = [STATE_DISCONNECTED, CONF_PROTOCOL_MQTT, CONF_PROTOCOL_HTTP]
    _attr_state_class = None

    @staticmethod
    def _get_attr_state(value):
        return ProtocolSensor.STATE_ACTIVE if value else ProtocolSensor.STATE_INACTIVE

    def __init__(
        self,
        device: MerossDevice,
    ):
        self._attr_extra_state_attributes = {}
        super().__init__(device, None, "sensor_protocol", self.DeviceClass.ENUM, None)

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
        self._attr_state = self.STATE_DISCONNECTED
        if self.device._mqtt_profile is None:
            self._attr_extra_state_attributes = {}
        else:
            self._attr_extra_state_attributes = {
                self.ATTR_MQTT_BROKER: self._get_attr_state(self.device._mqtt)
            }
        if self._hass_connected:
            self._async_write_ha_state()

    def update_connected(self):
        device = self.device
        self._attr_state = device.curr_protocol
        if device.conf_protocol is not device.curr_protocol:
            # this is to identify when conf_protocol is CONF_PROTOCOL_AUTO
            # if conf_protocol is fixed we'll not set these attrs (redundant)
            self._attr_extra_state_attributes[self.ATTR_HTTP] = (
                self._get_attr_state(device.lasthttpresponse)
            )
            self._attr_extra_state_attributes[self.ATTR_MQTT] = (
                self._get_attr_state(device.lastmqttresponse)
            )
            self._attr_extra_state_attributes[self.ATTR_MQTT_BROKER] = (
                self._get_attr_state(device._mqtt)
            )
        if self._hass_connected:
            self._async_write_ha_state()

    # these smart updates are meant to only flush attrs
    # when they are already present..i.e. meaning the device
    # conf_protocol is CONF_PROTOCOL_AUTO
    # call them 'before' connecting the device so they'll not flush
    # and the full state will be flushed by the update_connected call
    # and call them 'after' any eventual disconnection for the same reason

    def update_connected_attr(self, attrname):
        if self._attr_extra_state_attributes.get(attrname) is self.STATE_INACTIVE:
            # this actually means the device is already online and
            # conf_protocol is CONF_PROTOCOL_AUTO
            self._attr_extra_state_attributes[attrname] = self.STATE_ACTIVE
            if self._hass_connected:
                self._async_write_ha_state()

    def update_disconnected_attr(self, *attrnames):
        for attrname in attrnames:
            flush = False
            if self._attr_extra_state_attributes.get(attrname) is self.STATE_ACTIVE:
                self._attr_extra_state_attributes[attrname] = self.STATE_INACTIVE
                flush = True
            if flush and self._hass_connected:
                self._async_write_ha_state()


class ElectricityMixin(
    MerossDevice if typing.TYPE_CHECKING else object
):  # pylint: disable=used-before-assignment

    electricity: dict[str, object]

    _sensor_power: MLSensor
    _sensor_current: MLSensor
    _sensor_voltage: MLSensor

    def __init__(self, api, descriptor, entry):
        super().__init__(api, descriptor, entry)
        self._sensor_power = MLSensor.build_for_device(self, SensorDeviceClass.POWER)
        self._sensor_current = MLSensor.build_for_device(
            self, SensorDeviceClass.CURRENT
        )
        self._sensor_voltage = MLSensor.build_for_device(
            self, SensorDeviceClass.VOLTAGE
        )

    def shutdown(self):
        super().shutdown()
        self._sensor_power = None  # type: ignore
        self._sensor_current = None  # type: ignore
        self._sensor_voltage = None  # type: ignore

    def _handle_Appliance_Control_Electricity(self, header: dict, payload: dict):
        electricity = payload.get(mc.KEY_ELECTRICITY)
        try:
            self._sensor_power.update_state(electricity[mc.KEY_POWER] / 1000)  # type: ignore
        except:
            pass
        try:
            self._sensor_current.update_state(electricity[mc.KEY_CURRENT] / 1000)  # type: ignore
        except:
            pass
        try:
            self._sensor_voltage.update_state(electricity[mc.KEY_VOLTAGE] / 10)  # type: ignore
        except:
            pass

    async def async_request_updates(self, epoch, namespace):
        await super().async_request_updates(epoch, namespace)
        # we're not checking context namespace since it should be very unusual
        # to enter here with one of those following
        if (
            self._sensor_power.enabled
            or self._sensor_voltage.enabled
            or self._sensor_current.enabled
        ):
            await self.async_request(
                *get_default_arguments(mc.NS_APPLIANCE_CONTROL_ELECTRICITY)
            )


class ConsumptionMixin(
    MerossDevice if typing.TYPE_CHECKING else object
):  # pylint: disable=used-before-assignment

    _sensor_energy: MLSensor
    _lastupdate_energy = 0
    _lastreset_energy = 0

    def __init__(self, api, descriptor, entry):
        super().__init__(api, descriptor, entry)
        self._sensor_energy = MLSensor.build_for_device(self, SensorDeviceClass.ENERGY)
        self._sensor_energy._attr_state_class = STATE_CLASS_TOTAL_INCREASING

    def shutdown(self):
        super().shutdown()
        self._sensor_energy = None  # type: ignore

    def _handle_Appliance_Control_ConsumptionX(self, header: dict, payload: dict):
        self._lastupdate_energy = self.lastresponse
        days: list = payload.get(mc.KEY_CONSUMPTIONX)  # type: ignore
        days_len = len(days)
        if days_len < 1:
            if STATE_CLASS_TOTAL_INCREASING == STATE_CLASS_MEASUREMENT:
                self._sensor_energy._attr_last_reset = now()
            self._sensor_energy.update_state(0)  # type: ignore
            return
        # we'll look through the device array values to see
        # data timestamped (in device time) after last midnight
        # since we usually reset this around midnight localtime
        # the device timezone should be aligned else it will roundtrip
        # against it's own midnight and we'll see a delayed 'sawtooth'
        st = localtime()
        dt = datetime(
            st.tm_year,
            st.tm_mon,
            st.tm_mday,
            tzinfo=timezone(timedelta(seconds=st.tm_gmtoff), st.tm_zone)
            if st.tm_zone is not None
            else None,
        )
        timestamp_lastreset = dt.timestamp() - self.device_timedelta
        self.log(
            DEBUG,
            0,
            "MerossDevice(%s) Energy: device midnight = %d",
            self.name,
            timestamp_lastreset,
        )

        def get_timestamp(day):
            return day.get(mc.KEY_TIME)

        days = sorted(days, key=get_timestamp, reverse=True)
        day_last: dict = days[0]
        if day_last.get(mc.KEY_TIME) < timestamp_lastreset:  # type: ignore
            return
        if days_len > 1:
            timestamp_lastreset = days[1].get(mc.KEY_TIME)
        if self._lastreset_energy != timestamp_lastreset:
            # we 'cache' timestamp_last_reset so we don't 'jitter' _attr_last_reset
            # should device_timedelta change (and it will!)
            # this is not really working until days_len is >= 2
            self._lastreset_energy = timestamp_lastreset
            # we'll add .5 (sec) to the device last reading since the reset
            # occurs right after that
            # update the entity last_reset only for a 'corner case'
            # when the feature was initially added (2021.8) and
            # STATE_CLASS_TOTAL_INCREASING was not defined yet
            if STATE_CLASS_TOTAL_INCREASING == STATE_CLASS_MEASUREMENT:
                self._sensor_energy._attr_last_reset = datetime.utcfromtimestamp(
                    timestamp_lastreset + self.device_timedelta + 0.5
                )
                self.log(
                    DEBUG,
                    0,
                    "MerossDevice(%s) Energy: update last_reset to %s",
                    self.name,
                    self._sensor_energy._attr_last_reset.isoformat(),
                )
        self._sensor_energy.update_state(day_last.get(mc.KEY_VALUE))

    async def async_request_updates(self, epoch, namespace):
        await super().async_request_updates(epoch, namespace)
        if self._sensor_energy.enabled and (
            ((epoch - self._lastupdate_energy) > PARAM_ENERGY_UPDATE_PERIOD)
            or (
                (namespace is not None)
                and (  # namespace is not None when coming online
                    namespace != mc.NS_APPLIANCE_CONTROL_CONSUMPTIONX
                )
            )
        ):
            await self.async_request(
                *get_default_arguments(mc.NS_APPLIANCE_CONTROL_CONSUMPTIONX)
            )


class RuntimeMixin(
    MerossDevice if typing.TYPE_CHECKING else object
):  # pylint: disable=used-before-assignment

    _sensor_runtime: MLSensor
    _lastupdate_runtime = 0

    def __init__(self, api, descriptor, entry):
        super().__init__(api, descriptor, entry)
        # DEVICE_CLASS_SIGNAL_STRENGTH is now 'forcing' dB or dBm as unit
        # so we drop the device_class (none) but we let the 'entitykey' parameter
        # to keep the same value so the entity id inside HA remains stable (#239)
        self._sensor_runtime = MLSensor(self, None, "signal_strength", None, None)
        self._sensor_runtime._attr_entity_category = me.EntityCategory.DIAGNOSTIC
        self._sensor_runtime._attr_native_unit_of_measurement = PERCENTAGE
        self._sensor_runtime._attr_icon = "mdi:wifi"

    def shutdown(self):
        super().shutdown()
        self._sensor_runtime = None  # type: ignore

    def _handle_Appliance_System_Runtime(self, header: dict, payload: dict):
        self._lastupdate_runtime = self.lastresponse
        if isinstance(runtime := payload.get(mc.KEY_RUNTIME), dict):
            self._sensor_runtime.update_state(runtime.get(mc.KEY_SIGNAL))

    async def async_request_updates(self, epoch, namespace):
        await super().async_request_updates(epoch, namespace)
        if self._sensor_runtime.enabled and (
            ((epoch - self._lastupdate_runtime) > PARAM_SIGNAL_UPDATE_PERIOD)
            or (
                (namespace is not None)
                and (  # namespace is not None when coming online
                    namespace != mc.NS_APPLIANCE_SYSTEM_RUNTIME
                )
            )
        ):
            await self.async_request(
                *get_default_arguments(mc.NS_APPLIANCE_SYSTEM_RUNTIME)
            )
