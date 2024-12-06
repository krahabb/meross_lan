from datetime import datetime, timedelta
from time import time
import typing

from homeassistant.core import callback
from homeassistant.helpers.event import async_track_point_in_time
from homeassistant.util import dt as dt_util

from .. import const as mlc, meross_entity as me
from ..helpers.namespaces import (
    EntityNamespaceHandler,
    EntityNamespaceMixin,
    NamespaceHandler,
    VoidNamespaceHandler,
)
from ..merossclient import const as mc, namespaces as mn
from ..sensor import MLEnumSensor, MLNumericSensor
from ..switch import MLSwitch

if typing.TYPE_CHECKING:
    from ..meross_device import MerossDevice


class ElectricitySensor(me.MEAlwaysAvailableMixin, MLNumericSensor):
    """
    This sensor acts as the main parser for 'Electricity' and 'ElectricityX' namespaces
    taking care of power, current, voltage, etc, sensors for the same channel.
    It also implements a trapezoidal estimator for energy consumption. Based on observations
    this estimate is falling a bit behind the consumption reported from the device at least
    when the power is very low (likely due to power readings being a bit off).
    """

    manager: "MerossDevice"

    SENSOR_DEFS: typing.ClassVar[
        dict[
            str,
            tuple[
                bool,
                MLNumericSensor.DeviceClass,
                MLNumericSensor.StateClass,
                int,
                int,
            ],
        ]
    ] = {
        # key: (not-optional, DeviceClass, StateClass, suggested_display_precision, device_scale)
        mc.KEY_CURRENT: (
            True,
            MLNumericSensor.DeviceClass.CURRENT,
            MLNumericSensor.StateClass.MEASUREMENT,
            1,
            1000,
        ),
        mc.KEY_POWER: (
            True,
            MLNumericSensor.DeviceClass.POWER,
            MLNumericSensor.StateClass.MEASUREMENT,
            1,
            1000,
        ),
        mc.KEY_VOLTAGE: (
            True,
            MLNumericSensor.DeviceClass.VOLTAGE,
            MLNumericSensor.StateClass.MEASUREMENT,
            1,
            10,
        ),
    }

    # HA core entity attributes:
    entity_registry_enabled_default = False
    native_value: int

    __slots__ = (
        "_estimate",
        "_electricity_lastepoch",
        "_reset_unsub",
        "sensor_consumptionx",
    )

    def __init__(self, manager: "MerossDevice", channel: object | None):
        self._estimate = 0.0
        self._electricity_lastepoch = 0.0
        self._reset_unsub = None
        # depending on init order we might not have this ready now...
        self.sensor_consumptionx: ConsumptionXSensor | None = manager.entities.get(mlc.CONSUMPTIONX_SENSOR_KEY)  # type: ignore
        # here entitykey is the 'legacy' EnergyEstimateSensor one to mantain compatibility
        super().__init__(
            manager,
            channel,
            mlc.ELECTRICITY_SENSOR_KEY,
            self.DeviceClass.ENERGY,
            device_value=0,
        )
        self._schedule_reset(dt_util.now())
        for key, entity_def in self.SENSOR_DEFS.items():
            if entity_def[0]:
                MLNumericSensor(
                    manager,
                    channel,
                    key,
                    entity_def[1],
                    state_class=entity_def[2],
                    suggested_display_precision=entity_def[3],
                    device_scale=entity_def[4],
                )

    async def async_shutdown(self):
        if self._reset_unsub:
            self._reset_unsub()
            self._reset_unsub = None
        await super().async_shutdown()
        self.sensor_consumptionx = None

    async def async_added_to_hass(self):
        await super().async_added_to_hass()
        # state restoration is only needed on cold-start and we have to discriminate
        # from when this happens while the device is already working. In general
        # the sensor state is always kept in the instance even when it's disabled
        # so we don't want to overwrite that should we enable an entity after
        # it has been initialized. Checking native_value here should be enough
        # since it's surely 0 on boot/initial setup (entities are added before
        # device reading data). If an entity is disabled on startup of course our state
        # will start resetted and our sums will restart (disabled means not interesting
        # anyway)
        if self.native_value:
            return

        with self.exception_warning("restoring previous state"):
            state = await self.get_last_state_available()
            if state is None:
                return
            if state.last_updated < dt_util.start_of_local_day():
                # tbh I don't know what when last_update == start_of_day
                return
            # state should be an int though but in case we decide some
            # tweaks here or there this conversion is safer (allowing for a float state)
            # and more consistent
            self._estimate = float(state.state)
            self.native_value = int(self._estimate)

    # interface: self
    def _handle_Appliance_Control_Electricity(self, header: dict, payload: dict):
        self._parse_electricity(payload[mc.KEY_ELECTRICITY])

    def _parse_electricity(self, payload: dict):
        """{"channel": 0, "power": 11000, ...}"""
        device = self.manager
        entities = device.entities

        def _get_sensor_from_key(key: str):
            try:
                sensor: MLNumericSensor = entities[key if self.channel is None else f"{self.channel}_{key}"]  # type: ignore
            except KeyError:
                entity_def = self.SENSOR_DEFS[key]
                sensor = MLNumericSensor(
                    device,
                    self.channel,
                    key,
                    entity_def[1],
                    state_class=entity_def[2],
                    suggested_display_precision=entity_def[3],
                    device_scale=entity_def[4],
                )
            return sensor

        sensor_power = _get_sensor_from_key(mc.KEY_POWER)
        last_power = sensor_power.native_value

        for key in self.SENSOR_DEFS:
            if key in payload:
                _get_sensor_from_key(key).update_device_value(payload[key])

        power = sensor_power.native_value
        if not power:
            # might be an indication of issue #367 where the problem lies in missing
            # device timezone configuration
            device.check_device_timezone()

        # device.device_timestamp 'should be' current epoch of the message
        if last_power is not None:
            de = (
                (last_power + power)  # type: ignore
                * (device.device_timestamp - self._electricity_lastepoch)
            ) / 7200
            if self.sensor_consumptionx:
                # we're helping the ConsumptionXSensor to carry on
                # energy accumulation/readings around midnight
                self.sensor_consumptionx.energy_estimate += de
            self._estimate += de
            self.update_native_value(int(self._estimate))

        self._electricity_lastepoch = device.device_timestamp

    def reset_estimate(self):
        self._estimate -= self.native_value  # preserve fraction
        self.update_native_value(0)

    def _schedule_reset(self, _now: datetime):
        with self.exception_warning("_schedule_reset"):
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
            self._reset_unsub = async_track_point_in_time(
                self.manager.hass, self._reset, next_reset
            )
            self.log(self.DEBUG, "_schedule_reset at %s", next_reset.isoformat())

    @callback
    def _reset(self, _now: datetime):
        self._reset_unsub = None
        self.log(self.DEBUG, "_reset at %s", _now.isoformat())
        self.reset_estimate()
        self._schedule_reset(_now)


