import typing

from homeassistant.components import cover
from homeassistant.exceptions import InvalidStateError

from . import meross_entity as me
from .const import CONF_PROTOCOL_HTTP, PARAM_ROLLERSHUTTER_TRANSITION_POLL_TIMEOUT
from .helpers import schedule_async_callback, versiontuple
from .merossclient import const as mc
from .number import MLConfigNumber

if typing.TYPE_CHECKING:
    import asyncio

    from .meross_device import MerossDevice


# rollershutter extra attributes
EXTRA_ATTR_POSITION_NATIVE = "position_native"


async def async_setup_entry(hass, config_entry, async_add_devices):
    me.platform_setup_entry(hass, config_entry, async_add_devices, cover.DOMAIN)


class MLCover(me.MerossEntity, cover.CoverEntity):

    ENTITY_COMPONENT = cover
    PLATFORM = cover.DOMAIN
    DeviceClass = cover.CoverDeviceClass
    EntityFeature = cover.CoverEntityFeature

    manager: "MerossDevice"

    # HA core entity attributes:
    is_closed: bool | None
    is_closing: bool
    is_opening: bool

    __slots__ = (
        "is_closed",
        "is_closing",
        "is_opening",
        "_transition_unsub",
        "_transition_end_unsub",
    )

    def __init__(
        self,
        manager: "MerossDevice",
        channel: object | None,
        device_class: "MLCover.DeviceClass",
    ):
        self.is_closed = None
        self.is_closing = False
        self.is_opening = False
        self._transition_unsub: "asyncio.TimerHandle | None" = None
        self._transition_end_unsub: "asyncio.TimerHandle | None" = None
        super().__init__(manager, channel, None, device_class)

    # interface: MerossEntity
    async def async_shutdown(self):
        self._transition_cancel()
        await super().async_shutdown()

    async def async_will_remove_from_hass(self):
        self._transition_cancel()
        await super().async_will_remove_from_hass()

    def set_unavailable(self):
        self._transition_cancel()
        self.is_closed = None
        self.is_closing = False
        self.is_opening = False
        super().set_unavailable()

    # interface: self
    def _transition_cancel(self):
        if self._transition_end_unsub:
            self._transition_end_unsub.cancel()
            self._transition_end_unsub = None
        if self._transition_unsub:
            self._transition_unsub.cancel()
            self._transition_unsub = None


