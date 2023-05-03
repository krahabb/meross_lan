from __future__ import annotations

from logging import DEBUG
from time import time
import typing

from homeassistant.components import cover
from homeassistant.components.cover import (
    ATTR_CURRENT_POSITION,
    ATTR_POSITION,
    STATE_CLOSED,
    STATE_CLOSING,
    STATE_OPEN,
    STATE_OPENING,
    CoverDeviceClass,
    CoverEntityFeature,
)
from homeassistant.const import TIME_SECONDS
from homeassistant.core import callback
from homeassistant.util.dt import now

from . import meross_entity as me
from .binary_sensor import MLBinarySensor
from .const import (
    CONF_PROTOCOL_HTTP,
    PARAM_GARAGEDOOR_TRANSITION_MAXDURATION,
    PARAM_GARAGEDOOR_TRANSITION_MINDURATION,
)
from .helpers import (
    clamp,
    get_entity_last_state,
    schedule_async_callback,
    schedule_callback,
    versiontuple,
)
from .merossclient import const as mc, get_default_arguments
from .number import MLConfigNumber
from .switch import MLSwitch

if typing.TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

    from .meross_device import MerossDevice, MerossDeviceDescriptor

SUPPORT_OPEN = CoverEntityFeature.OPEN
SUPPORT_CLOSE = CoverEntityFeature.CLOSE
SUPPORT_SET_POSITION = CoverEntityFeature.SET_POSITION
SUPPORT_STOP = CoverEntityFeature.STOP

STATE_MAP = {0: STATE_CLOSED, 1: STATE_OPEN}

POSITION_FULLY_CLOSED = 0
POSITION_FULLY_OPENED = 100

# garagedoor extra attributes
NOTIFICATION_ID_TIMEOUT = "garagedoor_timeout"
EXTRA_ATTR_TRANSITION_DURATION = "transition_duration"
EXTRA_ATTR_TRANSITION_TIMEOUT = (
    "transition_timeout"  # the time at which the transition timeout occurred
)
EXTRA_ATTR_TRANSITION_TARGET = (
    "transition_target"  # the target state which was not reached
)

# rollershutter extra attributes
EXTRA_ATTR_DURATION_OPEN = "duration_open"
EXTRA_ATTR_DURATION_CLOSE = "duration_close"
EXTRA_ATTR_POSITION_NATIVE = "position_native"


async def async_setup_entry(
    hass: HomeAssistant, config_entry: ConfigEntry, async_add_devices
):
    me.platform_setup_entry(hass, config_entry, async_add_devices, cover.DOMAIN)


class MLGarageTimeoutBinarySensor(MLBinarySensor):
    def __init__(self, cover: MLGarage):
        self._attr_extra_state_attributes = {}
        super().__init__(
            cover.device, cover.channel, "problem", self.DeviceClass.PROBLEM
        )
        self._attr_state = me.STATE_OFF

    @property
    def available(self):
        return True

    @property
    def entity_category(self):
        return me.EntityCategory.DIAGNOSTIC

    def set_unavailable(self):
        pass

    def update_ok(self):
        self._attr_extra_state_attributes.pop(EXTRA_ATTR_TRANSITION_TIMEOUT, None)
        self._attr_extra_state_attributes.pop(EXTRA_ATTR_TRANSITION_TARGET, None)
        self.update_onoff(0)

    def update_timeout(self, target_state):
        self._attr_extra_state_attributes[EXTRA_ATTR_TRANSITION_TARGET] = target_state
        self._attr_extra_state_attributes[
            EXTRA_ATTR_TRANSITION_TIMEOUT
        ] = now().isoformat()
        self.update_onoff(1)


class MLGarageConfigSwitch(MLSwitch):
    device: GarageMixin

    def __init__(self, device, key: str):
        self.key_onoff = key
        self._attr_name = key
        super().__init__(device, None, f"config_{key}", None, None, None)

    @property
    def entity_category(self):
        return me.EntityCategory.CONFIG

    async def async_request_onoff(self, onoff: int):
        config = dict(self.device.garageDoor_config)
        config[self.key_onoff] = onoff

        def _ack_callback(acknowledge: bool, header: dict, payload: dict):
            if acknowledge:
                self.update_onoff(onoff)
                self.device.garageDoor_config[self.key_onoff] = config[self.key_onoff]

        await self.device.async_request(
            mc.NS_APPLIANCE_GARAGEDOOR_CONFIG,
            mc.METHOD_SET,
            {mc.KEY_CONFIG: config},
            _ack_callback,
        )


