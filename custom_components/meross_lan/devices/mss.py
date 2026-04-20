from bisect import insort_right
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, override

from homeassistant.util import dt as dt_util

from .. import const as mlc
from ..helpers.entity import EntityNamespaceMixin
from ..merossclient.device.handler import NamespaceHandler
from ..merossclient.protocol import const as mc, namespaces as mn
from ..sensor import EnumParser, SensorParser
from ..switch import SwitchParser

if TYPE_CHECKING:
    from typing import ClassVar, Final, Unpack

    from ..helpers.device import Device, MerossMessage
    from ..merossclient.protocol import types as mt


class _ElectricitySensor(SensorParser):
    """
    This sensor acts as the main parser for 'Electricity' and 'ElectricityX' namespaces
    taking care of power, current, voltage, etc, sensors for the same channel.
    It also implements a trapezoidal estimator for energy consumption. Based on observations
    this estimate is falling a bit behind the consumption reported from the device at least
    when the power is very low (likely due to power readings being a bit off).
    BEWARE: even though this could be a candidate for mixing with EntityNamespaceMixin when
    handling *.Electricity ns, it would instead be wrong when managing *.ElectricityX ns
    since it gets instanced once for every channel.
    """

    if TYPE_CHECKING:
        parent: Final[Device]  # type: ignore[override]

        type InitArgs = SensorParser.InitArgs

        class Args(SensorParser.Args):
            pass

        ENTITY_DEFS: ClassVar[dict[str, type[SensorParser]]]
        sensors: Final[list[SensorParser]]
        sensor_consumptionx: "ConsumptionXSensor | None"
        sensor_power: SensorParser
        # HA core entity attributes:
        native_value: int

    # This is good for both when used as Electiricity and ElectricityX since
    # ns registration will in both cases reach to this ;)
    POLLING_CONFIG_DEFAULT = mlc.POLLING_CONFIG_FASTSENSOR

    init_entity_key = "energy_estimate"
    ENTITY_DEFS = {
        mc.KEY_POWER: SensorParser.ENTITY_DEF(
            entity_key=mc.KEY_POWER,
            key_value=SensorParser.SimpleKeyValue(mc.KEY_POWER),
            device_class=SensorParser.DeviceClass.POWER,
            state_class=SensorParser.StateClass.MEASUREMENT,
            suggested_display_precision=1,
            device_scale=1000,
        ),
        mc.KEY_CURRENT: SensorParser.ENTITY_DEF(
            entity_key=mc.KEY_CURRENT,
            key_value=SensorParser.SimpleKeyValue(mc.KEY_CURRENT),
            device_class=SensorParser.DeviceClass.CURRENT,
            state_class=SensorParser.StateClass.MEASUREMENT,
            suggested_display_precision=1,
            device_scale=1000,
        ),
        mc.KEY_VOLTAGE: SensorParser.ENTITY_DEF(
            entity_key=mc.KEY_VOLTAGE,
            key_value=SensorParser.SimpleKeyValue(mc.KEY_VOLTAGE),
            device_class=SensorParser.DeviceClass.VOLTAGE,
            state_class=SensorParser.StateClass.MEASUREMENT,
            suggested_display_precision=1,
            device_scale=10,
        ),
    }

    # HA core entity attributes:
    _attr_device_class = SensorParser.DeviceClass.ENERGY
    _attr_entity_registry_enabled_default = False

    __slots__ = (
        "_estimate",
        "_electricity_lastepoch",
        "sensors",
        "sensor_consumptionx",
        "sensor_power",
    )

    def __init__(self, *args: "*InitArgs", **kwargs: "Unpack[Args]"):
        self._estimate = 0.0
        self._electricity_lastepoch = 0.0
        self.sensor_consumptionx = None
        kwargs["device_value"] = 0
        super().__init__(*args, **kwargs)
        self._schedule_reset()
        self.sensors = [
            _entity_def(self, ns=self.ns)
            for _entity_def in self.__class__.ENTITY_DEFS.values()
        ]
        self.sensor_power = self.sensors[0]
        # We enable the internal device time checks since the device could report
        # 0 power readings when not able to sync time and/or correctly configured
        self.parent.enable_check_device_time()

    def shutdown(self):
        super().shutdown()
        del self.sensor_consumptionx
        del self.sensor_power
        self.sensors.clear()

    async def async_added_to_hass(self):
        # state restoration is only needed on cold-start and we have to discriminate
        # from when this happens while the device is already working. In general
        # the sensor state is always kept in the instance even when it's disabled
        # so we don't want to overwrite that should we enable an entity after
        # it has been initialized. Checking native_value here should be enough
        # since it's surely 0 on boot/initial setup (entities are added before
        # device reading data). If an entity is disabled on startup of course our state
        # will start resetted and our sums will restart (disabled means not interesting
        # anyway)
        if not self.native_value:
            with self.exception_warning("restoring previous state"):
                state = await self.get_last_state_available()
                if state and (state.last_updated >= dt_util.start_of_local_day()):
                    # state should be an int though but in case we decide some
                    # tweaks here or there this conversion is safer (allowing for a float state)
                    # and more consistent
                    self._estimate = float(state.state)
                    self.native_value = int(self._estimate)
        await SensorParser.async_added_to_hass(self)

    async_will_remove_from_hass = SensorParser.async_will_remove_from_hass  # type: ignore[assignment]

    @override
    def set_available(self):
        self.available = True
        self.flush_state()

    @override
    def __call__(self, payload: dict, /):
        """{"channel": 0, "power": 11000, ...}"""
        device = self.parent
        last_power = self.sensor_power.native_value
        for sensor in self.sensors:
            try:
                sensor(payload)
            except KeyError:
                pass
        power = self.sensor_power.native_value
        # device.device_timestamp 'should be' current epoch of the message
        try:
            # TODO: add a check for 'excessive' timestamp diff and maybe log a
            # warning about possible device time issues (since this is critical for the estimate reliability)
            de = (
                (last_power + power)  # type: ignore
                * (device.device_timestamp - self._electricity_lastepoch)
            ) / 7200
            if self.sensor_consumptionx:
                # we're helping the ConsumptionXSensor to carry on
                # energy accumulation/readings around midnight
                # Keep in mind sensor_consumptionx.energy_estimate is reset
                # at every new consumptionx value change so it is not the same as
                # our self._estimate
                self.sensor_consumptionx.energy_estimate += de
            self._estimate += de
            self.update_native_value(int(self._estimate))
        except TypeError:
            # This is only expected when either last_power or power is None.
            # It should happen once after onlining.
            if (last_power is not None) and (power is not None):
                raise

        self._electricity_lastepoch = device.device_timestamp

    # interface: self
    def _schedule_reset(self, /):
        _now = dt_util.now()
        t = _now + timedelta(days=1)
        t = datetime(year=t.year, month=t.month, day=t.day, tzinfo=t.tzinfo)
        self.schedule_callback((t - _now).total_seconds(), self._reset)
        self.log(self.DEBUG, "_schedule_reset at %s", t)

    def _reset(self, /):
        self._schedule_reset()
        self._estimate -= self.native_value  # preserve fraction
        self.update_native_value(0)


