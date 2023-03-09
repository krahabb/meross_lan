from __future__ import annotations
from math import ceil
import typing
from logging import DEBUG, WARNING
from time import localtime
from datetime import datetime, timedelta, timezone

from homeassistant.components.sensor import (
    DOMAIN as PLATFORM_SENSOR,
)
from homeassistant.core import callback
from homeassistant.helpers.event import async_track_point_in_time
from homeassistant.util import dt as dt_util

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

from .helpers import get_entity_last_state_available
from . import meross_entity as me
from .merossclient import MerossDeviceDescriptor, const as mc  # mEROSS cONST
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


class MLSensor(me.MerossEntity, SensorEntity):  # type: ignore

    PLATFORM = PLATFORM_SENSOR

    _attr_state: int | float | None = None
    _attr_state_class: str | None = STATE_CLASS_MEASUREMENT
    _attr_last_reset: datetime | None = None
    _attr_native_unit_of_measurement: str | None = None

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


class EnergySensor(MLSensor):
    """
    Energy sensor is used for both our estimated energy consumption
    and the device self reported one (ConsumptionMixin). This entity
    should not be 'disconected' and beside the common 'cosmetic'
    properties there are some differences in how data are computed
    """

    ATTR_ENERGY_OFFSET = "energy_offset"
    energy_offset: int = 0
    ATTR_ENERGY_RESET_TS = "energy_reset_ts"
    energy_reset_ts: int = 0

    _attr_state: int = 0
    _attr_state_float: float = 0.0

    def __init__(self, device: MerossDevice, entity_key: str):
        self._attr_extra_state_attributes = {}
        super().__init__(device, None, entity_key, DEVICE_CLASS_ENERGY, None)

    @property
    def available(self):
        return True

    @property
    def state_class(self):
        return STATE_CLASS_TOTAL_INCREASING

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

        try:
            state = await get_entity_last_state_available(self.hass, self.entity_id)
            if state is None:
                return
            if state.last_updated < dt_util.start_of_local_day():
                # tbh I don't know what when last_update == start_of_day
                return
            for _attr_name in (self.ATTR_ENERGY_OFFSET, self.ATTR_ENERGY_RESET_TS):
                if _attr_name in state.attributes:
                    _attr_value = state.attributes[_attr_name]
                    self._attr_extra_state_attributes[
                        _attr_name
                    ] = _attr_value
                    # we also set the value as an instance attr for faster access
                    setattr(self, _attr_name, _attr_value)
            # state should be an int though but in case we decide some
            # tweaks here or there this conversion is safer (allowing for a float state)
            # and more consistent
            self._attr_state_float = float(state.state)
            self._attr_state = int(self._attr_state_float)
        except Exception as e:
            self.device.log(
                WARNING,
                14400,
                "EnergyEstimateSensor(%s): error(%s) while trying to restore previous state",
                self.name,
                str(e),
            )

    def set_unavailable(self):
        # we need to preserve our sum so we don't reset
        # it on disconnection. Also, it's nice to have it
        # available since this entity has a computed value
        # not directly related to actual connection state
        pass

    def update_estimate(self, de: float):
        # this is the 'estimated' sensor update api
        # based off ElectricityMixin power readings
        self._attr_state_float = self._attr_state_float + de
        state = int(self._attr_state_float)
        if self._attr_state != state:
            self._attr_state = state
            if self._hass_connected:
                self.async_write_ha_state()

    def update_consumption(self, consumption: int):
        # this is the 'official' ConsumptionMixin readings
        # but we're trying to fix #264 with our offset/reset
        self._attr_state = consumption - self.energy_offset
        # always try to flush even if state didnt change since
        # we might have attrs update...let HA check this..
        if self._hass_connected:
            self.async_write_ha_state()

    def update_reset(self):
        self._attr_state_float -= self._attr_state # preserve fraction
        self._attr_state = 0
        # we're also removing attrs since they have no meaningful
        # value in this state
        self._attr_extra_state_attributes = {}
        # to be safe (even if apparently not strictly needed)
        # better cleanup our offsets so we'll trigger
        # a consistent state refresh when data arrive
        self.energy_offset = 0
        self.energy_reset_ts = 0
        if self._hass_connected:
            self.async_write_ha_state()


