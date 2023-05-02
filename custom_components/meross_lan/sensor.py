from __future__ import annotations

from datetime import datetime, timedelta
from logging import DEBUG
from time import time
import typing

from homeassistant.components import sensor
from homeassistant.const import (
    ELECTRIC_CURRENT_AMPERE,
    ELECTRIC_POTENTIAL_VOLT,
    ENERGY_WATT_HOUR,
    PERCENTAGE,
    POWER_WATT,
    TEMP_CELSIUS,
)
from homeassistant.core import callback
from homeassistant.helpers.event import async_track_point_in_time
from homeassistant.util import dt as dt_util

from . import meross_entity as me
from .const import (
    CONF_PROTOCOL_HTTP,
    CONF_PROTOCOL_MQTT,
    PARAM_ENERGY_UPDATE_PERIOD,
    PARAM_SIGNAL_UPDATE_PERIOD,
)
from .helpers import ApiProfile, StrEnum, get_entity_last_state_available
from .merossclient import const as mc, get_default_arguments  # mEROSS cONST

if typing.TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

    from .meross_device import MerossDevice
    from .meross_device_hub import MerossSubDevice

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
    SensorDeviceClass.POWER: POWER_WATT,
    SensorDeviceClass.CURRENT: ELECTRIC_CURRENT_AMPERE,
    SensorDeviceClass.VOLTAGE: ELECTRIC_POTENTIAL_VOLT,
    SensorDeviceClass.ENERGY: ENERGY_WATT_HOUR,
    SensorDeviceClass.TEMPERATURE: TEMP_CELSIUS,
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
        device: MerossDevice,
        channel: object | None,
        entitykey: str | None,
        device_class: SensorDeviceClass | None,
        subdevice: MerossSubDevice | None,
    ):
        super().__init__(device, channel, entitykey, device_class, subdevice)
        self._attr_native_unit_of_measurement = DEVICECLASS_TO_UNIT_MAP.get(
            device_class
        )
        self._attr_state_class = DEVICECLASS_TO_STATECLASS_MAP.get(
            device_class, SensorStateClass.MEASUREMENT
        )

    @staticmethod
    def build_for_device(device: MerossDevice, device_class: SensorDeviceClass):
        return MLSensor(device, None, str(device_class), device_class, None)

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

    _attr_state: str
    _attr_options = [STATE_DISCONNECTED, CONF_PROTOCOL_MQTT, CONF_PROTOCOL_HTTP]

    @staticmethod
    def _get_attr_state(value):
        return ProtocolSensor.STATE_ACTIVE if value else ProtocolSensor.STATE_INACTIVE

    def __init__(
        self,
        device: MerossDevice,
    ):
        self._attr_extra_state_attributes = {}
        super().__init__(device, None, "sensor_protocol", self.DeviceClass.ENUM, None)
        self._attr_state = ProtocolSensor.STATE_DISCONNECTED

    @property
    def available(self):
        return True

    @property
    def entity_category(self):
        return me.EntityCategory.DIAGNOSTIC

    @property
    def entity_registry_enabled_default(self):
        return False

    @property
    def options(self) -> list[str] | None:
        return self._attr_options

    def set_unavailable(self):
        self._attr_state = ProtocolSensor.STATE_DISCONNECTED
        if self.device._mqtt_connection is None:
            self._attr_extra_state_attributes = {}
        else:
            self._attr_extra_state_attributes = {
                self.ATTR_MQTT_BROKER: self._get_attr_state(self.device._mqtt_connected)
            }
        if self._hass_connected:
            self._async_write_ha_state()

    def update_connected(self):
        device = self.device
        self._attr_state = device.curr_protocol
        if device.conf_protocol is not device.curr_protocol:
            # this is to identify when conf_protocol is CONF_PROTOCOL_AUTO
            # if conf_protocol is fixed we'll not set these attrs (redundant)
            self._attr_extra_state_attributes[self.ATTR_HTTP] = self._get_attr_state(
                device._http_lastresponse
            )
            self._attr_extra_state_attributes[self.ATTR_MQTT] = self._get_attr_state(
                device._mqtt_active
            )
            self._attr_extra_state_attributes[
                self.ATTR_MQTT_BROKER
            ] = self._get_attr_state(device._mqtt_connected)
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