class MLGarageConfigNumber(MLConfigNumber):
    """
    Helper entity to configure MSG open/close duration
    """

    device: GarageMixin

    def __init__(self, device, channel, key: str):
        self.key_value = key
        self._attr_name = key
        # these are ok for 2 of the 3 config numbers
        # customize those when instantiating
        self._attr_native_max_value = 60
        self._attr_native_min_value = 1
        self._attr_native_step = 1
        super().__init__(device, channel, f"config_{key}")

    @property
    def native_unit_of_measurement(self):
        return TIME_SECONDS

    async def async_set_native_value(self, value: float):
        config: dict[str, object] = dict(self.device.garageDoor_config)
        config[self.key_value] = int(value * self.ml_multiplier)

        def _ack_callback(acknowledge: bool, header: dict, payload: dict):
            if acknowledge:
                self.update_native_value(config[self.key_value])
                self.device.garageDoor_config[self.key_value] = config[self.key_value]

        await self.device.async_request(
            mc.NS_APPLIANCE_GARAGEDOOR_CONFIG,
            mc.METHOD_SET,
            {mc.KEY_CONFIG: config},
            _ack_callback,
        )

    @property
    def ml_multiplier(self):
        return 1000


class MLGarageOpenCloseDurationNumber(MLGarageConfigNumber):
    """
    Helper entity to configure MSG open/close duration
    This entity is bound to the garage channel (i.e. we have
    a pair of open/close for each garage entity) and
    is not linked to 'Appliance.GarageDoor.Config'.
    Newer msg devices appear to have a door/open configuration
    for each channel but we're still lacking the knowledge
    in order to configure them. These MLGarageOpenCloseDurationNumber
    entities will therefore be just 'emulated' in meross_lan and
    the state is managed inside this component
    """

    def __init__(self, cover: MLGarage, key: str):
        super().__init__(
            cover.device,
            cover.channel,
            key,
        )
        self._attr_state = (
            PARAM_GARAGEDOOR_TRANSITION_MAXDURATION
            + PARAM_GARAGEDOOR_TRANSITION_MINDURATION
        ) / 2

    @property
    def available(self):
        return True

    def set_unavailable(self):
        pass

    async def async_added_to_hass(self):
        await super().async_added_to_hass()
        with self.exception_warning("restoring previous state"):
            if last_state := await get_entity_last_state(self.hass, self.entity_id):
                self._attr_state = float(last_state.state)  # type: ignore

    async def async_set_native_value(self, value: float):
        self.update_native_value(value)

    @property
    def ml_multiplier(self):
        return 1


