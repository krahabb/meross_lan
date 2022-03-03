from __future__ import annotations
import logging
from time import localtime
from datetime import datetime, timedelta, timezone

from homeassistant.helpers.typing import StateType
from homeassistant.components.sensor import (
    DOMAIN as PLATFORM_SENSOR,
)

try:
    from homeassistant.components.sensor import SensorEntity
    try:
        from homeassistant.components.sensor import SensorStateClass
        STATE_CLASS_MEASUREMENT = SensorStateClass.MEASUREMENT
        STATE_CLASS_TOTAL_INCREASING = SensorStateClass.TOTAL_INCREASING
    except:
        try:
            from homeassistant.components.sensor import STATE_CLASS_MEASUREMENT
        except:
            STATE_CLASS_MEASUREMENT = None
        try:
            from homeassistant.components.sensor import STATE_CLASS_TOTAL_INCREASING
        except:
            STATE_CLASS_TOTAL_INCREASING = STATE_CLASS_MEASUREMENT
except:#someone still pre 2021.5.0 ?
    from homeassistant.helpers.entity import Entity as SensorEntity
    STATE_CLASS_MEASUREMENT = None
    STATE_CLASS_TOTAL_INCREASING = None

from homeassistant.const import (
    DEVICE_CLASS_POWER, POWER_WATT,
    DEVICE_CLASS_CURRENT,
    DEVICE_CLASS_VOLTAGE,
    DEVICE_CLASS_ENERGY, ENERGY_WATT_HOUR,
    DEVICE_CLASS_TEMPERATURE, TEMP_CELSIUS,
    DEVICE_CLASS_HUMIDITY, PERCENTAGE,
    DEVICE_CLASS_BATTERY,
    DEVICE_CLASS_SIGNAL_STRENGTH,
)
try:
    # new in 2021.8.0 core (#52 #53)
    from homeassistant.const import (
        ELECTRIC_CURRENT_AMPERE,
        ELECTRIC_POTENTIAL_VOLT,
    )
except:#someone still pre 2021.8.0 ?
    from homeassistant.const import (
        ELECTRICAL_CURRENT_AMPERE,
        VOLT,
    )
    ELECTRIC_CURRENT_AMPERE = ELECTRICAL_CURRENT_AMPERE
    ELECTRIC_POTENTIAL_VOLT = VOLT


from .merossclient import MerossDeviceDescriptor, const as mc  # mEROSS cONST
from .meross_entity import (
    _MerossEntity,
    platform_setup_entry, platform_unload_entry,
    ENTITY_CATEGORY_CONFIG, ENTITY_CATEGORY_DIAGNOSTIC,
)
from .const import (
    PARAM_ENERGY_UPDATE_PERIOD, PARAM_SIGNAL_UPDATE_PERIOD,
)


CLASS_TO_UNIT_MAP = {
    DEVICE_CLASS_POWER: POWER_WATT,
    DEVICE_CLASS_CURRENT: ELECTRIC_CURRENT_AMPERE,
    DEVICE_CLASS_VOLTAGE: ELECTRIC_POTENTIAL_VOLT,
    DEVICE_CLASS_ENERGY: ENERGY_WATT_HOUR,
    DEVICE_CLASS_TEMPERATURE: TEMP_CELSIUS,
    DEVICE_CLASS_HUMIDITY: PERCENTAGE,
    DEVICE_CLASS_BATTERY: PERCENTAGE,
    DEVICE_CLASS_SIGNAL_STRENGTH: PERCENTAGE,
}

CORE_HAS_NATIVE_UNIT = hasattr(SensorEntity, 'native_unit_of_measurement')


async def async_setup_entry(hass: object, config_entry: object, async_add_devices):
    platform_setup_entry(hass, config_entry, async_add_devices, PLATFORM_SENSOR)

async def async_unload_entry(hass: object, config_entry: object) -> bool:
    return platform_unload_entry(hass, config_entry, PLATFORM_SENSOR)



class MLSensor(_MerossEntity, SensorEntity):

    PLATFORM = PLATFORM_SENSOR

    _attr_state_class: str | None = STATE_CLASS_MEASUREMENT
    _attr_last_reset: datetime | None = None
    _attr_native_unit_of_measurement: str | None

    def __init__(
        self,
        device: "MerossDevice",
        channel: object,
        entitykey: str,
        device_class: str,
        subdevice: "MerossSubDevice"):
        super().__init__(device, channel, entitykey, device_class, subdevice)
        self._attr_native_unit_of_measurement = CLASS_TO_UNIT_MAP.get(device_class)


    @staticmethod
    def build_for_device(device: "MerossDevice", device_class: str):
        return MLSensor(device, None, device_class, device_class, None)


    @staticmethod
    def build_for_subdevice(subdevice: "MerossSubDevice", device_class: str):
        #return MerossLanSensor(subdevice.hub, f"{subdevice.id}_{device_class}", device_class, subdevice)
        return MLSensor(subdevice.hub, subdevice.id, device_class, device_class, subdevice)


    @property
    def state_class(self) -> str | None:
        return self._attr_state_class


    @property
    def last_reset(self) -> datetime | None: # Deprecated, to be removed in 2021.11
        return self._attr_last_reset


    @property
    def native_unit_of_measurement(self) -> str | None:
        return self._attr_native_unit_of_measurement


    @property
    def unit_of_measurement(self) -> str | None:
        if CORE_HAS_NATIVE_UNIT:
            # let the core implementation manage unit conversions
            # in it's '@final unit_of_measurement'
            return SensorEntity.unit_of_measurement.__get__(self)
        return self._attr_native_unit_of_measurement


    @property
    def native_value(self) -> StateType:
        return self._attr_state


    @property
    def state(self) -> StateType:
        if CORE_HAS_NATIVE_UNIT:
            # let the core implementation manage unit conversions
            return SensorEntity.state.__get__(self)
        return self._attr_state