class EnergyEstimateSensor(MLSensor):
    _attr_state: int
    _attr_state_float: float = 0.0

    def __init__(self, device: MerossDevice):
        super().__init__(device, None, "energy_estimate", self.DeviceClass.ENERGY, None)
        self._attr_state = 0

    @property
    def entity_registry_enabled_default(self):
        return False

    @property
    def available(self):
        return True

    async def async_added_to_hass(self):
        await super().async_added_to_hass()
        # state restoration is only needed on cold-start and we have to discriminate
        # from when this happens while the device is already working. In general
        # the sensor state is always kept in the instance even when it's disabled
        # so we don't want to overwrite that should we enable an entity after
        # it has been initialized. Checking _attr_state here should be enough
        # since it's surely 0 on boot/initial setup (entities are added before
        # device reading data). If an entity is disabled on startup of course our state
        # will start resetted and our sums will restart (disabled means not interesting
        # anyway)
        if self._attr_state != 0:
            return

        with self.exception_warning("restoring previous state"):
            state = await get_entity_last_state_available(self.hass, self.entity_id)
            if state is None:
                return
            if state.last_updated < dt_util.start_of_local_day():
                # tbh I don't know what when last_update == start_of_day
                return
            # state should be an int though but in case we decide some
            # tweaks here or there this conversion is safer (allowing for a float state)
            # and more consistent
            self._attr_state_float = float(state.state)
            self._attr_state = int(self._attr_state_float)

    def set_unavailable(self):
        # we need to preserve our sum so we don't reset
        # it on disconnection. Also, it's nice to have it
        # available since this entity has a computed value
        # not directly related to actual connection state
        pass

    def update_estimate(self, de: float):
        # this is the 'estimated' sensor update api
        # based off ElectricityMixin power readings
        self._attr_state_float += de
        state = int(self._attr_state_float)
        if self._attr_state != state:
            self._attr_state = state
            if self._hass_connected:
                self.async_write_ha_state()

    def reset_estimate(self):
        self._attr_state_float -= self._attr_state  # preserve fraction
        self._attr_state = 0
        if self._hass_connected:
            self.async_write_ha_state()


class ElectricityMixin(
    MerossDevice if typing.TYPE_CHECKING else object
):  # pylint: disable=used-before-assignment
    _electricity_lastupdate = 0.0
    _sensor_power: MLSensor
    _sensor_current: MLSensor
    _sensor_voltage: MLSensor
    # implement an estimated energy measure from _sensor_power.
    # Estimate is a trapezoidal integral sum on power. Using class
    # initializers to ease instance sharing (and type-checks)
    # between ElectricityMixin and ConsumptionMixin. Based on experience
    # ElectricityMixin and ConsumptionMixin are always present together
    # in metering plugs (mss310 is the historical example).
    # Based on observations this estimate is falling a bit behind
    # the consumption reported from the device at least when the
    # power is very low (likely due to power readings being a bit off)
    _sensor_energy_estimate: EnergyEstimateSensor

    # This is actually reset in ConsumptionMixin
    _consumption_estimate = 0.0

    def __init__(self, descriptor, entry):
        super().__init__(descriptor, entry)
        self._sensor_power = MLSensor.build_for_device(self, MLSensor.DeviceClass.POWER)
        self._sensor_current = MLSensor.build_for_device(
            self, MLSensor.DeviceClass.CURRENT
        )
        self._sensor_voltage = MLSensor.build_for_device(
            self, MLSensor.DeviceClass.VOLTAGE
        )
        self._sensor_energy_estimate = EnergyEstimateSensor(self)

    def start(self):
        self._schedule_next_reset(dt_util.now())
        super().start()

    async def async_shutdown(self):
        await super().async_shutdown()
        self._sensor_power = None  # type: ignore
        self._sensor_current = None  # type: ignore
        self._sensor_voltage = None  # type: ignore
        self._sensor_energy_estimate = None  # type: ignore
        if self._cancel_energy_reset is not None:
            self._cancel_energy_reset()
            self._cancel_energy_reset = None

    def _handle_Appliance_Control_Electricity(self, header: dict, payload: dict):
        electricity = payload[mc.KEY_ELECTRICITY]
        power = float(electricity[mc.KEY_POWER]) / 1000
        if (last_power := self._sensor_power._attr_state) is not None:
            # dt = self.lastupdate - self._electricity_lastupdate
            # de = (((last_power + power) / 2) * dt) / 3600
            de = (
                (last_power + power)
                * (self.lastresponse - self._electricity_lastupdate)
            ) / 7200
            self._consumption_estimate += de
            self._sensor_energy_estimate.update_estimate(de)

        self._electricity_lastupdate = self.lastresponse
        self._sensor_power.update_state(power)
        self._sensor_current.update_state(electricity[mc.KEY_CURRENT] / 1000)  # type: ignore
        self._sensor_voltage.update_state(electricity[mc.KEY_VOLTAGE] / 10)  # type: ignore

    async def async_request_updates(self, epoch, namespace):
        await super().async_request_updates(epoch, namespace)
        # we're always asking updates even if sensors could be disabled since
        # there are far too many dependencies for these readings (energy sensor
        # in ConsumptionMixin too depends on us) but it's unlikely all of these
        # are disabled!
        if self.online:
            await self.async_request(
                *get_default_arguments(mc.NS_APPLIANCE_CONTROL_ELECTRICITY)
            )

    def _schedule_next_reset(self, _now: datetime):
        with self.exception_warning("_schedule_next_reset"):
            today = _now.date()
            tomorrow = today + timedelta(days=1)
            next_reset = datetime(
                year=tomorrow.year,
                month=tomorrow.month,
                day=tomorrow.day,
                hour=0,
                minute=0,
                second=0,
                microsecond=0,
                tzinfo=dt_util.DEFAULT_TIME_ZONE,
            )
            self._cancel_energy_reset = async_track_point_in_time(
                ApiProfile.hass, self._energy_reset, next_reset
            )
            self.log(DEBUG, "_schedule_next_reset at %s", next_reset.isoformat())

    @callback
    def _energy_reset(self, _now: datetime):
        self.log(DEBUG, "_energy_reset at %s", _now.isoformat())
        self._sensor_energy_estimate.reset_estimate()
        self._schedule_next_reset(_now)