class ElectricitySensor(_ElectricitySensor, EntityNamespaceMixin):

    # skip EntityNamespaceMixin async_added_to_hass and async_will_remove_from_hass since
    # we want to keep polling this ns even when _ElectricitySensor is disabled
    # (we have to since it carries critical data for the energy estimate and ConsumptionXSensor)
    @classmethod
    @override
    def namespace_init(cls, ns: mn.Namespace, device: "Device", /):
        ns_entity = cls(ns, device, ns=ns)
        ns_entity.unique_id = f"{device.id}_{ns_entity.entity_key}"
        ns_entity.handler_ns = ns_entity
        return ns_entity


class ElectricityXSensor(_ElectricitySensor):

    if TYPE_CHECKING:

        class Args(_ElectricitySensor.Args):
            pass

    class MConsumeSensor(SensorParser):
        if TYPE_CHECKING:
            parent: Final[Device]  # type: ignore[override]

        def __init__(self, *args, **kwargs):
            SensorParser.__init__(self, *args, **kwargs)
            # TODO: in 6.x.x we should generalize this mechanism to any ns/device
            try:
                handler_ch: "ConsumptionHNamespaceHandler" = self.parent.ns_handlers[
                    mn.Appliance_Control_ConsumptionH
                ]  # type: ignore
                handler_ch.need_polling(self.index)
            except KeyError:
                pass

        @override
        def update_device_value(self, device_value: int | float, /):
            if SensorParser.update_device_value(self, device_value):
                # We'll use this event to trigger an update of the related
                # ConsumptionHSensor. We'll so ensure  ConsumptionH sensors in em06
                # are effectively instantiated since their list cannot be inferred
                # by any other means for these devices.
                try:
                    handler_ch: (
                        "ConsumptionHNamespaceHandler"
                    ) = self.parent.ns_handlers[
                        mn.Appliance_Control_ConsumptionH
                    ]  # type: ignore
                    handler_ch.need_polling(self.index)
                except KeyError:
                    # we expect ConsumptionH ns handler to be registered when ElextricityX
                    # is used since they go hand in hand. However, better be safe than sorry
                    pass
                return True

    ENTITY_DEFS = _ElectricitySensor.ENTITY_DEFS | {
        mc.KEY_VOLTAGE: SensorParser.ENTITY_DEF(
            entity_key=mc.KEY_VOLTAGE,
            key_value=SensorParser.SimpleKeyValue(mc.KEY_VOLTAGE),
            device_class=SensorParser.DeviceClass.VOLTAGE,
            state_class=SensorParser.StateClass.MEASUREMENT,
            suggested_display_precision=1,
            device_scale=1000,
        ),
        mc.KEY_FACTOR: SensorParser.ENTITY_DEF(
            entity_key=mc.KEY_FACTOR,
            key_value=SensorParser.SimpleKeyValue(mc.KEY_FACTOR),
            device_class=SensorParser.DeviceClass.POWER_FACTOR,
            state_class=SensorParser.StateClass.MEASUREMENT,
            suggested_display_precision=2,
            device_scale=1,
        ),
        mc.KEY_MCONSUME: MConsumeSensor.ENTITY_DEF(
            entity_key=mc.KEY_MCONSUME,
            key_value=SensorParser.SimpleKeyValue(mc.KEY_MCONSUME),
            device_class=SensorParser.DeviceClass.ENERGY,
            state_class=SensorParser.StateClass.TOTAL_INCREASING,  # quick patch for #621 (will be fixed in v6.x.x)
            suggested_display_precision=0,
            device_scale=1,
        ),
    }