class MLGarage(me.MerossEntity, cover.CoverEntity):
    PLATFORM = cover.DOMAIN

    device: GarageMixin

    __slots__ = (
        "_transition_duration",
        "_transition_start",
        "_transition_unsub",
        "_transition_end_unsub",
        "_open",
        "_open_request",
        "binary_sensor_timeout",
        "number_doorOpenDuration",
        "number_doorCloseDuration",
    )

    def __init__(self, device: "MerossDevice", channel: object):
        super().__init__(device, channel, None, CoverDeviceClass.GARAGE)
        self._transition_duration = (
            PARAM_GARAGEDOOR_TRANSITION_MAXDURATION
            + PARAM_GARAGEDOOR_TRANSITION_MINDURATION
        ) / 2
        self._transition_start = None
        self._transition_unsub = None
        self._transition_end_unsub = None
        # this is the last known (or actual) physical state from device state
        self._open = None
        # cache issued request since device reply doesnt report it
        self._open_request = None
        self._attr_extra_state_attributes = {
            EXTRA_ATTR_TRANSITION_DURATION: self._transition_duration
        }
        self.binary_sensor_timeout = MLGarageTimeoutBinarySensor(self)
        self.number_doorOpenDuration: MLGarageConfigNumber | None = None
        self.number_doorCloseDuration: MLGarageConfigNumber | None = None

    @property
    def supported_features(self):
        return SUPPORT_OPEN | SUPPORT_CLOSE

    @property
    def is_opening(self):
        return self._attr_state == STATE_OPENING

    @property
    def is_closing(self):
        return self._attr_state == STATE_CLOSING

    @property
    def is_closed(self):
        return self._attr_state == STATE_CLOSED

    async def async_added_to_hass(self):
        await super().async_added_to_hass()
        """
        we're trying to recover the '_transition_duration' from previous state
        """
        with self.exception_warning("restoring previous state"):
            if last_state := await get_entity_last_state(self.hass, self.entity_id):
                _attr = last_state.attributes  # type: ignore
                if EXTRA_ATTR_TRANSITION_DURATION in _attr:
                    # restore anyway besides PARAM_RESTORESTATE_TIMEOUT
                    # since this is no harm and unlikely to change
                    # better than defaulting to a pseudo-random value
                    self._transition_duration = _attr[EXTRA_ATTR_TRANSITION_DURATION]
                    self._attr_extra_state_attributes[
                        EXTRA_ATTR_TRANSITION_DURATION
                    ] = self._transition_duration

    async def async_open_cover(self, **kwargs):
        await self.async_request_position(1)

    async def async_close_cover(self, **kwargs):
        await self.async_request_position(0)

    async def async_request_position(self, open_request: int):
        def _ack_callback(acknowledge: bool, header: dict, payload: dict):
            """
            example payload in SETACK:
            {"state": {"channel": 0, "open": 0, "lmTime": 0, "execute": 1}}
            "open" reports the current state and not the command
            "execute" represents command ack (I guess: never seen this == 0)
            Beware: if the garage is 'closed' and we send a 'close' "execute" will
            be replied as "1" and the garage will stay closed
            """
            if acknowledge:
                p_state = payload.get(mc.KEY_STATE, {})
                if p_state.get(mc.KEY_EXECUTE) and open_request != p_state.get(
                    mc.KEY_OPEN
                ):
                    self._transition_cancel()
                    self._open_request = open_request
                    self._transition_start = time()
                    self.update_state(STATE_OPENING if open_request else STATE_CLOSING)
                    if open_request:
                        try:
                            timeout = self.number_doorOpenDuration.native_value  # type: ignore
                        except AttributeError:
                            # should really not happen if the GarageMixin has global conf key
                            # for closeDuration. Else, this fw supports 'per channel' conf (#82)
                            self.number_doorOpenDuration = (
                                MLGarageOpenCloseDurationNumber(
                                    self, mc.KEY_DOOROPENDURATION
                                )
                            )
                            timeout = self.number_doorOpenDuration.native_value
                    else:
                        try:
                            timeout = self.number_doorCloseDuration.native_value  # type: ignore
                        except AttributeError:
                            # should really not happen if the GarageMixin has global conf key
                            # for closeDuration. Else, this fw supports 'per channel' conf (#82)
                            self.number_doorCloseDuration = (
                                MLGarageOpenCloseDurationNumber(
                                    self, mc.KEY_DOORCLOSEDURATION
                                )
                            )
                            timeout = self.number_doorCloseDuration.native_value

                    # check the timeout 1 sec after expected to account
                    # for delays in communication
                    self._transition_end_unsub = schedule_callback(
                        self.hass,
                        timeout + 1,  # type: ignore
                        self._transition_end_callback,
                    )

        await self.device.async_request(
            mc.NS_APPLIANCE_GARAGEDOOR_STATE,
            mc.METHOD_SET,
            {mc.KEY_STATE: {mc.KEY_CHANNEL: self.channel, mc.KEY_OPEN: open_request}},
            _ack_callback,
        )

    async def async_will_remove_from_hass(self):
        self._transition_cancel()
        await super().async_will_remove_from_hass()

    def set_unavailable(self):
        self._open = None
        self._transition_cancel()
        super().set_unavailable()

    def _parse_state(self, payload: dict):
        # {"channel": 0, "open": 1, "lmTime": 0}
        self._open = _open = payload[mc.KEY_OPEN]
        epoch = self.device.lastresponse
        if self._transition_start is None:
            # our state machine is idle and we could be polling a
            # state change triggered by any external means (app, remote)
            self.update_state(STATE_MAP.get(_open))
        else:
            # state will be updated on _transition_end_callback
            # but we monitor the contact switch in order to
            # update our estimates for transition duration
            if self._open_request != _open:
                # keep monitoring the transition in less than 1 sec
                if self._transition_unsub is None:
                    self._transition_unsub = schedule_callback(
                        self.hass, 0.9, self._transition_callback
                    )
            else:
                # we can monitor the (sampled) exact time when the garage closes to
                # estimate the transition_duration and dynamically update it since
                # during the transition the state will be closed only at the end
                # while during opening the garagedoor contact will open right at the beginning
                # and so will be unuseful
                # Also to note: if we're on HTTP this sampled time could happen anyway after the 'real'
                # state switched to 'closed' so we're likely going to measure in exceed of real transition duration
                if not _open:
                    transition_duration = epoch - self._transition_start
                    # autoregression filtering applying 20% of last updated sample
                    self._update_transition_duration(
                        int((4 * self._transition_duration + transition_duration) / 5)
                    )
                    self._transition_cancel()
                    self.update_state(STATE_CLOSED)

    def _parse_config(self, payload):
        pass

    def update_onoff(self, onoff):
        """
        MSG100 exposes a 'togglex' interface so my code interprets that as a switch state
        Here we'll intercept that behaviour and right now the guess is:
        The toggle state represents the contact of the garagedoor which is likely a short
        pulse so we'll use it to guess state transitions in our cover (disabled this until further knowledge)

        if onoff:
            if self._attr_state == STATE_CLOSED:
                self._start_transition(STATE_OPEN)
            elif self._attr_state == STATE_OPEN:
                self._start_transition(STATE_CLOSED)
        #else: RIP!
        """

    def _transition_cancel(self):
        if self._transition_unsub is not None:
            self._transition_unsub.cancel()
            self._transition_unsub = None
        if self._transition_end_unsub is not None:
            self._transition_end_unsub.cancel()
            self._transition_end_unsub = None
        self._open_request = None
        self._transition_start = None

    @callback
    def _transition_callback(self):
        self._transition_unsub = None
        self.device.request(*get_default_arguments(mc.NS_APPLIANCE_GARAGEDOOR_STATE))

    @callback
    def _transition_end_callback(self):
        """
        checks the transition did finish as per the timeout(s)
        """
        self._transition_end_unsub = None
        if not self._open_request:
            # when closing we expect this callback not to be called since
            # the transition should be terminated by '_set_open' provided it gets
            # called on time (on polling this is not guaranteed).
            # If we're here, we still havent received a proper 'physical close'
            # because our configured closeduration is too short
            # or the garage didnt close at all
            transition_duration = time() - self._transition_start  # type: ignore
            if self._transition_duration < transition_duration:
                self._update_transition_duration(self._transition_duration + 1)

        if self._open_request == self._open:
            # transition correctly ended: set the state according to our last known hardware status
            self.binary_sensor_timeout.update_ok()
            self.update_state(STATE_MAP.get(self._open_request))  # type: ignore
        else:
            # let the current opening/closing state be updated only on subsequent poll
            self.binary_sensor_timeout.update_timeout(STATE_MAP.get(self._open_request))  # type: ignore

        self._open_request = None
        self._transition_start = None

    def _update_transition_duration(self, transition_duration):
        self._transition_duration = clamp(
            transition_duration,
            PARAM_GARAGEDOOR_TRANSITION_MINDURATION,
            PARAM_GARAGEDOOR_TRANSITION_MAXDURATION,
        )
        self._attr_extra_state_attributes[
            EXTRA_ATTR_TRANSITION_DURATION
        ] = self._transition_duration