def namespace_init_electricity(device: "MerossDevice"):
    NamespaceHandler(
        device,
        mn.Appliance_Control_Electricity,
        handler=ElectricitySensor(device, None)._handle_Appliance_Control_Electricity,
    )


class ElectricityXSensor(ElectricitySensor):

    SENSOR_DEFS = ElectricitySensor.SENSOR_DEFS | {
        mc.KEY_VOLTAGE: (
            True,
            MLNumericSensor.DeviceClass.VOLTAGE,
            MLNumericSensor.StateClass.MEASUREMENT,
            1,
            1000,
        ),
        mc.KEY_FACTOR: (
            False,
            MLNumericSensor.DeviceClass.POWER_FACTOR,
            MLNumericSensor.StateClass.MEASUREMENT,
            2,
            1,
        ),
        mc.KEY_MCONSUME: (
            False,
            MLNumericSensor.DeviceClass.ENERGY,
            MLNumericSensor.StateClass.TOTAL,
            0,
            1,
        ),
        mc.KEY_CONSUME: (
            False,
            MLNumericSensor.DeviceClass.ENERGY,
            MLNumericSensor.StateClass.TOTAL,
            0,
            1,
        ),
    }

    __slots__ = ()

    def __init__(self, manager: "MerossDevice", channel: object):
        super().__init__(manager, channel)
        manager.register_parser(self, mn.Appliance_Control_ElectricityX)