class ConsumptionHSensor(SensorParser):

    if TYPE_CHECKING:

        class Args(SensorParser.Args):
            pass

        handler_ns: "ConsumptionHNamespaceHandler"

    init_entity_key = mc.KEY_CONSUMPTIONH
    init_key_value = SensorParser.SimpleKeyValue(mc.KEY_TOTAL)

    _attr_device_class = SensorParser.DeviceClass.ENERGY
    _attr_name = "Consumption"
    _attr_suggested_display_precision = 0

    async def async_added_to_hass(self):
        self.handler_ns.channel_polling_add(self.index)
        await SensorParser.async_added_to_hass(self)

    async def async_will_remove_from_hass(self):
        self.handler_ns.channel_polling_remove(self.index)
        await SensorParser.async_will_remove_from_hass(self)

    @override
    def __call__(self, payload: dict, /):
        """
        {"channel": 1, "total": 958, "data": [{"timestamp": 1721548740, "value": 0}]}
        """
        handler = self.handler_ns
        if self.update_device_value(payload[mc.KEY_TOTAL]):
            # value is changing..reschedule polling sooner
            polling_delay = handler.polling_period * 2
        else:
            # value is steady..postpone next polling
            polling_delay = handler.polling_period * 6
        if handler.channel_polling_remove(self.index):
            handler.channel_polling_add(self.index, polling_delay)


