from bisect import insort_right
from datetime import datetime, timedelta
from time import time
from typing import TYPE_CHECKING, override

from homeassistant.core import callback
from homeassistant.helpers.event import async_track_point_in_time
from homeassistant.util import dt as dt_util

from .. import const as mlc
from ..helpers import entity as me
from ..helpers.namespaces import (
    POLLING_STRATEGY_CONF,
    EntityNamespaceMixin,
    NamespaceHandler,
    mc,
    mn,
)
from ..sensor import MLEnumSensor, MLNumericSensor
from ..switch import MLSwitch

if TYPE_CHECKING:
    from typing import ClassVar, Final, Unpack

    from ..helpers.device import Device, MerossMessage
    from ..merossclient.protocol import types as mt


class ElectricitySensor(me.MEAlwaysAvailableMixin, MLNumericSensor):
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
        manager: Device

        # not setting 'entity_key' in EntityDef will mark the entity as 'not required'
        # (see __init__)
        ENTITY_DEFS: ClassVar[dict[str, MLNumericSensor.EntityDef["MLNumericSensor"]]]
        # HA core entity attributes:
        native_value: int

        sensor_consumptionx: "ConsumptionXSensor | None"
        sensor_power: MLNumericSensor

    ns = mn.Appliance_Control_Electricity
    ENTITY_KEY = "energy_estimate"
    ENTITY_DEFS = {
        mc.KEY_CURRENT: MLNumericSensor.ENTITY_DEF(
            entity_key=mc.KEY_CURRENT,
            device_class=MLNumericSensor.DeviceClass.CURRENT,
            state_class=MLNumericSensor.StateClass.MEASUREMENT,
            suggested_display_precision=1,
            device_scale=1000,
        ),
        mc.KEY_POWER: MLNumericSensor.ENTITY_DEF(
            entity_key=mc.KEY_POWER,
            device_class=MLNumericSensor.DeviceClass.POWER,
            state_class=MLNumericSensor.StateClass.MEASUREMENT,
            suggested_display_precision=1,
            device_scale=1000,
        ),
        mc.KEY_VOLTAGE: MLNumericSensor.ENTITY_DEF(
            entity_key=mc.KEY_VOLTAGE,
            device_class=MLNumericSensor.DeviceClass.VOLTAGE,
            state_class=MLNumericSensor.StateClass.MEASUREMENT,
            suggested_display_precision=1,
            device_scale=10,
        ),
    }

    # HA core entity attributes:
    _attr_device_class = MLNumericSensor.DeviceClass.ENERGY
    _attr_entity_registry_enabled_default = False

    __slots__ = (
        "_estimate",
        "_electricity_lastepoch",
        "_reset_unsub",
        "sensor_consumptionx",
        "sensor_power",
    )

    def __init__(
        self,
        manager: "Device",
        channel: object | None,
        /,
        **kwargs: "Unpack[ElectricitySensor.Args]",
    ):
        self._estimate = 0.0
        self._electricity_lastepoch = 0.0
        self._reset_unsub = None
        # depending on init order we might not have this ready now...
        self.sensor_consumptionx = manager.entities.get(ConsumptionXSensor.ENTITY_KEY)  # type: ignore
        # here entity_key is the 'legacy' EnergyEstimateSensor one to mantain compatibility
        kwargs["device_value"] = 0
        super().__init__(manager, channel, **kwargs)
        self._schedule_reset(dt_util.now())
        for entity_def in self.ENTITY_DEFS.values():
            if "entity_key" in entity_def.kwargs:
                entity_def.type(
                    manager,
                    channel,
                    **entity_def.kwargs,
                )
        self.sensor_power = manager.entities[
            mc.KEY_POWER if channel is None else f"{channel}_{mc.KEY_POWER}"
        ]  # type: ignore

    async def async_shutdown(self):
        if self._reset_unsub:
            self._reset_unsub()
            self._reset_unsub = None
        await super().async_shutdown()
        del self.sensor_consumptionx
        del self.sensor_power

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
    def _handle_Appliance_Control_Electricity(self, message: "MerossMessage", /):
        # BEWARE: this indirection is needed since _parse is also used in ElectricityX
        self._parse(message.payload[mc.KEY_ELECTRICITY])

    def _parse(self, payload: dict, /):
        """{"channel": 0, "power": 11000, ...}"""
        device = self.manager
        entities = device.entities

        last_power = self.sensor_power.native_value

        for key in self.ENTITY_DEFS:
            try:
                entities[
                    key if self.channel is None else f"{self.channel}_{key}"
                ].update_device_value(payload[key])
            except KeyError:
                if key in payload:
                    entity_def = self.ENTITY_DEFS[key]
                    kwargs = dict(entity_def.kwargs)
                    kwargs["entity_key"] = key
                    kwargs["device_value"] = payload[key]
                    entity_def.type(self.manager, self.channel, **kwargs)

        power = self.sensor_power.native_value
        # device.device_timestamp 'should be' current epoch of the message
        try:
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
        except TypeError:
            # This is only expected when either last_power or power is None.
            # It should happen once after onlining or when the device
            # is not providing power readings for some reason.
            if not power:
                # might be an indication of issue #367 where the problem lies in missing
                # device timezone configuration. This check is mostly about (power == 0)
                # i.e. a formally good reading but likely indication of misbehaving device
                device.check_device_timezone()
            if (last_power is not None) and (power is not None):
                raise

        self._electricity_lastepoch = device.device_timestamp

    def _schedule_reset(self, _now: datetime, /):
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
                self.manager.api.hass, self._reset, next_reset
            )
            self.log(self.DEBUG, "_schedule_reset at %s", next_reset.isoformat())

    @callback
    def _reset(self, _now: datetime, /):
        self._reset_unsub = None
        self.log(self.DEBUG, "_reset at %s", _now.isoformat())
        self._estimate -= self.native_value  # preserve fraction
        self.update_native_value(0)
        self._schedule_reset(_now)