class ElectricityXNamespaceHandler(NamespaceHandler):
    """
    This namespace is still pretty unknown.
    Looks like an upgraded version of Appliance.Control.Electricity and currently appears in:
    - em06(Refoss)
    - mop320
    The em06 parsing looks established (not sure it really works..no updates from users so far)
    while the mop is still obscure. While the em06 query is a plain empty dict it might be
    the mop320 needs a 'channel indexed' request payload so we're now (2024-10-11) trying
    the same approach as in ConsumptionH namespace
    """

    def __init__(self, device: "MerossDevice"):
        NamespaceHandler.__init__(
            self,
            device,
            mn.Appliance_Control_ElectricityX,
        )
        # Current approach is to build a sensor for any appearing channel index
        # in digest. This in turns will not directly build the EM06 sensors
        # but they should come when polling.
        self.register_entity_class(ElectricityXSensor, build_from_digest=True)

    def _polling_request_init(self, request_payload_type: mn.RequestPayloadType):
        # TODO: move this device type 'patching' to some 'smart' Namespace grammar
        if self.device.descriptor.type.startswith(mc.TYPE_EM06):
            super()._polling_request_init(mn.RequestPayloadType.DICT)
        else:
            super()._polling_request_init(request_payload_type)


class ConsumptionXSensor(EntityNamespaceMixin, MLNumericSensor):
    ATTR_OFFSET: typing.Final = "offset"
    ATTR_RESET_TS: typing.Final = "reset_ts"

    manager: "MerossDevice"
    ns = mn.Appliance_Control_ConsumptionX

    __slots__ = (
        "offset",
        "reset_ts",
        "energy_estimate",
        "_consumption_last_value",
        "_consumption_last_time",
        "_yesterday_midnight_epoch",
        "_today_midnight_epoch",
        "_tomorrow_midnight_epoch",
    )

    def __init__(self, manager: "MerossDevice"):
        self.offset: int = 0
        self.reset_ts: int = 0
        self.energy_estimate: float = 0.0
        self._consumption_last_value: int | None = None
        self._consumption_last_time: int | None = None
        # these are the device actual EPOCHs of the last midnight
        # and the midnight of they before. midnight epoch(s) are
        # the times at which the device local time trips around
        # midnight (which could be different than GMT tripping of course)
        self._yesterday_midnight_epoch = 0  # 12:00 am yesterday
        self._today_midnight_epoch = 0  # 12:00 am today
        self._tomorrow_midnight_epoch = 0  # 12:00 am tomorrow
        # depending on init order we might not have this ready now...
        sensor_energy_estimate: ElectricitySensor | None = manager.entities.get(mlc.ELECTRICITY_SENSOR_KEY)  # type: ignore
        if sensor_energy_estimate:
            sensor_energy_estimate.sensor_consumptionx = self
        self.extra_state_attributes = {}
        super().__init__(
            manager, None, mlc.CONSUMPTIONX_SENSOR_KEY, self.DeviceClass.ENERGY
        )
        EntityNamespaceHandler(self).polling_response_size_adj(30)

    # interface: MerossEntity
    def set_unavailable(self):
        self._yesterday_midnight_epoch = 0
        self._today_midnight_epoch = 0
        self._tomorrow_midnight_epoch = 0
        return super().set_unavailable()

    async def async_added_to_hass(self):
        await super().async_added_to_hass()
        # state restoration is only needed on cold-start and we have to discriminate
        # from when this happens while the device is already working. In general
        # the sensor state is always kept in the instance even when it's disabled
        # so we don't want to overwrite that should we enable an entity after
        # it has been initialized. Checking native_value here should be enough
        # since it's surely None on boot/initial setup (entities are added before
        # device reading data). If an entity is disabled on startup of course our state
        # will start resetted and our sums will restart (disabled means not interesting
        # anyway)
        if (self.native_value is not None) or self.extra_state_attributes:
            return

        with self.exception_warning("restoring previous state"):
            state = await self.get_last_state_available()
            if state is None:
                return
            # check if the restored sample is fresh enough i.e. it was
            # updated after the device midnight for today..else it is too
            # old to be good. Since we don't have actual device epoch we
            # 'guess' it is nicely synchronized so we'll use our time
            devicetime = self.manager.get_device_datetime(time())
            devicetime_today_midnight = datetime(
                devicetime.year,
                devicetime.month,
                devicetime.day,
                tzinfo=devicetime.tzinfo,
            )
            if state.last_updated < devicetime_today_midnight:
                return
            for _attr_name in (self.ATTR_OFFSET, self.ATTR_RESET_TS):
                if _attr_name in state.attributes:
                    _attr_value = state.attributes[_attr_name]
                    self.extra_state_attributes[_attr_name] = _attr_value
                    # we also set the value as an instance attr for faster access
                    setattr(self, _attr_name, _attr_value)
            # HA adds decimals when the display precision is set for the entity
            # according to this issue #268. In order to try not mess statistics
            # we're reverting to the old design where the sensor state is
            # reported as 'unavailable' when the device is disconnected and so
            # we don't restore the state value at all but just wait for a 'fresh'
            # consumption value from the device. The attributes restoration will
            # instead keep patching the 'consumption reset bug'

    # interface: self
    def reset_consumption(self):
        if self.native_value != 0:
            self.native_value = 0
            self.extra_state_attributes = {}
            self.offset = 0
            self.reset_ts = 0
            self.flush_state()
            self.log(self.DEBUG, "no readings available for new day - resetting")

    def _handle(self, header: dict, payload: dict):
        device = self.manager
        # we'll look through the device array values to see
        # data timestamped (in device time) after last midnight
        # since we usually reset this around midnight localtime
        # the device timezone should be aligned else it will roundtrip
        # against it's own midnight and we'll see a delayed 'sawtooth'
        if device.device_timestamp > self._tomorrow_midnight_epoch:
            # catch the device starting a new day since our last update (yesterday)
            devtime = device.get_device_datetime(device.device_timestamp)
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
                self.DEBUG,
                "updated midnight epochs: yesterday=%s - today=%s - tomorrow=%s",
                str(self._yesterday_midnight_epoch),
                str(self._today_midnight_epoch),
                str(self._tomorrow_midnight_epoch),
            )

        # we're optimizing the payload response_size calculation
        # so our multiple requests are more reliable. If anything
        # goes wrong, the MerossDevice multiple payload managment
        # is smart enough to adapt to wrong estimates
        days = payload[mc.KEY_CONSUMPTIONX]
        days_len = len(days)
        device.namespace_handlers[
            mn.Appliance_Control_ConsumptionX.name
        ].polling_response_size_adj(days_len)
        # the days array contains a month worth of data
        # but we're only interested in the last few days (today
        # and maybe yesterday) so we discard a bunch of
        # elements before sorting (in order to not waste time)
        # checks for 'not enough meaningful data' are post-poned
        # and just for safety since they're unlikely to happen
        # in a normal running environment over few days
        days = [
            day for day in days if day[mc.KEY_TIME] >= self._yesterday_midnight_epoch
        ]
        days_len = len(days)
        if days_len:

            def _get_timestamp(day):
                return day[mc.KEY_TIME]

            days = sorted(days, key=_get_timestamp)
        else:
            self.reset_consumption()
            return

        day_last: dict = days[-1]
        day_last_time: int = day_last[mc.KEY_TIME]

        if day_last_time < self._today_midnight_epoch:
            # this could happen right after midnight when the device
            # should start a new cycle but the consumption is too low
            # (device starts reporting from 1 wh....) so, even if
            # new day has come, new data have not
            self._consumption_last_value = None
            self.reset_consumption()
            return

        # now day_last 'should' contain today data in HA time.
        day_last_value: int = day_last[mc.KEY_VALUE]
        # check if the device tripped its own midnight and started a
        # new day readings
        if days_len > 1 and (
            self.reset_ts != (day_yesterday_time := days[-2][mc.KEY_TIME])
        ):
            # this is the first time after device midnight that we receive new data.
            # in order to fix #264 we're going to set our internal energy offset.
            # This is very dangerous since we must discriminate between faulty
            # resets and good resets from the device. Typically the device resets
            # itself correctly and we have new 0-based readings but we can't
            # reliably tell when the error happens since the 'new' reading could be
            # any positive value depending on actual consumption of the device

            # first off we consider the device readings good
            self.reset_ts = day_yesterday_time
            self.offset = 0
            self.extra_state_attributes = {self.ATTR_RESET_TS: day_yesterday_time}
            if (self._consumption_last_time is not None) and (
                self._consumption_last_time <= day_yesterday_time
            ):
                # In order to fix #264 and any further bug in consumption
                # we'll check it against our ElectricitySensor. Here we're
                # across the device midnight reset so our energy_estimate
                # is trying to measure the effective consumption since the last
                # updated reading of yesterday. The check on _consumption_last_time is
                # to make sure we're not applying any offset when we start 'fresh'
                # reading during a day and HA has no state carried over since
                # midnight on this sensor
                energy_estimate = int(self.energy_estimate) + 1
                if day_last_value > energy_estimate:
                    self.extra_state_attributes[self.ATTR_OFFSET] = self.offset = (
                        day_last_value - energy_estimate
                    )
            self.log(
                self.DEBUG,
                "first consumption reading for new day, offset=%d",
                self.offset,
            )

        elif day_last_value == self._consumption_last_value:
            # no change in consumption..skip updating unless sensor was disconnected
            if self.native_value is None:
                self.native_value = day_last_value - self.offset
                self.flush_state()
            return

        self._consumption_last_time = day_last_time
        self._consumption_last_value = day_last_value
        self.energy_estimate = 0.0
        self.native_value = day_last_value - self.offset
        self.flush_state()
        self.log(self.DEBUG, "updating consumption=%d", day_last_value)