class ConsumptionHNamespaceHandler(NamespaceHandler):
    """
    This namespace carries hourly statistics (over last 24 ours?) of energy consumption
    Appearing in: mts200 - em06 (Refoss) - mop320
    This ns looks tricky since for mts200, the query (payload GET) needs the channel
    index while for em06 this doesn't look necessary (empty query replies full sensor set statistics).
    At any rate, querying the whole em06 ConsumptionH set might be cumbersome (also risking response
    overflow - maybe leading to https://github.com/krahabb/meross_lan/issues/611)
    so we're going to use some 'smart tricks' linked to ElectricityX sensors to
    infer which channels are available on the device and smart-query them.
    Also, we need to come up with a reasonable euristic on which channels are available
    mts200: 1 (channel 0)
    mop320: 3 (channel 0 - 1 - 2) even tho it only has 2 metering channels (0 looks toggling both)
    em06: 6 channels (but the query works without setting any)
    """

    if TYPE_CHECKING:
        indexToPollType = tuple[float, mn.IndexValue]
        """(last_request_epoch, NamespaceParser.index)"""
        _indexes_to_poll: list[indexToPollType]

    __SLOTS__ = ("_indexes_to_poll",)

    def __init__(self, ns: mn.Namespace, device: "Device", /):
        self._indexes_to_poll = []
        NamespaceHandler.__init__(self, ns, device, parser_class=ConsumptionHSensor)
        if len(device.descriptor.channels) > 1:
            self.polling_strategy = ConsumptionHNamespaceHandler.async_poll_probe
        device.enable_check_device_time()

    def channel_polling_add(self, index: mn.IndexValue, delay: float = 0, /):
        # assert not already present ?
        insort_right(
            self._indexes_to_poll,
            (self.parent.polling_epoch + delay, index),
            key=lambda ctp: ctp[0],
        )

    def channel_polling_remove(self, index: mn.IndexValue, /):
        indexes_to_poll = self._indexes_to_poll
        for i in range(len(indexes_to_poll)):
            if indexes_to_poll[i][1] is index:
                del indexes_to_poll[i]
                return True
        return False

    def need_polling(self, index: mn.IndexValue, /):
        """Raise the channel polling priority in the queue."""
        indexes_to_poll = self._indexes_to_poll
        for i in range(len(indexes_to_poll)):
            if indexes_to_poll[i][1] is index:
                if indexes_to_poll[i][0] > self.parent.polling_epoch:
                    del indexes_to_poll[i]
                    insort_right(
                        indexes_to_poll,
                        (self.parent.polling_epoch, index),
                        key=lambda ctp: ctp[0],
                    )
                return

    async def async_poll_probe(self):
        # This poller is mainly used to discover the multiple_response available buffer size
        # since em06 looks like having a way more than our default estimated 2400 (3 * 800) bytes
        # We're then going to try a full poll and see what happens. Also, we're expecting the device
        # to reply with just 3 channels when queried with an empty list (em06).
        if not self._indexes_to_poll:
            return
        self.polling_response_size = (
            NamespaceHandler.HEADER_AVG_SIZE + 3 * self.id.payload_item_size
        )
        self.polling_request_payload.clear()
        await self.parent.async_poll_request(self)
        self.polling_request_payload.append({})
        self.polling_response_size = (
            NamespaceHandler.HEADER_AVG_SIZE + self.id.payload_item_size
        )
        self.polling_strategy = ConsumptionHNamespaceHandler.async_poll_smartchunk

    async def async_poll_smartchunk(self):
        """This has a huge ns response payload so we need to optimize polling.
        We're going to just query a single channel per polling cycle."""
        if not self._indexes_to_poll:
            return
        _poll_epoch, self.polling_request_payload[0] = self._indexes_to_poll[0]
        device = self.parent
        epoch = device.polling_epoch
        if _poll_epoch > epoch:
            # Insert into the lazypoll_requests ordering by least recently polled
            insort_right(
                device._lazypoll_requests, self, key=lambda h: h.last_poll_epoch - epoch
            )
        else:
            await device.async_poll_request_rl(self)

    POLLING_CONFIG_DEFAULT = (
        mlc.PARAM_ENERGY_UPDATE_PERIOD,
        mlc.PARAM_ENERGY_CLOUD_UPDATE_PERIOD,
        async_poll_smartchunk,
    )