def namespace_init_electricity(
    device: "Device", ns=mn.Appliance_Control_Electricity, /
):
    NamespaceHandler(
        device,
        ns,
        handler=ElectricitySensor(device, None)._handle_Appliance_Control_Electricity,
    )


class ElectricityXSensor(ElectricitySensor):

    ns = mn.Appliance_Control_ElectricityX

    class MConsumeSensor(MLNumericSensor):
        manager: "Device"

        def __init__(self, manager: "Device", channel, **kwargs):
            MLNumericSensor.__init__(self, manager, channel, **kwargs)
            # TODO: in 6.x.x we should generalize this mechanism to any ns/device
            try:
                handler_ch: "ConsumptionHNamespaceHandler" = manager.ns_handlers[
                    mn.Appliance_Control_ConsumptionH
                ]  # type: ignore
                handler_ch.need_polling(channel)
            except KeyError:
                pass

        @override
        def update_device_value(self, device_value: int | float, /):
            if MLNumericSensor.update_device_value(self, device_value):
                # We'll use this event to trigger an update of the related
                # ConsumptionHSensor. We'll so ensure  ConsumptionH sensors in em06
                # are effectively instantiated since their list cannot be inferred
                # by any other means for these devices.
                try:
                    handler_ch: (
                        "ConsumptionHNamespaceHandler"
                    ) = self.manager.ns_handlers[
                        mn.Appliance_Control_ConsumptionH
                    ]  # type: ignore
                    handler_ch.need_polling(self.channel)
                except KeyError:
                    # we expect ConsumptionH ns handler to be registered when ElextricityX
                    # is used since they go hand in hand. However, better be safe than sorry
                    pass
                return True

    ENTITY_DEFS = ElectricitySensor.ENTITY_DEFS | {
        mc.KEY_VOLTAGE: MLNumericSensor.ENTITY_DEF(
            entity_key=mc.KEY_VOLTAGE,
            device_class=MLNumericSensor.DeviceClass.VOLTAGE,
            state_class=MLNumericSensor.StateClass.MEASUREMENT,
            suggested_display_precision=1,
            device_scale=1000,
        ),
        mc.KEY_FACTOR: MLNumericSensor.ENTITY_DEF(
            device_class=MLNumericSensor.DeviceClass.POWER_FACTOR,
            state_class=MLNumericSensor.StateClass.MEASUREMENT,
            suggested_display_precision=2,
            device_scale=1,
        ),
        mc.KEY_MCONSUME: MConsumeSensor.ENTITY_DEF(
            device_class=MLNumericSensor.DeviceClass.ENERGY,
            state_class=MLNumericSensor.StateClass.TOTAL_INCREASING,  # quick patch for #621 (will be fixed in v6.x.x)
            suggested_display_precision=0,
            device_scale=1,
        ),
    }

    def __init__(
        self,
        manager: "Device",
        channel: object,
        /,
        **kwargs: "Unpack[ElectricityXSensor.Args]",
    ):
        ElectricitySensor.__init__(self, manager, channel, **kwargs)
        manager.register_parser_entity(self)