class ElectricityMixin(
    MerossDevice if typing.TYPE_CHECKING else object
):  # pylint: disable=used-before-assignment

    _sensor_power: MLSensor
    _sensor_current: MLSensor
    _sensor_voltage: MLSensor
    # implement an estimated energy measure from _sensor_power.
    # Estimate is a trapezoidal integral sum on power. Using class
    # initializers to ease instance sharing (and type-checks)
    # between ElectricityMixin and ConsumptionMixin. Based on experience
    # ElectricityMixin and ConsumptionMixin are always present together
    # in metering plugs (mss310 is the historical example)
    _sensor_energy: EnergySensor | None = None
    _sensor_energy_estimate: EnergySensor

    _energy_estimate_lastupdate = 0.0
    _energy_estimate_integraltime = 0.0

    def __init__(self, api, descriptor: MerossDeviceDescriptor, entry):
        super().__init__(api, descriptor, entry)
        self._sensor_power = MLSensor.build_for_device(self, DEVICE_CLASS_POWER)
        self._sensor_current = MLSensor.build_for_device(self, DEVICE_CLASS_CURRENT)
        self._sensor_voltage = MLSensor.build_for_device(self, DEVICE_CLASS_VOLTAGE)
        self._sensor_energy_estimate = EnergySensor(self, "energy_estimate")
        self._sensor_energy_estimate._attr_entity_registry_enabled_default = False

    def start(self):
        self._schedule_next_reset(dt_util.now())
        super().start()

    def shutdown(self):
        super().shutdown()
        self._sensor_power = None  # type: ignore
        self._sensor_current = None  # type: ignore
        self._sensor_voltage = None  # type: ignore
        self._sensor_energy_estimate = None  # type: ignore
        if self._cancel_energy_reset is not None:
            self._cancel_energy_reset()
            self._cancel_energy_reset = None

    def _handle_Appliance_Control_Electricity(self, header: dict, payload: dict):
        electricity = payload[mc.KEY_ELECTRICITY]
        power: float = float(electricity[mc.KEY_POWER]) / 1000
        if (last_power := self._sensor_power._attr_state) is not None:
            dt = self.lastupdate - self._energy_estimate_lastupdate
            self._energy_estimate_integraltime += dt
            # de = (((last_power + power) / 2) * dt) / 3600
            self._sensor_energy_estimate.update_estimate(
                ((last_power + power) * dt)
                / 7200
            )
        self._energy_estimate_lastupdate = self.lastupdate
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
            await self.async_request_get(mc.NS_APPLIANCE_CONTROL_ELECTRICITY)

    def _schedule_next_reset(self, _now: datetime):
        try:
            today = _now.date()
            tomorrow = today + timedelta(days=1)
            next_reset = datetime(
                year=tomorrow.year,
                month=tomorrow.month,
                day=tomorrow.day,
                hour=0,
                minute=0,
                second=0,
                microsecond=1,
                tzinfo=dt_util.DEFAULT_TIME_ZONE,
            )
            self._cancel_energy_reset = async_track_point_in_time(
                self.api.hass, self._energy_reset, next_reset
            )
            self.log(
                DEBUG,
                0,
                "ElectricityMixin(%s) _schedule_next_reset: %s",
                self.name,
                next_reset.isoformat(),
            )
        except Exception as error:
            # really? log something
            self.log(
                DEBUG,
                0,
                "ElectricityMixin(%s) _schedule_next_reset Exception: %s",
                self.name,
                str(error),
            )

    @callback
    def _energy_reset(self, _now: datetime):
        self.log(
            DEBUG,
            0,
            "ElectricityMixin(%s) _energy_reset: %s",
            self.name,
            _now.isoformat(),
        )
        self._energy_estimate_integraltime = 0.0
        self._sensor_energy_estimate.update_reset()
        if self._sensor_energy is not None:
            self._sensor_energy.update_reset()
        self._schedule_next_reset(_now)