class GarageMixin(
    MerossDevice if typing.TYPE_CHECKING else object
):  # pylint: disable=used-before-assignment
    number_signalDuration: MLGarageConfigNumber
    switch_buzzerEnable: MLGarageConfigSwitch
    number_doorOpenDuration: MLGarageConfigNumber | None = None
    number_doorCloseDuration: MLGarageConfigNumber | None = None

    def __init__(self, descriptor: MerossDeviceDescriptor, entry):
        self.garageDoor_config = {}
        self._polling_payload = []
        super().__init__(descriptor, entry)
        self.number_signalDuration = MLGarageConfigNumber(
            self, None, mc.KEY_SIGNALDURATION
        )
        self.number_signalDuration._attr_native_step = 0.1
        self.number_signalDuration._attr_native_min_value = 0.1
        self.switch_buzzerEnable = MLGarageConfigSwitch(self, mc.KEY_BUZZERENABLE)
        if mc.NS_APPLIANCE_GARAGEDOOR_CONFIG in descriptor.ability:
            self.polling_dictionary[mc.NS_APPLIANCE_GARAGEDOOR_CONFIG] = mc.PAYLOAD_GET[
                mc.NS_APPLIANCE_GARAGEDOOR_CONFIG
            ]
        if mc.NS_APPLIANCE_GARAGEDOOR_MULTIPLECONFIG in descriptor.ability:
            self.polling_dictionary[mc.NS_APPLIANCE_GARAGEDOOR_MULTIPLECONFIG] = {
                mc.KEY_CONFIG: self._polling_payload
            }

    async def async_shutdown(self):
        await super().async_shutdown()
        self.number_signalDuration = None  # type: ignore
        self.switch_buzzerEnable = None  # type: ignore
        self.number_doorOpenDuration = None
        self.number_doorCloseDuration = None

    def _init_garageDoor(self, payload: dict):
        channel = payload[mc.KEY_CHANNEL]
        MLGarage(self, channel)
        self._polling_payload.append({mc.KEY_CHANNEL: channel})

    def _handle_Appliance_GarageDoor_State(self, header: dict, payload: dict):
        self._parse__generic(mc.KEY_STATE, payload.get(mc.KEY_STATE))

    def _handle_Appliance_GarageDoor_Config(self, header: dict, payload: dict):
        # {"config": {"signalDuration": 1000, "buzzerEnable": 0, "doorOpenDuration": 30000, "doorCloseDuration": 30000}}
        # no channel here ?!..need to parse the manual way
        if isinstance(payload := payload.get(mc.KEY_CONFIG), dict):  # type: ignore
            self.garageDoor_config.update(payload)

            if mc.KEY_SIGNALDURATION in payload:
                self.number_signalDuration.update_native_value(
                    payload[mc.KEY_SIGNALDURATION]
                )

            if mc.KEY_BUZZERENABLE in payload:
                self.switch_buzzerEnable.update_onoff(payload[mc.KEY_BUZZERENABLE])

            if mc.KEY_DOOROPENDURATION in payload:
                # this config key has been removed in recent firmwares
                # now we have door open/close duration set per channel (#82)
                # but legacy ones still manage this
                try:
                    self.number_doorOpenDuration.update_native_value(  # type: ignore
                        payload[mc.KEY_DOOROPENDURATION]
                    )
                except AttributeError:
                    _number_doorOpenDuration = MLGarageConfigNumber(
                        self,
                        None,
                        mc.KEY_DOOROPENDURATION,
                    )
                    _number_doorOpenDuration.update_native_value(
                        payload[mc.KEY_DOOROPENDURATION]
                    )
                    self.number_doorOpenDuration = _number_doorOpenDuration
                    for entity in self.entities.values():
                        if isinstance(entity, MLGarage):
                            entity.number_doorOpenDuration = _number_doorOpenDuration

            if mc.KEY_DOORCLOSEDURATION in payload:
                # this config key has been removed in recent firmwares
                # now we have door open/close duration set per channel (#82)
                try:
                    self.number_doorCloseDuration.update_native_value(  # type: ignore
                        payload[mc.KEY_DOORCLOSEDURATION]
                    )
                except AttributeError:
                    _number_doorCloseDuration = MLGarageConfigNumber(
                        self,
                        None,
                        mc.KEY_DOORCLOSEDURATION,
                    )
                    _number_doorCloseDuration.update_native_value(
                        payload[mc.KEY_DOORCLOSEDURATION]
                    )
                    self.number_doorCloseDuration = _number_doorCloseDuration
                    for entity in self.entities.values():
                        if isinstance(entity, MLGarage):
                            entity.number_doorCloseDuration = _number_doorCloseDuration

    def _handle_Appliance_GarageDoor_MultipleConfig(self, header: dict, payload: dict):
        self._parse__generic(mc.KEY_CONFIG, payload.get(mc.KEY_CONFIG))

    def _parse_garageDoor(self, payload):
        self._parse__generic(mc.KEY_STATE, payload)