class ConsumptionSensor(MLSensor):
    ATTR_OFFSET = "offset"
    offset: int = 0
    ATTR_RESET_TS = "reset_ts"
    reset_ts: int = 0

    _attr_state: int | None

    def __init__(self, device: MerossDevice):
        self._attr_extra_state_attributes = {}
        super().__init__(
            device, None, str(self.DeviceClass.ENERGY), self.DeviceClass.ENERGY, None
        )

    async def async_added_to_hass(self):
        await super().async_added_to_hass()
        # state restoration is only needed on cold-start and we have to discriminate
        # from when this happens while the device is already working. In general
        # the sensor state is always kept in the instance even when it's disabled
        # so we don't want to overwrite that should we enable an entity after
        # it has been initialized. Checking _attr_state here should be enough
        # since it's surely 0 on boot/initial setup (entities are added before
        # device reading data). If an entity is disabled on startup of course our state
        # will start resetted and our sums will restart (disabled means not interesting
        # anyway)
        if (self._attr_state is not None) or self._attr_extra_state_attributes:
            return

        with self.exception_warning("restoring previous state"):
            state = await get_entity_last_state_available(self.hass, self.entity_id)
            if state is None:
                return
            # check if the restored sample is fresh enough i.e. it was
            # updated after the device midnight for today..else it is too
            # old to be good. Since we don't have actual device epoch we
            # 'guess' it is nicely synchronized so we'll use our time
            devicetime = self.device.get_datetime(time())
            devicetime_today_midnight = datetime(
                devicetime.year,
                devicetime.month,
                devicetime.day,
                tzinfo=devicetime.tzinfo,
            )
            if state.last_updated < devicetime_today_midnight:
                return

            # check if the restored sample is fresh enough i.e. it was
            # updated after the device midnight for today..else it is too
            # old to be good. Since we don't have actual device epoch we
            # 'guess' it is nicely synchronized so we'll use our time
            devicetime = self.device.get_datetime(time())
            devicetime_today_midnight = datetime(
                devicetime.year,
                devicetime.month,
                devicetime.day,
                tzinfo=devicetime.tzinfo,
            )
            if state.last_updated < devicetime_today_midnight:
                return

            # fix beta/preview attr names (sometime REMOVE)
            if "energy_offset" in state.attributes:
                _attr_value = state.attributes["energy_offset"]
                self._attr_extra_state_attributes[self.ATTR_OFFSET] = _attr_value
                setattr(self, self.ATTR_OFFSET, _attr_value)
            if "energy_reset_ts" in state.attributes:
                _attr_value = state.attributes["energy_reset_ts"]
                self._attr_extra_state_attributes[self.ATTR_RESET_TS] = _attr_value
                setattr(self, self.ATTR_RESET_TS, _attr_value)

            for _attr_name in (self.ATTR_OFFSET, self.ATTR_RESET_TS):
                if _attr_name in state.attributes:
                    _attr_value = state.attributes[_attr_name]
                    self._attr_extra_state_attributes[_attr_name] = _attr_value
                    # we also set the value as an instance attr for faster access
                    setattr(self, _attr_name, _attr_value)

            # HA adds decimals when the display precision is set for the entity
            # according to this issue #268. In order to try not mess statistics
            # we're reverting to the old design where the sensor state is
            # reported as 'unavailable' when the device is disconnected and so
            # we don't restore the state value at all but just wait for a 'fresh'
            # consumption value from the device. The attributes restoration will
            # instead keep patching the 'consumption reset bug'

    def reset_consumption(self):
        if self._attr_state != 0:
            self._attr_state = 0
            self._attr_extra_state_attributes = {}
            self.offset = 0
            self.reset_ts = 0
            if self._hass_connected:
                self.async_write_ha_state()
            self.log(DEBUG, "no readings available for new day - resetting")