class MLRollerShutter(MLCover):
    """
    MRS100 SHUTTER ENTITY
    """

    # HA core entity attributes:
    assumed_state = True
    current_cover_position: int | None
    supported_features: cover.CoverEntityFeature

    __slots__ = (
        "current_cover_position",
        "supported_features",
        "number_signalOpen",
        "number_signalClose",
        "_mrs_state",
        "_position_native",
        "_position_native_isgood",
        "_position_start",
        "_position_starttime",
    )

    def __init__(self, manager: "MerossDevice"):
        self.current_cover_position = None
        self.supported_features = (
            MLCover.EntityFeature.OPEN
            | MLCover.EntityFeature.CLOSE
            | MLCover.EntityFeature.STOP
        )
        self.extra_state_attributes = {}
        self._mrs_state = None
        self._position_native = None  # as reported by the device
        self._position_start = 0  # set when when we're controlling a timed position
        self._position_starttime = 0  # epoch of transition start
        descriptor = manager.descriptor
        # flag indicating the device position is reliable (#227)
        # this will anyway be set in case we 'decode' a meaningful device position
        try:
            fw_version = versiontuple(descriptor.firmwareVersion)
            if fw_version >= (6, 6, 6):
                self._position_native_isgood = True
                self.supported_features |= MLCover.EntityFeature.SET_POSITION
            else:
                self._position_native_isgood = False
                if fw_version <= (2, 1, 4):
                    # trying to detect if ns_multiple is offending
                    # 2.1.4 devices (#419)
                    manager.disable_multiple()

        except Exception:
            self._position_native_isgood = False
        super().__init__(manager, 0, MLCover.DeviceClass.SHUTTER)
        self.number_signalOpen = MLRollerShutterConfigNumber(self, mc.KEY_SIGNALOPEN)
        self.number_signalClose = MLRollerShutterConfigNumber(self, mc.KEY_SIGNALCLOSE)
        if mc.NS_APPLIANCE_ROLLERSHUTTER_ADJUST in descriptor.ability:
            # unknown use: actually the polling period is set on a very high timeout
            manager.register_parser(mc.NS_APPLIANCE_ROLLERSHUTTER_ADJUST, self)
        manager.register_parser(mc.NS_APPLIANCE_ROLLERSHUTTER_CONFIG, self)
        manager.register_parser(mc.NS_APPLIANCE_ROLLERSHUTTER_POSITION, self)
        manager.register_parser(mc.NS_APPLIANCE_ROLLERSHUTTER_STATE, self)

    async def async_added_to_hass(self):
        await super().async_added_to_hass()
        """
        we're trying to recover the 'timed' position from previous state
        if it happens it wasn't updated too far in time
        """
        with self.exception_warning("restoring previous state"):
            if last_state := await self.get_last_state_available():
                _attr = last_state.attributes  # type: ignore
                if not self._position_native_isgood:
                    # at this stage, the euristic on fw version doesn't say anything
                    if EXTRA_ATTR_POSITION_NATIVE in _attr:
                        # this means we haven't detected (so far) a reliable 'native_position'
                        # so we restore the cover position (which was emulated)
                        self.extra_state_attributes[EXTRA_ATTR_POSITION_NATIVE] = _attr[
                            EXTRA_ATTR_POSITION_NATIVE
                        ]
                        if cover.ATTR_CURRENT_POSITION in _attr:
                            self.current_cover_position = _attr[
                                cover.ATTR_CURRENT_POSITION
                            ]
                            self.supported_features |= (
                                MLCover.EntityFeature.SET_POSITION
                            )

    async def async_open_cover(self, **kwargs):
        await self.async_request_position(mc.ROLLERSHUTTER_POSITION_OPENED)

    async def async_close_cover(self, **kwargs):
        await self.async_request_position(mc.ROLLERSHUTTER_POSITION_CLOSED)

    async def async_set_cover_position(self, **kwargs):
        position = kwargs[cover.ATTR_POSITION]
        if (
            self._position_native_isgood
            or (position == mc.ROLLERSHUTTER_POSITION_OPENED)
            or (position == mc.ROLLERSHUTTER_POSITION_CLOSED)
        ):
            # ensure a full 'untimed' run when asked for
            # fully opened/closed (#170)
            await self.async_request_position(position)
        else:
            # this is the estimate: could be None on very first run
            # or when the entity state is not properly restored anyway
            current_position = self.current_cover_position
            if current_position is None:
                raise InvalidStateError(
                    "Cannot estimate command direction. Please use open_cover or close_cover"
                )
            if position > current_position:
                timeout = (
                    (position - current_position)
                    * (self.number_signalOpen.device_value or 30000)
                ) / 100000
                position = mc.ROLLERSHUTTER_POSITION_OPENED
            elif position < current_position:
                timeout = (
                    (current_position - position)
                    * (self.number_signalClose.device_value or 30000)
                ) / 100000
                position = mc.ROLLERSHUTTER_POSITION_CLOSED
            else:
                return  # No-Op
            if await self.async_request_position(position):
                self._transition_end_unsub = schedule_async_callback(
                    self.hass, timeout, self._async_transition_end_callback
                )

    async def async_stop_cover(self, **kwargs):
        await self.async_request_position(mc.ROLLERSHUTTER_POSITION_STOP)

    async def async_request_position(self, position: int):
        self._transition_cancel()
        """
        TODO: looks like the mrs100 doesn't love set_position in multiple req.
        update 16-05-2024: it might be the problem lies in the payloads sent as
        lists. The mrs100 is under investigation following #419 and #321.

        manager = self.manager
        channel = self.channel
        if (manager.multiple_max >= 3) and (
            responses := await manager.async_multiple_requests_ack(
                (
                    (
                        mc.NS_APPLIANCE_ROLLERSHUTTER_POSITION,
                        mc.METHOD_SET,
                        {
                            mc.KEY_POSITION: [
                                {
                                    mc.KEY_CHANNEL: channel,
                                    mc.KEY_POSITION: position,
                                }
                            ]
                        },
                    ),
                    (
                        mc.NS_APPLIANCE_ROLLERSHUTTER_STATE,
                        mc.METHOD_GET,
                        {mc.KEY_STATE: [{mc.KEY_CHANNEL: channel}]},
                    ),
                    (
                        mc.NS_APPLIANCE_ROLLERSHUTTER_POSITION,
                        mc.METHOD_GET,
                        {mc.KEY_POSITION: [{mc.KEY_CHANNEL: channel}]},
                    ),
                )
            )
        ):
            # we expect a full success (3 responses) 99% of the times
            # since the only reason for failing is the device not supporting
            # ns_multiple (unlikely) or the response being truncated due to
            # overflow (unlikely too)
            # At this stage the responses are already processed by the MerossDevice
            # interface and we should already be 'in transition'
            if responses[0][mc.KEY_HEADER][mc.KEY_METHOD] == mc.METHOD_SETACK:
                if (
                    (len(responses) == 3)
                    and (responses[1][mc.KEY_HEADER][mc.KEY_METHOD] == mc.METHOD_GETACK)
                    and (responses[2][mc.KEY_HEADER][mc.KEY_METHOD] == mc.METHOD_GETACK)
                ):
                    # our state machine is already updated since the STATE and POSITION
                    # messages were correctly processed
                    return True

                if (
                    not self._transition_unsub
                    and position != mc.ROLLERSHUTTER_POSITION_STOP
                ):
                    # this could happen if the shutter was already 'at position'
                    # so that it didn't start an internal transition (guessing)
                    # or if the 2nd message in our requests failed somehow
                    # at any rate, we'll monitor the state
                    await self._async_transition_callback()
                return True
        """

        # in case the ns_multiple didn't succesfully kick-in we'll
        # fallback to the legacy procedure
        if await self.manager.async_request_ack(
            mc.NS_APPLIANCE_ROLLERSHUTTER_POSITION,
            mc.METHOD_SET,
            {
                mc.KEY_POSITION: {
                    mc.KEY_CHANNEL: self.channel,
                    mc.KEY_POSITION: position,
                }
            },
        ):
            # re-ensure current transitions are clean after await
            self._transition_cancel()
            await self._async_transition_callback()
            return True

    def set_unavailable(self):
        self._mrs_state = None
        super().set_unavailable()

    def _parse_adjust(self, payload: dict):
        # payload = {"channel": 0, "status": 0}
        for key, value in payload.items():
            if key != mc.KEY_CHANNEL:
                self.extra_state_attributes[f"adjust_{key}"] = value

    def _parse_config(self, payload: dict):
        # payload = {"channel": 0, "signalOpen": 50000, "signalClose": 50000}
        if mc.KEY_SIGNALOPEN in payload:
            self.number_signalOpen.update_device_value(payload[mc.KEY_SIGNALOPEN])
        if mc.KEY_SIGNALCLOSE in payload:
            self.number_signalClose.update_device_value(payload[mc.KEY_SIGNALCLOSE])

    def _parse_position(self, payload: dict):
        """
        legacy devices only reported 0 or 100 as position
        so we used to store this as an extra attribute and perform
        a trajectory calculation to emulate time based positioning
        now (#227) we'll detect devices reporting 'actual' good
        positioning and switch entity behaviour to trust this value
        bypassing all of the 'time based' emulation
        """
        position = payload[mc.KEY_POSITION]

        if self._position_native_isgood:
            if position != self.current_cover_position:
                self.current_cover_position = position
                self.is_closed = position == mc.ROLLERSHUTTER_POSITION_CLOSED
                self.flush_state()
            return

        if position == self._position_native:
            # no news...
            return

        if (position > 0) and (position < 100):
            # detecting a device reporting 'good' positions
            self._position_native_isgood = True
            self._position_native = None
            self.is_closed = False
            self.extra_state_attributes.pop(EXTRA_ATTR_POSITION_NATIVE, None)
            self.supported_features |= MLCover.EntityFeature.SET_POSITION
            self.current_cover_position = position
        else:
            self._position_native = position
            self.is_closed = position == mc.ROLLERSHUTTER_POSITION_CLOSED
            self.extra_state_attributes[EXTRA_ATTR_POSITION_NATIVE] = position
            if self.current_cover_position is None:
                # only happening when we didn't restore state on devices
                # which are likely not supporting native positioning
                # at this stage we'll enable set_position anyway and
                # trusting the device position as the better guess
                # If current_cover_position is already set, it represents the
                # emulated state and so we don't touch it
                self.supported_features |= MLCover.EntityFeature.SET_POSITION
                self.current_cover_position = position

        self.flush_state()

    def _parse_state(self, payload: dict):
        state = payload[mc.KEY_STATE]
        if not self._position_native_isgood:
            epoch = self.manager.lastresponse
            if self.is_opening:
                self.current_cover_position = round(
                    self._position_start
                    + ((epoch - self._position_starttime) * 100000)
                    / (self.number_signalOpen.device_value or 30000)
                )
                if self.current_cover_position > mc.ROLLERSHUTTER_POSITION_OPENED:
                    self.current_cover_position = mc.ROLLERSHUTTER_POSITION_OPENED
                self._mrs_state = None  # ensure flushing state
            elif self.is_closing:
                self.current_cover_position = round(
                    self._position_start
                    - ((epoch - self._position_starttime) * 100000)
                    / (self.number_signalClose.device_value or 30000)
                )
                if self.current_cover_position < mc.ROLLERSHUTTER_POSITION_CLOSED:
                    self.current_cover_position = mc.ROLLERSHUTTER_POSITION_CLOSED
                self._mrs_state = None  # ensure flushing state

            if state == mc.ROLLERSHUTTER_STATE_OPENING:
                if not self.is_opening:
                    if self.current_cover_position is None:
                        # this should never really happen since we've
                        # already set current_cover_position in _parse_position
                        self.current_cover_position = mc.ROLLERSHUTTER_POSITION_CLOSED
                        self.supported_features |= MLCover.EntityFeature.SET_POSITION
                    self._position_start = self.current_cover_position
                    self._position_starttime = epoch
            elif state == mc.ROLLERSHUTTER_STATE_CLOSING:
                if not self.is_closing:
                    if self.current_cover_position is None:
                        self.current_cover_position = mc.ROLLERSHUTTER_POSITION_OPENED
                        self.supported_features |= MLCover.EntityFeature.SET_POSITION
                    self._position_start = self.current_cover_position
                    self._position_starttime = epoch

        if self._mrs_state != state:
            self._mrs_state = state
            self.is_closed = (
                self.current_cover_position == mc.ROLLERSHUTTER_POSITION_CLOSED
            )
            if state == mc.ROLLERSHUTTER_STATE_IDLE:
                self.is_closing = False
                self.is_opening = False
            else:
                self.is_closing = state == mc.ROLLERSHUTTER_STATE_CLOSING
                self.is_opening = not self.is_closing
                if not self._transition_unsub:
                    # ensure we 'follow' cover movement
                    self._transition_unsub = schedule_async_callback(
                        self.hass,
                        PARAM_ROLLERSHUTTER_TRANSITION_POLL_TIMEOUT,
                        self._async_transition_callback,
                    )
            self.flush_state()

        if self._transition_unsub and (state == mc.ROLLERSHUTTER_STATE_IDLE):
            self._transition_cancel()

    async def _async_transition_callback(self):
        """Schedule a repetitive callback when we detect or suspect shutter movement.
        It will be invalidated only when a successful state message is parsed stating
        there's no movement.
        This is a very 'gentle' polling happening only on HTTP when we're sure we're
        not receiving MQTT updates. If device was configured for MQTT only we could
        not setup this at all."""
        self._transition_unsub = schedule_async_callback(
            self.hass,
            PARAM_ROLLERSHUTTER_TRANSITION_POLL_TIMEOUT,
            self._async_transition_callback,
        )
        manager = self.manager
        if (
            manager.curr_protocol is CONF_PROTOCOL_HTTP and not manager._mqtt_active
        ) or (self._mrs_state == mc.ROLLERSHUTTER_STATE_IDLE):
            p_channel_payload = []
            if manager.multiple_max >= 2:
                await manager.async_multiple_requests_ack(
                    (
                        (
                            mc.NS_APPLIANCE_ROLLERSHUTTER_STATE,
                            mc.METHOD_GET,
                            {mc.KEY_STATE: p_channel_payload},
                        ),
                        (
                            mc.NS_APPLIANCE_ROLLERSHUTTER_POSITION,
                            mc.METHOD_GET,
                            {mc.KEY_POSITION: p_channel_payload},
                        ),
                    )
                )
            else:
                await manager.async_request(
                    mc.NS_APPLIANCE_ROLLERSHUTTER_STATE,
                    mc.METHOD_GET,
                    {mc.KEY_STATE: p_channel_payload},
                )
                if self._position_native_isgood:
                    await manager.async_request(
                        mc.NS_APPLIANCE_ROLLERSHUTTER_POSITION,
                        mc.METHOD_GET,
                        {mc.KEY_POSITION: p_channel_payload},
                    )

    async def _async_transition_end_callback(self):
        self._transition_end_unsub = None
        self.log(self.DEBUG, "_async_transition_end_callback")
        await self.async_stop_cover()


class MLRollerShutterConfigNumber(me.MEDictChannelMixin, MLConfigNumber):
    """
    Helper entity to configure MRS open/close duration
    """

    namespace = mc.NS_APPLIANCE_ROLLERSHUTTER_CONFIG
    key_namespace = mc.KEY_CONFIG

    device_scale = 1000

    # HA core entity attributes:
    # these are ok for open/close durations
    # customize those when needed...
    native_max_value = 60
    native_min_value = 1
    native_step = 1

    __slots__ = ("_cover",)

    def __init__(self, cover: "MLRollerShutter", key: str):
        self._cover = cover
        self.key_value = key
        self.name = key
        super().__init__(
            cover.manager,
            cover.channel,
            f"config_{key}",
            MLConfigNumber.DEVICE_CLASS_DURATION,
        )