class MLRollerShutter(me.MerossEntity, cover.CoverEntity):
    """
    MRS100 SHUTTER ENTITY
    """

    PLATFORM = cover.DOMAIN

    device: RollerShutterMixin

    __slots__ = (
        "number_signalOpen",
        "number_signalClose",
        "_signalOpen",
        "_signalClose",
        "_position_native",
        "_position_start",
        "_position_starttime",
        "_position_endtime",
        "_transition_unsub",
        "_transition_end_unsub",
    )

    def __init__(self, device: "MerossDevice", channel: object):
        super().__init__(device, channel, None, CoverDeviceClass.SHUTTER)
        self.number_signalOpen = MLRollerShutterConfigNumber(self, mc.KEY_SIGNALOPEN)
        self.number_signalClose = MLRollerShutterConfigNumber(self, mc.KEY_SIGNALCLOSE)
        self._signalOpen: int = 30000  # msec to fully open (config'd on device)
        self._signalClose: int = 30000  # msec to fully close (config'd on device)
        self._position_native = None  # as reported by the device
        self._position_start = None  # set when when we're controlling a timed position
        self._position_starttime = None  # epoch of transition start
        self._position_endtime = None  # epoch of 'target position reached'
        self._transition_unsub = None
        self._transition_end_unsub = None
        self._attr_current_cover_position: int | None = None
        self._attr_extra_state_attributes = {}
        # flag indicating the device position is reliable (#227)
        # this will anyway be set in case we 'decode' a meaningful device position
        try:
            self._position_native_isgood = versiontuple(
                device.descriptor.firmware.get(mc.KEY_VERSION, "")
            ) >= versiontuple("7.6.10")
        except Exception:
            self._position_native_isgood = None

    @property
    def assumed_state(self):
        """RollerShutter position is unreliable"""
        return True

    @property
    def supported_features(self):
        return SUPPORT_OPEN | SUPPORT_CLOSE | SUPPORT_STOP | SUPPORT_SET_POSITION

    @property
    def is_opening(self):
        return self._attr_state == STATE_OPENING

    @property
    def is_closing(self):
        return self._attr_state == STATE_CLOSING

    @property
    def is_closed(self):
        return self._attr_state == STATE_CLOSED

    async def async_added_to_hass(self):
        await super().async_added_to_hass()
        """
        we're trying to recover the 'timed' position from previous state
        if it happens it wasn't updated too far in time
        """
        with self.exception_warning("restoring previous state"):
            if last_state := await get_entity_last_state(self.hass, self.entity_id):
                _attr = last_state.attributes  # type: ignore
                if EXTRA_ATTR_DURATION_OPEN in _attr:
                    self._signalOpen = _attr[EXTRA_ATTR_DURATION_OPEN]
                    self._attr_extra_state_attributes[
                        EXTRA_ATTR_DURATION_OPEN
                    ] = self._signalOpen
                if EXTRA_ATTR_DURATION_CLOSE in _attr:
                    self._signalClose = _attr[EXTRA_ATTR_DURATION_CLOSE]
                    self._attr_extra_state_attributes[
                        EXTRA_ATTR_DURATION_CLOSE
                    ] = self._signalClose
                if ATTR_CURRENT_POSITION in _attr:
                    self._attr_current_cover_position = _attr[ATTR_CURRENT_POSITION]

    async def async_will_remove_from_hass(self):
        self._transition_cancel()
        await super().async_will_remove_from_hass()

    async def async_open_cover(self, **kwargs):
        await self.async_request_position(POSITION_FULLY_OPENED)

    async def async_close_cover(self, **kwargs):
        await self.async_request_position(POSITION_FULLY_CLOSED)

    async def async_set_cover_position(self, **kwargs):
        if ATTR_POSITION in kwargs:
            position = kwargs[ATTR_POSITION]
            if (
                self._position_native_isgood
                or (position == POSITION_FULLY_OPENED)
                or (position == POSITION_FULLY_CLOSED)
            ):
                # ensure a full 'untimed' run when asked for
                # fully opened/closed (#170)
                await self.async_request_position(position)
            else:
                if position > self._attr_current_cover_position:
                    await self.async_request_position(
                        POSITION_FULLY_OPENED,
                        (
                            (position - self._attr_current_cover_position)
                            * self._signalOpen
                        )
                        / 100000,
                    )
                elif position < self._attr_current_cover_position:
                    await self.async_request_position(
                        POSITION_FULLY_CLOSED,
                        (
                            (self._attr_current_cover_position - position)
                            * self._signalClose
                        )
                        / 100000,
                    )

    async def async_stop_cover(self, **kwargs):
        await self.async_request_position(-1)

    def request_position(self, position: int, timeout: float | None = None):
        self.hass.async_create_task(self.async_request_position(position, timeout))

    async def async_request_position(self, position: int, timeout: float | None = None):
        def _ack_callback(acknowledge: bool, header: dict, payload: dict):
            if acknowledge:
                # the _ack_callback might be async'd (on MQTT) so
                # we re-ensure current transitions are clean
                self._transition_cancel()
                self._transition_unsub = schedule_async_callback(
                    self.hass, 0, self._async_transition_callback
                )
                if timeout is not None:
                    self._position_endtime = time() + timeout
                    self._transition_end_unsub = schedule_callback(
                        self.hass, timeout, self._transition_end_callback
                    )

        self._transition_cancel()
        await self.device.async_request(
            mc.NS_APPLIANCE_ROLLERSHUTTER_POSITION,
            mc.METHOD_SET,
            {
                mc.KEY_POSITION: {
                    mc.KEY_CHANNEL: self.channel,
                    mc.KEY_POSITION: position,
                }
            },
            _ack_callback,
        )

    def set_unavailable(self):
        self._transition_cancel()
        super().set_unavailable()

    def _parse_position(self, payload: dict):
        """
        legacy devices only reported 0 or 100 as position
        so we used to store this as an extra attribute and perform
        a trajectory calculation to emulate time based positioning
        now (#227) we'll detect devices reporting 'actual' good
        positioning and switch entity behaviour to trust this value
        bypassing all of the 'time based' emulation
        """
        if not isinstance(position := payload.get(mc.KEY_POSITION), int):
            return

        if self._position_native_isgood:
            if position != self._attr_current_cover_position:
                self._attr_current_cover_position = position
                if self._hass_connected:
                    self._async_write_ha_state()
            return

        if position == self._position_native:
            # no news...
            return

        if (position > 0) and (position < 100):
            # detecting a device reporting 'good' positions
            self._position_native_isgood = True
            self._position_native = None
            self._attr_extra_state_attributes.pop(EXTRA_ATTR_POSITION_NATIVE, None)
            self._attr_current_cover_position = position
        else:
            self._position_native = position
            self._attr_extra_state_attributes[EXTRA_ATTR_POSITION_NATIVE] = position
        if self._hass_connected:
            self._async_write_ha_state()

    def _parse_state(self, payload: dict):
        epoch = self.device.lastresponse
        state = payload.get(mc.KEY_STATE)
        if self._position_native_isgood:
            if state == mc.ROLLERSHUTTER_STATE_OPENING:
                self.update_state(STATE_OPENING)
            elif state == mc.ROLLERSHUTTER_STATE_CLOSING:
                self.update_state(STATE_CLOSING)
            else:  # state == mc.ROLLERSHUTTER_STATE_IDLE:
                self._transition_cancel()
                self.update_state(
                    STATE_OPEN if self._attr_current_cover_position else STATE_CLOSED
                )
                return
        else:
            if self._attr_state == STATE_OPENING:
                self._attr_current_cover_position = int(self._position_start + ((epoch - self._position_starttime) * 100000) / self._signalOpen)  # type: ignore
                if self._attr_current_cover_position > POSITION_FULLY_OPENED:
                    self._attr_current_cover_position = POSITION_FULLY_OPENED
                if self._hass_connected and (state == mc.ROLLERSHUTTER_STATE_OPENING):
                    self._async_write_ha_state()
            elif self._attr_state == STATE_CLOSING:
                self._attr_current_cover_position = int(self._position_start - ((epoch - self._position_starttime) * 100000) / self._signalClose)  # type: ignore
                if self._attr_current_cover_position < POSITION_FULLY_CLOSED:
                    self._attr_current_cover_position = POSITION_FULLY_CLOSED
                if self._hass_connected and (state == mc.ROLLERSHUTTER_STATE_CLOSING):
                    self._async_write_ha_state()

            if state == mc.ROLLERSHUTTER_STATE_OPENING:
                if self._attr_state != STATE_OPENING:
                    self._position_start = (
                        self._attr_current_cover_position
                        if self._attr_current_cover_position is not None
                        else POSITION_FULLY_CLOSED
                    )
                    self._position_starttime = epoch
                    self.update_state(STATE_OPENING)
            elif state == mc.ROLLERSHUTTER_STATE_CLOSING:
                if self._attr_state != STATE_CLOSING:
                    self._position_start = (
                        self._attr_current_cover_position
                        if self._attr_current_cover_position is not None
                        else POSITION_FULLY_OPENED
                    )
                    self._position_starttime = epoch
                    self.update_state(STATE_CLOSING)
            else:  # state == mc.ROLLERSHUTTER_STATE_IDLE:
                self._transition_cancel()
                self.update_state(
                    STATE_OPEN if self.current_cover_position else STATE_CLOSED
                )
                return

        # here the cover is moving
        if self._position_endtime is not None:
            # in case our _close_calback has not yet been called or failed
            if epoch >= self._position_endtime:
                self.request_position(-1)
                return

        if self._transition_unsub is None:
            # ensure we 'follow' cover movement
            self._transition_unsub = schedule_async_callback(
                self.hass, 2, self._async_transition_callback
            )

    def _parse_config(self, payload: dict):
        # payload = {"channel": 0, "signalOpen": 50000, "signalClose": 50000}
        if mc.KEY_SIGNALOPEN in payload:
            self._signalOpen = payload[
                mc.KEY_SIGNALOPEN
            ]  # time to fully open cover in msec
            self.number_signalOpen.update_native_value(self._signalOpen)
            self._attr_extra_state_attributes[
                EXTRA_ATTR_DURATION_OPEN
            ] = self._signalOpen
        if mc.KEY_SIGNALCLOSE in payload:
            self._signalClose = payload[
                mc.KEY_SIGNALCLOSE
            ]  # time to fully close cover in msec
            self.number_signalClose.update_native_value(self._signalClose)
            self._attr_extra_state_attributes[
                EXTRA_ATTR_DURATION_CLOSE
            ] = self._signalClose

    async def _async_transition_callback(self):
        self._transition_unsub = schedule_async_callback(
            self.hass, 2, self._async_transition_callback
        )
        device = self.device
        if device.curr_protocol is CONF_PROTOCOL_HTTP and device._mqtt_active is None:
            await device.async_http_request(
                *get_default_arguments(mc.NS_APPLIANCE_ROLLERSHUTTER_STATE)
            )
            if self._position_native_isgood:
                await device.async_http_request(
                    *get_default_arguments(mc.NS_APPLIANCE_ROLLERSHUTTER_POSITION)
                )

    @callback
    def _transition_end_callback(self):
        self.log(DEBUG, "_transition_end_callback")
        self._transition_end_unsub = None
        self.request_position(-1)

    def _transition_cancel(self):
        self._position_endtime = None
        if self._transition_end_unsub is not None:
            self._transition_end_unsub.cancel()
            self._transition_end_unsub = None
        if self._transition_unsub is not None:
            self._transition_unsub.cancel()
            self._transition_unsub = None


