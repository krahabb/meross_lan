from __future__ import annotations
import typing
from logging import DEBUG
from time import localtime
from datetime import datetime, timedelta, timezone

from homeassistant.components.sensor import (
    DOMAIN as PLATFORM_SENSOR,
)
from homeassistant.util.dt import now

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
except:  # someone still pre 2021.5.0 ?
    from homeassistant.helpers.entity import Entity as SensorEntity

    STATE_CLASS_MEASUREMENT = None
    STATE_CLASS_TOTAL_INCREASING = None

from homeassistant.const import (
    DEVICE_CLASS_POWER,
    POWER_WATT,
    DEVICE_CLASS_CURRENT,
    DEVICE_CLASS_VOLTAGE,
    DEVICE_CLASS_ENERGY,
    ENERGY_WATT_HOUR,
    DEVICE_CLASS_TEMPERATURE,
    TEMP_CELSIUS,
    DEVICE_CLASS_HUMIDITY,
    PERCENTAGE,
    DEVICE_CLASS_BATTERY,
    DEVICE_CLASS_SIGNAL_STRENGTH,
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


from .merossclient import MerossDeviceDescriptor, const as mc  # mEROSS cONST
from . import meross_entity as me
from .const import (
    PARAM_ENERGY_UPDATE_PERIOD,
    PARAM_SIGNAL_UPDATE_PERIOD,
)

if typing.TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from homeassistant.config_entries import ConfigEntry
    from .meross_device import MerossDevice
    from .meross_device_hub import MerossSubDevice


CLASS_TO_UNIT_MAP = {
    DEVICE_CLASS_POWER: POWER_WATT,
    DEVICE_CLASS_CURRENT: ELECTRIC_CURRENT_AMPERE,
    DEVICE_CLASS_VOLTAGE: ELECTRIC_POTENTIAL_VOLT,
    DEVICE_CLASS_ENERGY: ENERGY_WATT_HOUR,
    DEVICE_CLASS_TEMPERATURE: TEMP_CELSIUS,
    DEVICE_CLASS_HUMIDITY: PERCENTAGE,
    DEVICE_CLASS_BATTERY: PERCENTAGE,
}

CORE_HAS_NATIVE_UNIT = hasattr(SensorEntity, "native_unit_of_measurement")


async def async_setup_entry(
    hass: HomeAssistant, config_entry: ConfigEntry, async_add_devices
):
    me.platform_setup_entry(hass, config_entry, async_add_devices, PLATFORM_SENSOR)


async def async_unload_entry(hass: HomeAssistant, config_entry: ConfigEntry):
    return me.platform_unload_entry(hass, config_entry, PLATFORM_SENSOR)


class MLSensor(me.MerossEntity, SensorEntity):  # type: ignore

    PLATFORM = PLATFORM_SENSOR

    _attr_state_class: str | None = STATE_CLASS_MEASUREMENT
    _attr_last_reset: datetime | None = None
    _attr_native_unit_of_measurement: str | None

    def __init__(
        self,
        device: MerossDevice,
        channel: object | None,
        entitykey: str | None,
        device_class: str | None,
        subdevice: MerossSubDevice | None,
    ):
        super().__init__(device, channel, entitykey, device_class, subdevice)
        self._attr_native_unit_of_measurement = CLASS_TO_UNIT_MAP.get(device_class)  # type: ignore

    @staticmethod
    def build_for_device(device: MerossDevice, device_class: str):
        return MLSensor(device, None, device_class, device_class, None)

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
        return self._attr_state


class ElectricityMixin(
    MerossDevice if typing.TYPE_CHECKING else object
):  # pylint: disable=used-before-assignment
    def __init__(self, api, descriptor: MerossDeviceDescriptor, entry):
        super().__init__(api, descriptor, entry)
        self._sensor_power = MLSensor.build_for_device(self, DEVICE_CLASS_POWER)
        self._sensor_current = MLSensor.build_for_device(self, DEVICE_CLASS_CURRENT)
        self._sensor_voltage = MLSensor.build_for_device(self, DEVICE_CLASS_VOLTAGE)

    def _handle_Appliance_Control_Electricity(self, header: dict, payload: dict):
        electricity = payload.get(mc.KEY_ELECTRICITY)
        self._sensor_power.update_state(electricity.get(mc.KEY_POWER) / 1000)  # type: ignore
        self._sensor_current.update_state(electricity.get(mc.KEY_CURRENT) / 1000)  # type: ignore
        self._sensor_voltage.update_state(electricity.get(mc.KEY_VOLTAGE) / 10)  # type: ignore

    async def async_request_updates(self, epoch, namespace):
        await super().async_request_updates(epoch, namespace)
        # we're not checking context namespace since it should be very unusual
        # to enter here with one of those following
        if (
            self._sensor_power.enabled
            or self._sensor_voltage.enabled
            or self._sensor_current.enabled
        ):
            await self.async_request_get(mc.NS_APPLIANCE_CONTROL_ELECTRICITY)


class ConsumptionMixin(
    MerossDevice if typing.TYPE_CHECKING else object
):  # pylint: disable=used-before-assignment

    _lastupdate_energy = 0
    _lastreset_energy = (
        0  # store the last 'device time' we passed onto to _attr_last_reset
    )

    def __init__(self, api, descriptor: MerossDeviceDescriptor, entry):
        super().__init__(api, descriptor, entry)
        self._sensor_energy = MLSensor.build_for_device(self, DEVICE_CLASS_ENERGY)
        self._sensor_energy._attr_state_class = STATE_CLASS_TOTAL_INCREASING

    def _handle_Appliance_Control_ConsumptionX(self, header: dict, payload: dict):
        self._lastupdate_energy = self.lastupdate
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
            tzinfo=timezone(timedelta(seconds=st.tm_gmtoff), st.tm_zone),
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
            await self.async_request_get(mc.NS_APPLIANCE_CONTROL_CONSUMPTIONX)


class RuntimeMixin(
    MerossDevice if typing.TYPE_CHECKING else object
):  # pylint: disable=used-before-assignment

    _lastupdate_runtime = 0

    def __init__(self, api, descriptor: MerossDeviceDescriptor, entry):
        super().__init__(api, descriptor, entry)
        # DEVICE_CLASS_SIGNAL_STRENGTH is now 'forcing' dB or dBm as unit
        # so we drop the device_class (none) but we let the 'entitykey' parameter
        # to keep the same value so the entity id inside HA remains stable (#239)
        self._sensor_runtime = MLSensor(
            self, None, DEVICE_CLASS_SIGNAL_STRENGTH, None, None
        )
        self._sensor_runtime._attr_entity_category = me.EntityCategory.DIAGNOSTIC
        self._sensor_runtime._attr_native_unit_of_measurement = PERCENTAGE
        self._sensor_runtime._attr_icon = "mdi:wifi"

    def _handle_Appliance_System_Runtime(self, header: dict, payload: dict):
        self._lastupdate_runtime = self.lastupdate
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
            await self.async_request_get(mc.NS_APPLIANCE_SYSTEM_RUNTIME)