class ConsumptionMixin(
    MerossDevice if typing.TYPE_CHECKING else object
):  # pylint: disable=used-before-assignment

    _sensor_energy: EnergySensor
    _energy_lastupdate = 0

    # these come from ElectricityMixin instance
    _sensor_energy_estimate: EnergySensor | None = None
    _energy_estimate_integraltime: float

    def __init__(self, api, descriptor: MerossDeviceDescriptor, entry):
        super().__init__(api, descriptor, entry)
        self._sensor_energy = EnergySensor(self, DEVICE_CLASS_ENERGY)

    def shutdown(self):
        super().shutdown()
        self._sensor_energy = None  # type: ignore

    def _handle_Appliance_Control_ConsumptionX(self, header: dict, payload: dict):
        self._energy_lastupdate = self.lastupdate
        days: list = payload[mc.KEY_CONSUMPTIONX]  # type: ignore
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
        midnight_epoch = dt.timestamp() - self.device_timedelta
        # the days array contains a month worth of data
        # but we're only interested in the last few days (today
        # and maybe yesterday) so we discard a bunch of
        # elements before sorting (in order to not waste time)
        # checks for 'not enough meaningful data' are post-poned
        # and just for safety since they're unlikely to happen
        # in a normal running environment over few days
        yesterday_epoch = midnight_epoch - 86400
        days = [day for day in days if day[mc.KEY_TIME] >= yesterday_epoch]
        if (days_len := len(days)) == 0:
            return

        elif days_len > 1:

            def _get_timestamp(day):
                return day[mc.KEY_TIME]

            days = sorted(days, key=_get_timestamp)

        day_last: dict = days[-1]
        day_last_time: int = day_last[mc.KEY_TIME]
        day_last_consumption: int = day_last[mc.KEY_VALUE]

        if day_last_time < midnight_epoch:
            # this could happen right after midnight when the device
            # should start a new cycle but the consumption is too low
            # (device starts reporting from 1 wh....) so, even if
            # new day has come, new data are not
            self._sensor_energy.update_reset()
            return
        # now day_last 'should' contain today data in HA time.
        _sensor_energy = self._sensor_energy
        if days_len > 1:
            # we also have yesterday readings
            day_yesterday_time = days[-2][mc.KEY_TIME]
            if _sensor_energy.energy_reset_ts != day_yesterday_time:
                # this is the first time today that we receive new data.
                # in order to fix #264 we're going to set our internal energy offset.
                # This is very dangerous since we must discriminate between faulty
                # resets and good resets from the device. Typically the device resets
                # itself correctly and we have new 0-based readings but we can't
                # reliably tell when the error happens since the 'new' reading could be
                # any positive value depending on actual consumption of the device
                self.log(
                    DEBUG,
                    0,
                    "MerossDevice(%s) Energy: device midnight = %d",
                    self.name,
                    midnight_epoch,
                )
                # first off we consider the device readings good
                _sensor_energy.energy_reset_ts = day_yesterday_time
                _sensor_energy.energy_offset = 0
                _sensor_energy._attr_extra_state_attributes = {
                    _sensor_energy.ATTR_ENERGY_RESET_TS: day_yesterday_time
                }
                # In order to fix #264 and any further bug in consumption
                # we'll check it against our energy_estimate in ElectricityMixin.
                if self._sensor_energy_estimate is not None:
                    consumption_integral_time = day_last_time - midnight_epoch
                    # This test ensures our estimate has run a decent amount of
                    # time after midnight to be accurate (i.e. no disconnections)
                    # when checking against reported consumption 'around the clock'.
                    # If the device updates us very soon (consumption_integral_time < 600)
                    # we consider it as enough since we don't want our time check
                    # to be exposed to time jitter in midnight_epoch
                    if (consumption_integral_time < 600) or (
                        (self._energy_estimate_integraltime / consumption_integral_time) > 0.9
                    ):
                        # day_last_consumption carries over accumulated energy from
                        # the time of last change so, depending on time alignment
                        # it also has a part of energy of the day before (being int it
                        # doesnt get discarded). We're trying to implement the
                        # same accumulation carrying over on our estimate too
                        # since, saving rounded values, we might loss the fractions
                        # when resetting around midnight.
                        # This is not a big issue for the day (just rounding) but losing
                        # this fraction would accumulate (small) errors over longer periods
                        # Also, since our int energy estimate is 'floored' we don't want
                        # small disalignments in time and/or accumulation to kick in
                        energy_estimate = self._sensor_energy_estimate._attr_state + 1
                        if day_last_consumption > energy_estimate:
                            _sensor_energy._attr_extra_state_attributes[
                                _sensor_energy.ATTR_ENERGY_OFFSET
                            ] = _sensor_energy.energy_offset = day_last_consumption - energy_estimate

        _sensor_energy.update_consumption(day_last_consumption)

    async def async_request_updates(self, epoch, namespace):
        await super().async_request_updates(epoch, namespace)
        if self.online and self._sensor_energy.enabled and (
            (epoch - self._energy_lastupdate) > PARAM_ENERGY_UPDATE_PERIOD
        ):
            await self.async_request_get(mc.NS_APPLIANCE_CONTROL_CONSUMPTIONX)


class RuntimeMixin(
    MerossDevice if typing.TYPE_CHECKING else object
):  # pylint: disable=used-before-assignment

    _sensor_runtime: MLSensor
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

    def shutdown(self):
        super().shutdown()
        self._sensor_runtime = None  # type: ignore

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