class MLRollerShutterConfigNumber(MLConfigNumber):
    """
    Helper entity to configure MRS open/close duration
    """

    __slots__ = ("_cover",)

    def __init__(self, cover: MLRollerShutter, key: str):
        self._cover = cover
        self.key_value = key
        self._attr_name = key
        self._attr_native_max_value = 60
        self._attr_native_min_value = 1
        self._attr_native_step = 1
        super().__init__(cover.device, cover.channel, f"config_{key}")

    @property
    def native_unit_of_measurement(self):
        return TIME_SECONDS

    async def async_set_native_value(self, value: float):
        config = {
            mc.KEY_CHANNEL: self.channel,
            mc.KEY_SIGNALOPEN: self._cover._signalOpen,
            mc.KEY_SIGNALCLOSE: self._cover._signalClose,
        }
        config[self.key_value] = int(value * 1000)

        def _ack_callback(acknowledge: bool, header: dict, payload: dict):
            if acknowledge:
                self._cover._parse_config(config)

        await self.device.async_request(
            mc.NS_APPLIANCE_ROLLERSHUTTER_CONFIG,
            mc.METHOD_SET,
            {mc.KEY_CONFIG: [config]},
            _ack_callback,
        )

    @property
    def ml_multiplier(self):
        return 1000


