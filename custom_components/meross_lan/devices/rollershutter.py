from typing import TYPE_CHECKING, override

from homeassistant.exceptions import InvalidStateError

from ..cover import Cover, cover
from ..helpers.namespaces import POLLING_STRATEGY_CONF, NamespaceHandler, mc, mlc, mn
from ..merossclient.client import Transport
from ..number import ParserNumber
from ..switch import SwitchParser

if TYPE_CHECKING:
    from typing import ClassVar, NotRequired

    from ..helpers.device import Device
    from ..merossclient.protocol.types import rollershutter as mt_rs


class RollerShutter(Cover):
    """
    Meross Roller Shutter cover device implementation.
    """

    if TYPE_CHECKING:
        current_cover_position: int | None
        supported_features: Cover.EntityFeature

    # TODO: switchover main ns to State so we could use device_value for _mrs_state
    ns = mn.Appliance_RollerShutter_Position
    key_value = mc.KEY_POSITION

    ATTR_POSITION_NATIVE = "position_native"

    # HA core entity attributes:
    _attr_device_class = Cover.DeviceClass.SHUTTER
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

    def __init__(self, channel: int, device: "Device", /):
        self.current_cover_position = None
        self.supported_features = (
            Cover.EntityFeature.OPEN
            | Cover.EntityFeature.CLOSE
            | Cover.EntityFeature.STOP
        )
        self.extra_state_attributes = {}
        self._mrs_state = None
        self._position_native = None  # as reported by the device
        self._position_start = 0  # set when when we're controlling a timed position
        self._position_starttime = 0  # epoch of transition start
        descriptor = device.descriptor
        # flag indicating the device position is reliable (#227)
        # this will anyway be set in case we 'decode' a meaningful device position
        try:
            fw_version = descriptor.firmware_version
            if fw_version >= (6, 6, 6):
                self._position_native_isgood = True
                self.supported_features |= Cover.EntityFeature.SET_POSITION
            else:
                self._position_native_isgood = False
                if fw_version <= (2, 1, 4):
                    # trying to detect if ns_multiple is offending
                    # 2.1.4 devices (#419)
                    device.enable_multiple(False)

        except Exception:
            self._position_native_isgood = False
        Cover.__init__(self, channel, device)
        device.register_parser_ex(
            self,
            self.ns,
            mn.Appliance_RollerShutter_Config,
            mn.Appliance_RollerShutter_State,
        )
        if mn.Appliance_Control_ToggleX in descriptor.ability:
            # This is still to be understood. This call will do nothing
            # since the digest seen so far carries an empty list of channels
            # even though the abilities show ToggleX support.
            device.register_togglex_channel(self, False)
        if mn.Appliance_RollerShutter_Adjust in descriptor.ability:
            # unknown use: actually the polling period is set on a very high timeout
            device.register_parser_entity(RollerShutterAdjustSwitch(channel, device))
        self.number_signalOpen = RollerShutterConfigNumber(self, mc.KEY_SIGNALOPEN)
        self.number_signalClose = RollerShutterConfigNumber(self, mc.KEY_SIGNALCLOSE)

    def set_unavailable(self):
        self._mrs_state = None
        Cover.set_unavailable(self)

    async def async_added_to_hass(self):
        await Cover.async_added_to_hass(self)
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
                            RollerShutter.ATTR_POSITION_NATIVE
                        ] = last_state.attributes[RollerShutter.ATTR_POSITION_NATIVE]
                        # Having ATTR_POSITION_NATIVE in attributes
                        # means we didn't trust native position so far
                        self.current_cover_position = last_state.attributes[
                            cover.ATTR_CURRENT_POSITION
                        ]
                        # If this didn't fail, we can now assume device native_position is reliable
                        self.supported_features |= Cover.EntityFeature.SET_POSITION
                    except KeyError:
                        pass

    # interface: cover.CoverEntity
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
            await self.async_request_position(position)
            self.schedule_async_callback(timeout, self._async_transition_end_callback)

    async def async_stop_cover(self, **kwargs):
        await self.async_request_position(mc.ROLLERSHUTTER_POSITION_STOP)

    # interface: self
    async def async_request_position(self, position: int):
        self._transition_cancel()
        await self.async_request_payload({self.key_value: position})
        self._transition_cancel()
        await self._async_read_state()

    async def _async_read_state(self, /):
        if self.parent.multiple_max >= 2:
            await self.parent.async_handle_request_multiple(
                (
                    mn.Appliance_RollerShutter_State.request_default,
                    self.ns.request_default,
                )
            )
        else:
            await self.parent.ns_handlers[mn.Appliance_RollerShutter_State].async_get(
                self.channel
            )
            if self._position_native_isgood:
                await self.handler_ns.async_get(self.channel)

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
        bypassing all of the 'time based' emulation.
        TODO: we might prefer using schedule_flush_state here since we might
        be in a 'transaction' where we receive multiple async updates
        for state and position and we want to avoid multiple flushes.
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
            self.extra_state_attributes.pop(RollerShutter.ATTR_POSITION_NATIVE, None)
            self.supported_features |= Cover.EntityFeature.SET_POSITION
            self.current_cover_position = position
        else:
            self._position_native = position
            self.is_closed = position == mc.ROLLERSHUTTER_POSITION_CLOSED
            self.extra_state_attributes[RollerShutter.ATTR_POSITION_NATIVE] = position
            if self.current_cover_position is None:
                # only happening when we didn't restore state on devices
                # which are likely not supporting native positioning
                # at this stage we'll enable set_position anyway and
                # trusting the device position as the better guess
                # If current_cover_position is already set, it represents the
                # emulated state and so we don't touch it
                self.supported_features |= Cover.EntityFeature.SET_POSITION
                self.current_cover_position = position

        self.flush_state()

    def _parse_state(self, payload: "mt_rs.Status_C"):
        state = payload[mc.KEY_STATE]
        if not self._position_native_isgood:
            epoch = self.parent.last_rx_epoch
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
                        self.supported_features |= Cover.EntityFeature.SET_POSITION
                    self._position_start = self.current_cover_position
                    self._position_starttime = epoch
            elif state == mc.ROLLERSHUTTER_STATE_CLOSING:
                if not self.is_closing:
                    if self.current_cover_position is None:
                        self.current_cover_position = mc.ROLLERSHUTTER_POSITION_OPENED
                        self.supported_features |= Cover.EntityFeature.SET_POSITION
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
            self.flush_state()

        if state == mc.ROLLERSHUTTER_STATE_IDLE:
            self._transition_cancel()
        else:
            self.schedule_callback(
                mlc.PARAM_ROLLERSHUTTER_TRANSITION_POLL_TIMEOUT,
                self._transition_callback,
            )

    @override
    def _transition_callback(self):
        if (
            self.parent.transport is Transport.HTTP and not self.parent.mqtt_active
        ) or (self._mrs_state == mc.ROLLERSHUTTER_STATE_IDLE):
            self.create_task(
                self._async_read_state(), "._transition_callback", eager_start=True
            )

    @override
    async def _async_transition_end_callback(self, /):
        await self.async_stop_cover()