class ConsumptionHSensor(MLNumericSensor):

    if TYPE_CHECKING:
        handler_ns: "ConsumptionHNamespaceHandler"

    ENTITY_KEY = mc.KEY_CONSUMPTIONH
    ns = mn.Appliance_Control_ConsumptionH
    key_value = mc.KEY_TOTAL

    _attr_device_class = MLNumericSensor.DeviceClass.ENERGY
    _attr_suggested_display_precision = 0

    def __init__(
        self, manager: "Device", channel, /, **kwargs: "Unpack[ConsumptionHSensor.Args]"
    ):
        kwargs["name"] = "Consumption"
        MLNumericSensor.__init__(self, manager, channel, **kwargs)
        manager.register_parser_entity(self)

    async def async_added_to_hass(self):
        self.handler_ns.channel_polling_add(self.channel)
        return await MLNumericSensor.async_added_to_hass(self)

    async def async_will_remove_from_hass(self):
        self.handler_ns.channel_polling_remove(self.channel)
        return await MLNumericSensor.async_will_remove_from_hass(self)

    def _parse_consumptionH(self, payload: dict):
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
        if handler.channel_polling_remove(self.channel):
            handler.channel_polling_add(self.channel, polling_delay)


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
        ChannelToPollType = tuple[float, object]
        """(last_request_epoch, channel)"""
        _channels_to_poll: list[ChannelToPollType]
        # TODO: reconcile this member with polling_request_channels in base cls

    __slots__ = ("_channels_to_poll",)

    def __init__(self, device: "Device", ns=mn.Appliance_Control_ConsumptionH, /):
        self._channels_to_poll = []
        NamespaceHandler.__init__(self, device, ns)
        self.register_entity_class(ConsumptionHSensor, device.descriptor.channels)
        self.polling_strategy = ConsumptionHNamespaceHandler.async_poll_probe  # type: ignore

    @override
    def polling_request_add_channel(
        self, channel, extra: "mt.MerossPayloadType" = mn.EMPTY_DICT, /
    ):
        # disable polling_request_channels setup since we're overriding the default
        # polling mechanics
        pass

    def channel_polling_add(self, channel, delay: float = 0, /):
        # assert not already present ?
        insort_right(
            self._channels_to_poll,
            (self.device._polling_epoch + delay, channel),
            key=lambda ctp: ctp[0],
        )

    def channel_polling_remove(self, channel, /):
        channels_to_poll = self._channels_to_poll
        for i in range(len(channels_to_poll)):
            if channels_to_poll[i][1] == channel:
                del channels_to_poll[i]
                return True
        return False

    def need_polling(self, channel, /):
        """Raise the channel polling priority in the queue."""
        channels_to_poll = self._channels_to_poll
        for i in range(len(channels_to_poll)):
            if channels_to_poll[i][1] == channel:
                if channels_to_poll[i][0] > self.device._polling_epoch:
                    del channels_to_poll[i]
                    insort_right(
                        channels_to_poll,
                        (self.device._polling_epoch, channel),
                        key=lambda ctp: ctp[0],
                    )
                return

    async def async_poll_probe(self):
        # This poller is mainly used to discover the multiple_response available buffer size
        # since em06 looks like having a way more than our default estimated 2400 (3 * 800) bytes
        # We're then going to try a full poll and see what happens. Also, we're expecting the device
        # to reply with just 3 channels when queried with an empty list (em06).
        if not self._channels_to_poll:
            return
        self.polling_response_size = (
            mlc.PARAM_HEADER_SIZE + 3 * self.polling_response_item_size
        )
        await self.device.async_request_poll(self)
        self.polling_request_channels.append({})
        self.polling_response_size = (
            mlc.PARAM_HEADER_SIZE + self.polling_response_item_size
        )
        self.polling_strategy = ConsumptionHNamespaceHandler.async_poll_smartchunk  # type: ignore

    async def async_poll_smartchunk(self):
        """This has a huge ns response payload so we need to optimize polling.
        We're going to just query a single channel per polling cycle."""
        if not self._channels_to_poll:
            return
        _poll_epoch, channel = self._channels_to_poll[0]
        self.polling_request_channels[0][self.ns.key_channel] = channel
        device = self.device
        epoch = device._polling_epoch
        if _poll_epoch > epoch:
            # Insert into the lazypoll_requests ordering by least recently polled
            insort_right(
                device._lazypoll_requests, self, key=lambda h: h.lastrequest - epoch
            )
        else:
            await device.async_request_smartpoll(self)