class ConsumptionConfigNamespaceHandler(VoidNamespaceHandler):
    """Suppress processing Appliance.Control.ConsumptionConfig since
    it is already processed at the MQTTConnection message handling."""

    def __init__(self, device: "MerossDevice"):
        super().__init__(device, mn.Appliance_Control_ConsumptionConfig)


class OverTempEnableSwitch(EntityNamespaceMixin, me.MENoChannelMixin, MLSwitch):

    ns = mn.Appliance_Config_OverTemp
    key_value = mc.KEY_ENABLE

    # HA core entity attributes:
    entity_category = me.EntityCategory.CONFIG

    __slots__ = ("sensor_overtemp_type",)

    def __init__(self, manager: "MerossDevice"):
        super().__init__(
            manager, None, "config_overtemp_enable", MLSwitch.DeviceClass.SWITCH
        )
        self.sensor_overtemp_type: MLEnumSensor = MLEnumSensor(
            manager, None, "config_overtemp_type"
        )
        EntityNamespaceHandler(self)

    # interface: MerossToggle
    async def async_shutdown(self):
        await super().async_shutdown()
        self.sensor_overtemp_type = None  # type: ignore

    # interface: self
    def _handle(self, header: dict, payload: dict):
        """{"overTemp": {"enable": 1,"type": 1}}"""
        overtemp = payload[mc.KEY_OVERTEMP]
        if mc.KEY_ENABLE in overtemp:
            self.update_onoff(overtemp[mc.KEY_ENABLE])
        if mc.KEY_TYPE in overtemp:
            self.sensor_overtemp_type.update_native_value(overtemp[mc.KEY_TYPE])
