from typing import TYPE_CHECKING, override

from homeassistant.exceptions import InvalidStateError

from ..const import CONF_PROTOCOL_HTTP, PARAM_ROLLERSHUTTER_TRANSITION_POLL_TIMEOUT
from ..cover import MLCover, cover
from ..merossclient.protocol import const as mc, namespaces as mn
from ..number import MLConfigNumber
from ..switch import MLDeviceSwitch

if TYPE_CHECKING:
    from typing import ClassVar, NotRequired

    from ..helpers.device import Device
    from ..merossclient.protocol.types import rollershutter as mt_rs


class MLRollerShutter(MLCover):
    """
    Meross Roller Shutter cover device implementation.
    """

    if TYPE_CHECKING:
        current_cover_position: int | None
        supported_features: MLCover.EntityFeature

    ns = mn.Appliance_RollerShutter_Position
    NS_CHANNELS = (0,)
    key_value = mc.KEY_POSITION

    ATTR_POSITION_NATIVE = "position_native"

    # HA core entity attributes:
    _attr_device_class = MLCover.DeviceClass.SHUTTER
    assumed_state = True

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

    def __init__(self, manager: "Device", channel: int, /):
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
            fw_version = descriptor.firmware_version
            if fw_version >= (6, 6, 6):
                self._position_native_isgood = True
                self.supported_features |= MLCover.EntityFeature.SET_POSITION
            else:
                self._position_native_isgood = False
                if fw_version <= (2, 1, 4):
                    # trying to detect if ns_multiple is offending
                    # 2.1.4 devices (#419)
                    manager.enable_multiple(False)

        except Exception:
            self._position_native_isgood = False
        MLCover.__init__(self, manager, channel)
        manager.register_parser_entity(self)
        manager.register_parser(self, mn.Appliance_RollerShutter_Config)
        manager.register_parser(self, mn.Appliance_RollerShutter_State)
        if mn.Appliance_Control_ToggleX in descriptor.ability:
            # This is still to be understood. This call will do nothing
            # since the digest seen so far carries an empty list of channels
            # even though the abilities show ToggleX support.
            manager.register_togglex_channel(self, False)
        if mn.Appliance_RollerShutter_Adjust in descriptor.ability:
            # unknown use: actually the polling period is set on a very high timeout
            manager.register_parser_entity(
                MLRollerShutterAdjustSwitch(self.manager, channel)
            )
        self.number_signalOpen = MLRollerShutterConfigNumber(self, mc.KEY_SIGNALOPEN)
        self.number_signalClose = MLRollerShutterConfigNumber(self, mc.KEY_SIGNALCLOSE)

    async def async_added_to_hass(self):
        await MLCover.async_added_to_hass(self)
        """
        we're trying to recover the 'timed' position from previous state
        if it happens it wasn't updated too far in time
        """
        with self.exception_warning("restoring previous state"):
            if last_state := await self.get_last_state_available():
                if not self._position_native_isgood:
                    # at this stage, the euristic on fw version doesn't say anything
                    try:
                        self.extra_state_attributes[
                            MLRollerShutter.ATTR_POSITION_NATIVE
                        ] = last_state.attributes[MLRollerShutter.ATTR_POSITION_NATIVE]
                        # Having ATTR_POSITION_NATIVE in attributes
                        # means we didn't trust native position so far
                        self.current_cover_position = last_state.attributes[
                            cover.ATTR_CURRENT_POSITION
                        ]
                        # If this didn't fail, we can now assume device native_position is reliable
                        self.supported_features |= MLCover.EntityFeature.SET_POSITION
                    except KeyError:
                        pass

    @override
    async def async_open_cover(self, **kwargs):
        await self.async_request_position(mc.ROLLERSHUTTER_POSITION_OPENED)

    @override
    async def async_close_cover(self, **kwargs):
        await self.async_request_position(mc.ROLLERSHUTTER_POSITION_CLOSED)

    @override
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
                self._transition_end_unsub = self.manager.schedule_async_callback(
                    timeout, self._async_transition_end_callback
                )

    @override
    async def async_stop_cover(self, **kwargs):
        await self.async_request_position(mc.ROLLERSHUTTER_POSITION_STOP)

    async def async_request_position(self, position: int):
        self._transition_cancel()
        # TODO: this is a case where we don't want the async_set to callback
        # the _parse_position with the request payload since we're the ones requesting it.
        # That's why we're not forwarding self as parser to the call.
        # As a note, consider the device replies an empty dict on SETACK
        if await self.handler_ns.async_set(
            {mc.KEY_CHANNEL: self.channel, self.key_value: position}
        ):
            # re-ensure current transitions are clean after await
            self._transition_cancel()
            await self._async_transition_callback()
            return True

    def set_unavailable(self):
        self._mrs_state = None
        MLCover.set_unavailable(self)

    def _parse_config(self, payload: dict):
        # payload = {"channel": 0, "signalOpen": 50000, "signalClose": 50000}
        try:
            self.number_signalOpen.update_device_value(payload[mc.KEY_SIGNALOPEN])
        except KeyError:
            pass
        try:
            self.number_signalClose.update_device_value(payload[mc.KEY_SIGNALCLOSE])
        except KeyError:
            pass

    def _parse_position(self, payload: "mt_rs.Position_C"):
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
            self.extra_state_attributes.pop(MLRollerShutter.ATTR_POSITION_NATIVE, None)
            self.supported_features |= MLCover.EntityFeature.SET_POSITION
            self.current_cover_position = position
        else:
            self._position_native = position
            self.is_closed = position == mc.ROLLERSHUTTER_POSITION_CLOSED
            self.extra_state_attributes[MLRollerShutter.ATTR_POSITION_NATIVE] = position
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

    def _parse_state(self, payload: "mt_rs.Status_C"):
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
                    self._transition_unsub = self.manager.schedule_async_callback(
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
        manager = self.manager
        self._transition_unsub = manager.schedule_async_callback(
            PARAM_ROLLERSHUTTER_TRANSITION_POLL_TIMEOUT,
            self._async_transition_callback,
        )
        if (
            manager.curr_protocol is CONF_PROTOCOL_HTTP and not manager._mqtt_active
        ) or (self._mrs_state == mc.ROLLERSHUTTER_STATE_IDLE):
            if manager.multiple_max >= 2:
                # TODO: migrate call
                await manager.async_multiple_requests_ack(
                    (
                        mn.Appliance_RollerShutter_State.request_default,
                        mn.Appliance_RollerShutter_Position.request_default,
                    )
                )
            else:
                await manager.ns_handlers[mn.Appliance_RollerShutter_State].async_get(
                    self.channel
                )
                if self._position_native_isgood:
                    await self.handler_ns.async_get(self.channel)

    async def _async_transition_end_callback(self):
        self._transition_end_unsub = None
        self.log(self.DEBUG, "_async_transition_end_callback")
        await self.async_stop_cover()


class MLRollerShutterAdjustSwitch(MLDeviceSwitch):
    """
    Appliance.RollerShutter.Adjust is a bit weird. It seems to report
    some binary status about shutter tuning operations.
    It doesn't support method GET (while PSH query works..).
    Status reported in push contains a "status" key (unknown meaning)
    It also suppports method SET with payload like { "value": 1 or 2 }
    which seems to start some kind of adjustment operation.
    """

    ns = mn.Appliance_RollerShutter_Adjust
    key_value = mc.KEY_VALUE  # used to configure method SET
    native_on = 1
    native_off = 2

    def __init__(self, manager: "Device", channel: int):
        MLDeviceSwitch.__init__(
            self,
            manager,
            channel,
            f"{self.ns.slug}__{self.key_value}",
            name="Auto Calibration",
        )

    def _parse_adjust(self, payload: "mt_rs.AdjustResponse_C"):
        # payload = {"channel": 0, "status": 0}
        try:
            # As noted in the docstring, meaning of status is unknown
            # also, this method is called both when parsing a push update
            # and when parsing a response to our SET command.
            # The 2 payloads are thus different so we're just handling
            # these scenarios with a try/except conditional
            self.update_device_value(payload[self.key_value])
        except KeyError:
            self.update_native_value(payload[mc.KEY_STATUS] != 0)


class MLRollerShutterConfigNumber(MLConfigNumber):
    """
    Helper entity to configure MRS open/close duration
    """

    ns = mn.Appliance_RollerShutter_Config

    _attr_device_scale = 1000

    # HA core entity attributes:
    _attr_device_class = MLConfigNumber.DEVICE_CLASS_DURATION
    # these are ok for open/close durations
    # customize those when needed...
    native_max_value = 60
    native_min_value = 1
    native_step = 1

    def __init__(self, cover: "MLRollerShutter", key: str):
        self.key_value = key
        MLConfigNumber.__init__(
            self,
            cover.manager,
            cover.channel,
            f"config_{key}",
            name=key,
        )