class ConsumptionXSensor(EntityNamespaceMixin, MLNumericSensor):

    if TYPE_CHECKING:
        manager: "Device"

        ATTR_OFFSET: Final
        ATTR_RESET_TS: Final

        offset: int
        reset_ts: int
        energy_estimate: float
        _consumption_last_value: int | None
        _consumption_last_time: int | None

    ENTITY_KEY = "energy"
    ns = mn.Appliance_Control_ConsumptionX

    ATTR_OFFSET = "offset"
    ATTR_RESET_TS = "reset_ts"

    _attr_device_class = MLNumericSensor.DeviceClass.ENERGY

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

    def __init__(self, manager: "Device", channel, /):
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
        # depending on init order we might not have this ready now...
        sensor_energy_estimate: ElectricitySensor | None = manager.entities.get(ElectricitySensor.ENTITY_KEY)  # type: ignore
        if sensor_energy_estimate:
            sensor_energy_estimate.sensor_consumptionx = self
        self.extra_state_attributes = {}
        super().__init__(manager, channel)

    # interface: MLEntity
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

    def _handle(self, message: "MerossMessage", /):
        device = self.manager
        days = message.payload[mc.KEY_CONSUMPTIONX]

        if device.device_timestamp > self._tomorrow_midnight_epoch:
            # we're optimizing the payload response_size calculation
            # so our multiple requests are more reliable. If anything
            # goes wrong, the Device multiple payload managment
            # is smart enough to adapt to wrong estimates
            device.ns_handlers[
                mn.Appliance_Control_ConsumptionX
            ].polling_response_size_adj(len(days))
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
                    devtime_yesterday_midnight.isoformat(),
                    devtime_today_midnight.isoformat(),
                    devtime_tomorrow_midnight.isoformat(),
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


class OverTempEnableSwitch(EntityNamespaceMixin, MLSwitch):

    ENTITY_KEY = "config_overtemp_enable"
    ns = mn.Appliance_Config_OverTemp
    key_value = mc.KEY_ENABLE

    __slots__ = ("sensor_overtemp_type",)

    # interface: self
    @override
    def _handle(self, message: "MerossMessage", /):
        """{"overTemp": {"enable": 1,"type": 1}}"""
        overtemp = message.payload[mc.KEY_OVERTEMP]
        self._parse(overtemp)
        try:
            self.sensor_overtemp_type.update_native_value(overtemp[mc.KEY_TYPE])
        except AttributeError:
            self.sensor_overtemp_type = MLEnumSensor(
                self.manager,
                self.channel,
                entity_key="config_overtemp_type",
                native_value=overtemp[mc.KEY_TYPE],
            )
        except KeyError:
            pass


POLLING_STRATEGY_CONF.update(
    {
        mn.Appliance_Config_OverTemp: (
            mlc.PARAM_CONFIG_UPDATE_PERIOD,
            mlc.PARAM_CLOUDMQTT_UPDATE_PERIOD,
            40,
            NamespaceHandler.async_poll_smart,
        ),
        mn.Appliance_Control_ConsumptionH: (
            mlc.PARAM_ENERGY_UPDATE_PERIOD,
            mlc.PARAM_ENERGY_UPDATE_CLOUD_PERIOD,
            1900,
            NamespaceHandler.async_poll_smart,
        ),
        mn.Appliance_Control_ConsumptionX: (
            mlc.PARAM_ENERGY_UPDATE_PERIOD,
            mlc.PARAM_ENERGY_UPDATE_CLOUD_PERIOD,
            53,
            NamespaceHandler.async_poll_smart,
        ),
        mn.Appliance_Control_Electricity: (
            mlc.PARAM_SENSOR_FAST_UPDATE_PERIOD,
            mlc.PARAM_SENSOR_FAST_UPDATE_CLOUD_PERIOD,
            130,
            NamespaceHandler.async_poll_smart,
        ),
        mn.Appliance_Control_ElectricityX: (
            mlc.PARAM_SENSOR_FAST_UPDATE_PERIOD,
            mlc.PARAM_SENSOR_FAST_UPDATE_CLOUD_PERIOD,
            100,
            NamespaceHandler.async_poll_smart,
        ),
    }
)