class RollerShutterAdjustSwitch(SwitchParser):
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

    ENTITY_KEY = f"{ns.slug}__{key_value}"

    _attr_name = "Auto Calibration"

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
            self.update_boolean_value(payload[mc.KEY_STATUS] != 0)


class RollerShutterConfigNumber(ParserNumber):
    """
    Helper entity to configure MRS open/close duration
    """

    ns = mn.Appliance_RollerShutter_Config

    _attr_device_scale = 1000

    # HA core entity attributes:
    _attr_device_class = ParserNumber.DEVICE_CLASS_DURATION
    # these are ok for open/close durations
    # customize those when needed...
    native_max_value = 60
    native_min_value = 1
    native_step = 1

    def __init__(self, cover: "RollerShutter", key: str):
        self.key_value = key
        ParserNumber.__init__(
            self, cover.channel, cover.parent, entity_key=f"config_{key}", name=key
        )


POLLING_STRATEGY_CONF.update(
    {
        mn.Appliance_RollerShutter_Adjust: (
            mlc.PARAM_CONFIG_UPDATE_PERIOD,
            mlc.PARAM_CLOUD_UPDATE_PERIOD,
            NamespaceHandler.async_poll_smart,
        ),
        mn.Appliance_RollerShutter_Config: (
            mlc.PARAM_CONFIG_UPDATE_PERIOD,
            mlc.PARAM_CLOUD_UPDATE_PERIOD,
            NamespaceHandler.async_poll_smart,
        ),
        mn.Appliance_RollerShutter_Position: (
            0,
            0,
            NamespaceHandler.async_poll_default,
        ),
        mn.Appliance_RollerShutter_State: (
            0,
            0,
            NamespaceHandler.async_poll_default,
        ),
    }
)