class ConsumptionXSensor(SensorParser, EntityNamespaceMixin):

    if TYPE_CHECKING:
        ATTR_OFFSET: Final
        ATTR_RESET_TS: Final

        offset: int
        reset_ts: int
        energy_estimate: float
        _consumption_last_value: int | None
        _consumption_last_time: int | None

    POLLING_CONFIG_DEFAULT = (
        mlc.PARAM_ENERGY_UPDATE_PERIOD,
        mlc.PARAM_ENERGY_CLOUD_UPDATE_PERIOD,
        EntityNamespaceMixin.async_poll_smart,
    )
    init_entity_key = "energy"
    _attr_device_class = SensorParser.DeviceClass.ENERGY

    ATTR_OFFSET = "offset"
    ATTR_RESET_TS = "reset_ts"

    __SLOTS__ = (
        "offset",
        "reset_ts",
        "energy_estimate",
        "_consumption_last_value",
        "_consumption_last_time",
        "_yesterday_midnight_epoch",
        "_today_midnight_epoch",
        "_tomorrow_midnight_epoch",
    )

    def __init__(self, *args, **kwargs):
        self.offset = 0
        self.reset_ts = 0
        self.energy_estimate = 0.0
        self._consumption_last_value = None
        self._consumption_last_time = None
        # these are the device actual EPOCHs of the last midnight
        # and the midnight of they before. midnight epoch(s) are
        # the times at which the device local time trips around
        # midnight (which could be different than GMT tripping of course)
        self._yesterday_midnight_epoch = 0  # 12:00 am yesterday
        self._today_midnight_epoch = 0  # 12:00 am today
        self._tomorrow_midnight_epoch = 0  # 12:00 am tomorrow
        self.extra_state_attributes = {}
        SensorParser.__init__(self, *args, **kwargs)
        self.polling_response_size_adj(30)  # maximum item count for payload
        self.parent.enable_check_device_time()

    def set_unavailable(self):
        self._yesterday_midnight_epoch = 0
        self._today_midnight_epoch = 0
        self._tomorrow_midnight_epoch = 0
        return SensorParser.set_unavailable(self)

    async def async_added_to_hass(self):
        try:
            self.parent.ns_handlers[mn.Appliance_Control_Electricity].sensor_consumptionx = self  # type: ignore
        except KeyError:
            pass
        # state restoration is only needed on cold-start and we have to discriminate
        # from when this happens while the device is already working. In general
        # the sensor state is always kept in the instance even when it's disabled
        # so we don't want to overwrite that should we enable an entity after
        # it has been initialized. Checking native_value here should be enough
        # since it's surely None on boot/initial setup (entities are added before
        # device reading data). If an entity is disabled on startup of course our state
        # will start resetted and our sums will restart (disabled means not interesting
        # anyway)
        if (self.native_value is None) and not self.extra_state_attributes:
            with self.exception_warning("restoring previous state"):
                if state := await self.get_last_state_available():
                    # check if the restored sample is fresh enough i.e. it was
                    # updated after the device midnight for today..else it is too
                    # old to be good. Since we don't have actual device epoch we
                    # 'guess' it is nicely synchronized so we'll use our time
                    _t = self.parent.get_device_datetime(self.time())
                    _t_midnight = datetime(_t.year, _t.month, _t.day, tzinfo=_t.tzinfo)
                    if state.last_updated >= _t_midnight:
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

        await super().async_added_to_hass()

    @override
    def _handle(self, message: "MerossMessage", /):
        device = self.parent
        days = message.payload[mc.KEY_CONSUMPTIONX]
        if device.device_timestamp > self._tomorrow_midnight_epoch:
            # we're optimizing the payload response_size calculation
            # so our multiple requests are more reliable. If anything
            # goes wrong, the Device multiple payload managment
            # is smart enough to adapt to wrong estimates
            self.handler_ns.polling_response_size_adj(len(days))
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
            devtime_tomorrow_midnight = devtime_today_midnight + daydelta
            self._tomorrow_midnight_epoch = devtime_tomorrow_midnight.timestamp()
            devtime_yesterday_midnight = devtime_today_midnight - daydelta
            self._yesterday_midnight_epoch = devtime_yesterday_midnight.timestamp()
            if self.isEnabledFor(self.DEBUG):
                self.log(
                    self.DEBUG,
                    "updated midnight epochs: yesterday=%s - today=%s - tomorrow=%s",
                    devtime_yesterday_midnight,
                    devtime_today_midnight,
                    devtime_tomorrow_midnight,
                )

        # the days array contains a month worth of data
        # but we're only interested in the last few days (today
        # and maybe yesterday) so we discard a bunch of
        # elements before sorting (in order to not waste time)
        # checks for 'not enough meaningful data' are post-poned
        # and just for safety since they're unlikely to happen
        # in a normal running environment over few days
        def _get_timestamp(_day, /):
            return _day[mc.KEY_TIME]

        days = sorted(
            (day for day in days if day[mc.KEY_TIME] >= self._yesterday_midnight_epoch),
            key=_get_timestamp,
        )
        days_len = len(days)
        if not days_len:
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

    # interface: self
    def reset_consumption(self):
        if self.native_value != 0:
            self.native_value = 0
            self.extra_state_attributes = {}
            self.offset = 0
            self.reset_ts = 0
            self.flush_state()
            self.log(self.DEBUG, "no readings available for new day - resetting")


class OverTempEnableSwitch(SwitchParser, EntityNamespaceMixin):

    POLLING_CONFIG_DEFAULT = mlc.POLLING_CONFIG_CONFIGURATION
    init_entity_key = "config_overtemp_enable"
    init_key_value = SwitchParser.SimpleKeyValue(mc.KEY_ENABLE)

    __SLOTS__ = ("sensor_overtemp_type",)

    @override
    def _handle(self, message: "MerossMessage", /):
        """{"overTemp": {"enable": 1,"type": 1}}"""
        self.ns_payload = overtemp = message.payload[mc.KEY_OVERTEMP]
        self.update_device_value(overtemp[self.key_value])
        try:
            type = overtemp[mc.KEY_TYPE]
            self.sensor_overtemp_type.update_device_value(type)
        except AttributeError:
            self.sensor_overtemp_type = EnumParser(
                self, entity_key="config_overtemp_type", native_value=type
            )
        except KeyError:
            pass