class RollerShutterMixin(
    MerossDevice if typing.TYPE_CHECKING else object
):  # pylint: disable=used-before-assignment
    def __init__(self, descriptor, entry):
        super().__init__(descriptor, entry)
        with self.exception_warning("RollerShutterMixin init"):
            # looks like digest (in NS_ALL) doesn't carry state
            # so we're not implementing _init_xxx and _parse_xxx methods here
            MLRollerShutter(self, 0)
            self.polling_dictionary[
                mc.NS_APPLIANCE_ROLLERSHUTTER_STATE
            ] = mc.PAYLOAD_GET[mc.NS_APPLIANCE_ROLLERSHUTTER_STATE]
            self.polling_dictionary[
                mc.NS_APPLIANCE_ROLLERSHUTTER_POSITION
            ] = mc.PAYLOAD_GET[mc.NS_APPLIANCE_ROLLERSHUTTER_POSITION]
            self.polling_dictionary[
                mc.NS_APPLIANCE_ROLLERSHUTTER_CONFIG
            ] = mc.PAYLOAD_GET[mc.NS_APPLIANCE_ROLLERSHUTTER_CONFIG]

    def _handle_Appliance_RollerShutter_Position(self, header: dict, payload: dict):
        self._parse__generic_array(mc.KEY_POSITION, payload.get(mc.KEY_POSITION))

    def _handle_Appliance_RollerShutter_State(self, header: dict, payload: dict):
        self._parse__generic_array(mc.KEY_STATE, payload.get(mc.KEY_STATE))

    def _handle_Appliance_RollerShutter_Config(self, header: dict, payload: dict):
        self._parse__generic_array(mc.KEY_CONFIG, payload.get(mc.KEY_CONFIG))