class ConsumptionMixin(
    MerossDevice if typing.TYPE_CHECKING else object
):  # pylint: disable=used-before-assignment
    _consumption_lastupdate = 0.0
    _consumption_last_value: int | None = None
    _consumption_last_time: int | None = None
    # these are the device actual EPOCHs of the last midnight
    # and the midnight of they before. midnight epoch(s) are
    # the times at which the device local time trips around
    # midnight (which could be different than GMT tripping of course)
    _yesterday_midnight_epoch = 0  # 12:00 am yesterday
    _today_midnight_epoch = 0  # 12:00 am today
    _tomorrow_midnight_epoch = 0  # 12:00 am tomorrow

    # instance value shared with ElectricityMixin
    _consumption_estimate = 0.0

    def __init__(self, descriptor, entry):
        super().__init__(descriptor, entry)
        self._sensor_consumption: ConsumptionSensor = ConsumptionSensor(self)

    async def async_shutdown(self):
        await super().async_shutdown()
        self._sensor_consumption = None  # type: ignore

    def _handle_Appliance_Control_ConsumptionX(self, header: dict, payload: dict):
        self._consumption_lastupdate = self.lastresponse
        # we'll look through the device array values to see
        # data timestamped (in device time) after last midnight
        # since we usually reset this around midnight localtime
        # the device timezone should be aligned else it will roundtrip
        # against it's own midnight and we'll see a delayed 'sawtooth'
        if self.device_timestamp > self._tomorrow_midnight_epoch:
            # catch the device starting a new day since our last update (yesterday)
            devtime = self.get_datetime(self.device_timestamp)
            devtime_today_midnight = datetime(
                devtime.year,
                devtime.month,
                devtime.day,
                tzinfo=devtime.tzinfo,
            )
            # we'd better not trust our cached tomorrow, today and yesterday
            # epochs (even if 99% of the times they should be good)
            # so we fully recalculate them on each 'midnight trip update'
            # and spend some cpu resources this way...
            self._today_midnight_epoch = devtime_today_midnight.timestamp()
            daydelta = timedelta(days=1)
            self._tomorrow_midnight_epoch = (
                devtime_today_midnight + daydelta
            ).timestamp()
            self._yesterday_midnight_epoch = (
                devtime_today_midnight - daydelta
            ).timestamp()
            self.log(
                DEBUG,
                "updated midnight epochs: yesterday=%s - today=%s - tomorrow=%s",
                str(self._yesterday_midnight_epoch),
                str(self._today_midnight_epoch),
                str(self._tomorrow_midnight_epoch),
            )

        # the days array contains a month worth of data
        # but we're only interested in the last few days (today
        # and maybe yesterday) so we discard a bunch of
        # elements before sorting (in order to not waste time)
        # checks for 'not enough meaningful data' are post-poned
        # and just for safety since they're unlikely to happen
        # in a normal running environment over few days
        days = [
            day
            for day in payload[mc.KEY_CONSUMPTIONX]
            if day[mc.KEY_TIME] >= self._yesterday_midnight_epoch
        ]
        if (days_len := len(days)) == 0:
            self._sensor_consumption.reset_consumption()
            return

        elif days_len > 1:

            def _get_timestamp(day):
                return day[mc.KEY_TIME]

            days = sorted(days, key=_get_timestamp)

        day_last: dict = days[-1]
        day_last_time: int = day_last[mc.KEY_TIME]

        if day_last_time < self._today_midnight_epoch:
            # this could happen right after midnight when the device
            # should start a new cycle but the consumption is too low
            # (device starts reporting from 1 wh....) so, even if
            # new day has come, new data have not
            self._consumption_last_value = None
            self._sensor_consumption.reset_consumption()
            return

        # now day_last 'should' contain today data in HA time.
        day_last_value: int = day_last[mc.KEY_VALUE]
        _sensor_consumption = self._sensor_consumption
        # check if the device tripped its own midnight and started a
        # new day readings
        if days_len > 1 and (
            _sensor_consumption.reset_ts
            != (day_yesterday_time := days[-2][mc.KEY_TIME])
        ):
            # this is the first time after device midnight that we receive new data.
            # in order to fix #264 we're going to set our internal energy offset.
            # This is very dangerous since we must discriminate between faulty
            # resets and good resets from the device. Typically the device resets
            # itself correctly and we have new 0-based readings but we can't
            # reliably tell when the error happens since the 'new' reading could be
            # any positive value depending on actual consumption of the device

            # first off we consider the device readings good
            _sensor_consumption.reset_ts = day_yesterday_time
            _sensor_consumption.offset = 0
            _sensor_consumption._attr_extra_state_attributes = {
                _sensor_consumption.ATTR_RESET_TS: day_yesterday_time
            }
            if (self._consumption_last_time is not None) and (
                self._consumption_last_time <= day_yesterday_time
            ):
                # In order to fix #264 and any further bug in consumption
                # we'll check it against _consumption_estimate from ElectricityMixin.
                # _consumption_estimate is reset in ConsumptionMixin every time we
                # get a new fresh consumption value and should contain an estimate
                # over the last (device) accumulation period. Here we're across the
                # device midnight reset so our _consumption_estimate is trying
                # to measure the effective consumption since the last updated
                # reading of yesterday. The check on _consumption_last_time is
                # to make sure we're not applying any offset when we start 'fresh'
                # reading during a day and HA has no state carried over since
                # midnight on this sensor
                energy_estimate = int(self._consumption_estimate) + 1
                if day_last_value > energy_estimate:
                    _sensor_consumption._attr_extra_state_attributes[
                        _sensor_consumption.ATTR_OFFSET
                    ] = _sensor_consumption.offset = (day_last_value - energy_estimate)
            self.log(
                DEBUG,
                "first consumption reading for new day, offset=%d",
                _sensor_consumption.offset,
            )

        elif day_last_value == self._consumption_last_value:
            # no change in consumption..skip updating unless sensor was disconnected
            if _sensor_consumption._attr_state is None:
                _sensor_consumption._attr_state = (
                    day_last_value - _sensor_consumption.offset
                )
                if _sensor_consumption._hass_connected:
                    _sensor_consumption.async_write_ha_state()
            return

        self._consumption_last_time = day_last_time
        self._consumption_last_value = day_last_value
        self._consumption_estimate = 0.0  # reset ElecticityMixin estimate cycle
        _sensor_consumption._attr_state = day_last_value - _sensor_consumption.offset
        if _sensor_consumption._hass_connected:
            _sensor_consumption.async_write_ha_state()
        self.log(DEBUG, "updating consumption=%d", day_last_value)

    async def async_request_updates(self, epoch, namespace):
        await super().async_request_updates(epoch, namespace)
        if (
            self.online
            and self._sensor_consumption.enabled
            and ((epoch - self._consumption_lastupdate) > PARAM_ENERGY_UPDATE_PERIOD)
        ):
            await self.async_request(
                *get_default_arguments(mc.NS_APPLIANCE_CONTROL_CONSUMPTIONX)
            )

    def _set_offline(self):
        super()._set_offline()
        self._yesterday_midnight_epoch = 0
        self._today_midnight_epoch = 0
        self._tomorrow_midnight_epoch = 0


class RuntimeMixin(
    MerossDevice if typing.TYPE_CHECKING else object
):  # pylint: disable=used-before-assignment
    _sensor_runtime: MLSensor
    _lastupdate_runtime = 0

    def __init__(self, descriptor, entry):
        super().__init__(descriptor, entry)
        # DEVICE_CLASS_SIGNAL_STRENGTH is now 'forcing' dB or dBm as unit
        # so we drop the device_class (none) but we let the 'entitykey' parameter
        # to keep the same value so the entity id inside HA remains stable (#239)
        self._sensor_runtime = MLSensor(self, None, "signal_strength", None, None)
        self._sensor_runtime._attr_entity_category = me.EntityCategory.DIAGNOSTIC
        self._sensor_runtime._attr_native_unit_of_measurement = PERCENTAGE
        self._sensor_runtime._attr_icon = "mdi:wifi"

    async def async_shutdown(self):
        await super().async_shutdown()
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