class ElectricityMixin:


    def __init__(self, api, descriptor: MerossDeviceDescriptor, entry) -> None:
        super().__init__(api, descriptor, entry)
        self._sensor_power = MLSensor.build_for_device(self, DEVICE_CLASS_POWER)
        self._sensor_current = MLSensor.build_for_device(self, DEVICE_CLASS_CURRENT)
        self._sensor_voltage = MLSensor.build_for_device(self, DEVICE_CLASS_VOLTAGE)


    def _handle_Appliance_Control_Electricity(
        self,
        namespace: str,
        method: str,
        payload: dict,
        header: dict
    ) -> None:
        electricity = payload.get(mc.KEY_ELECTRICITY)
        self._sensor_power.update_state(electricity.get(mc.KEY_POWER) / 1000)
        self._sensor_current.update_state(electricity.get(mc.KEY_CURRENT) / 1000)
        self._sensor_voltage.update_state(electricity.get(mc.KEY_VOLTAGE) / 10)


    def _request_updates(self, epoch, namespace):
        super()._request_updates(epoch, namespace)
        # we're not checking context namespace since it should be very unusual
        # to enter here with one of those following
        if self._sensor_power.enabled or self._sensor_voltage.enabled or self._sensor_current.enabled:
            self.request_get(mc.NS_APPLIANCE_CONTROL_ELECTRICITY)



class ConsumptionMixin:

    _lastupdate_energy = 0
    _lastreset_energy = 0 # store the last 'device time' we passed onto to _attr_last_reset


    def __init__(self, api, descriptor: MerossDeviceDescriptor, entry) -> None:
        super().__init__(api, descriptor, entry)
        self._sensor_energy = MLSensor.build_for_device(self, DEVICE_CLASS_ENERGY)
        self._sensor_energy._attr_state_class = STATE_CLASS_TOTAL_INCREASING


    def _handle_Appliance_Control_ConsumptionX(
        self,
        namespace: str,
        method: str,
        payload: dict,
        header: dict
    ) -> None:
        self._lastupdate_energy = self.lastupdate
        days = payload.get(mc.KEY_CONSUMPTIONX)
        days_len = len(days)
        if days_len < 1:
            if STATE_CLASS_TOTAL_INCREASING == STATE_CLASS_MEASUREMENT:
                self._sensor_energy._attr_last_reset = datetime.utcfromtimestamp(0)
            self._sensor_energy.update_state(0)
            return True
        # we'll look through the device array values to see
        # data timestamped (in device time) after last midnight
        # since we usually reset this around midnight localtime
        # the device timezone should be aligned else it will roundtrip
        # against it's own midnight and we'll see a delayed 'sawtooth'
        st = localtime()
        dt = datetime(
            st.tm_year, st.tm_mon, st.tm_mday,
            tzinfo=timezone(timedelta(seconds=st.tm_gmtoff), st.tm_zone)
        )
        timestamp_lastreset = dt.timestamp() - self.device_timedelta
        self.log(
            logging.DEBUG, 0,
            "MerossDevice(%s) Energy: device midnight = %d",
            self.device_id, timestamp_lastreset
        )
        def get_timestamp(day):
            return day.get(mc.KEY_TIME)
        days = sorted(days, key=get_timestamp, reverse=True)
        day_last:dict = days[0]
        if day_last.get(mc.KEY_TIME) < timestamp_lastreset:
            return True
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
                    timestamp_lastreset + self.device_timedelta + .5
                )
                self.log(
                    logging.DEBUG, 0,
                    "MerossDevice(%s) Energy: update last_reset to %s",
                    self.device_id, self._sensor_energy._attr_last_reset.isoformat()
                )
        self._sensor_energy.update_state(day_last.get(mc.KEY_VALUE))


    def _request_updates(self, epoch, namespace):
        super()._request_updates(epoch, namespace)
        if self._sensor_energy.enabled and (
            (
                (epoch - self._lastupdate_energy) > PARAM_ENERGY_UPDATE_PERIOD) or (
                    (namespace is not None) and # namespace is not None when coming online
                    (namespace != mc.NS_APPLIANCE_CONTROL_CONSUMPTIONX)
                )
            ):
            self.request_get(mc.NS_APPLIANCE_CONTROL_CONSUMPTIONX)



class RuntimeMixin:

    _lastupdate_runtime = 0


    def __init__(self, api, descriptor: MerossDeviceDescriptor, entry) -> None:
        super().__init__(api, descriptor, entry)
        self._sensor_runtime = MLSensor.build_for_device(self, DEVICE_CLASS_SIGNAL_STRENGTH)
        self._sensor_runtime._attr_entity_category = ENTITY_CATEGORY_DIAGNOSTIC


    def _handle_Appliance_System_Runtime(self,
    namespace: str, method: str, payload: dict, header: dict):
        self._lastupdate_runtime = self.lastupdate
        if isinstance(runtime := payload.get(mc.KEY_RUNTIME), dict):
            self._sensor_runtime.update_state(runtime.get(mc.KEY_SIGNAL))


    def _request_updates(self, epoch, namespace):
        super()._request_updates(epoch, namespace)
        if self._sensor_runtime.enabled and (
            (
                (epoch - self._lastupdate_runtime) > PARAM_SIGNAL_UPDATE_PERIOD) or (
                    (namespace is not None) and # namespace is not None when coming online
                    (namespace != mc.NS_APPLIANCE_SYSTEM_RUNTIME)
                )
            ):
            self.request_get(mc.NS_APPLIANCE_SYSTEM_RUNTIME)
