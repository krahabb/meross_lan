from __future__ import annotations
from time import time
from datetime import datetime
from logging import DEBUG, INFO, WARNING

from homeassistant.components.cover import (
    DOMAIN as PLATFORM_COVER,
    CoverEntity,
    DEVICE_CLASS_GARAGE, DEVICE_CLASS_SHUTTER,
    ATTR_POSITION,
    SUPPORT_OPEN, SUPPORT_CLOSE, SUPPORT_SET_POSITION, SUPPORT_STOP,
    STATE_OPEN, STATE_OPENING, STATE_CLOSED, STATE_CLOSING
)
from homeassistant.core import HassJob, callback
from homeassistant.helpers.event import async_track_point_in_utc_time

from .helpers import LOGGER

from .merossclient import const as mc
from .meross_entity import _MerossEntity, platform_setup_entry, platform_unload_entry
from .number import MLConfigNumber
from .switch import MLConfigSwitch
from .const import (
    PARAM_GARAGEDOOR_TRANSITION_MAXDURATION,
    PARAM_GARAGEDOOR_TRANSITION_MINDURATION,
)

POSITION_FULLY_CLOSED = 0
POSITION_FULLY_OPENED = 100

# garagedoor extra attributes
NOTIFICATION_ID_TIMEOUT = 'garagedoor_timeout'
EXTRA_ATTR_TRANSITION_DURATION = 'transition_duration'
EXTRA_ATTR_TRANSITION_TIMEOUT = 'transition_timeout' # the time at which the transition timeout occurred
EXTRA_ATTR_TRANSITION_TARGET = 'transition_target' # the target state which was not reached

# rollershutter extra attributes
EXTRA_ATTR_DURATION_OPEN = 'duration_open'
EXTRA_ATTR_DURATION_CLOSE = 'duration_close'
EXTRA_ATTR_POSITION_NATIVE = 'position_native'

async def async_setup_entry(hass: object, config_entry: object, async_add_devices):
    platform_setup_entry(hass, config_entry, async_add_devices, PLATFORM_COVER)

async def async_unload_entry(hass: object, config_entry: object) -> bool:
    return platform_unload_entry(hass, config_entry, PLATFORM_COVER)


class MLGarage(_MerossEntity, CoverEntity):

    PLATFORM = PLATFORM_COVER

    def __init__(self, device: 'MerossDevice', channel: object):
        super().__init__(device, channel, None, DEVICE_CLASS_GARAGE)
        self._payload = {mc.KEY_STATE: {mc.KEY_CHANNEL: channel, mc.KEY_OPEN: 0 } }
        self._transition_duration = (PARAM_GARAGEDOOR_TRANSITION_MAXDURATION + PARAM_GARAGEDOOR_TRANSITION_MINDURATION) / 2
        self._transition_start = 0
        self._transition_end_job = HassJob(self._transition_end_callback)
        self._transition_unsub = None
        self._state_lastupdate = 0
        self._open = None # this is the last known (or actual) physical state from device state
        self._open_pending = None # cache since device reply doesnt report it (just actual state)
        self._attr_extra_state_attributes = dict()
        self._attr_extra_state_attributes[EXTRA_ATTR_TRANSITION_DURATION] = self._transition_duration


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


    async def async_open_cover(self, **kwargs) -> None:
        self.request(1)


    async def async_close_cover(self, **kwargs) -> None:
        self.request(0)


    def request(self, open):
        def _ack_callback():
            """
            this will be calledback on HTTP only. The confirmation
            payload itself from the garagedoor (talking about HTTP) is anyway processed
            from the standard parser (this payload will carry status informations)
            example payload in SETACK:
            {"state": {"channel": 0, "open": 0, "lmTime": 0, "execute": 1}}
            "open" reports the current state and not the command
            "execute" represent command ack (I guess: never seen this == 0)
            Beware: if the garage is 'closed' and we send a 'close' "execute" will
            be replied as "1" and the garage will stay closed
            """
            pass

        self._open_pending = open
        self._payload[mc.KEY_STATE][mc.KEY_OPEN] = open
        self.device.request(
            mc.NS_APPLIANCE_GARAGEDOOR_STATE,
            mc.METHOD_SET,
            self._payload,
            _ack_callback
            )


    async def async_will_remove_from_hass(self) -> None:
        self._cancel_transition()


    def set_unavailable(self) -> None:
        self._open = None
        self._cancel_transition()
        super().set_unavailable()


    def _parse_state(self, payload: dict) -> None:
        #{"channel": 0, "open": 1, "lmTime": 0, "execute": 1}
        now = time()
        open = payload.get(mc.KEY_OPEN)
        execute = payload.get(mc.KEY_EXECUTE)
        self._open = open

        if execute:
            if self._open_pending == open:
                self.device.log(DEBUG, 0, "MLGarage(%s): ignoring start of ghost transition", self.name)
                #continue processing after this
            elif self._open_pending is not None:
                if self._transition_unsub is not None:
                    self._transition_unsub()
                    self._transition_unsub = None
                    self.device.log(WARNING, 0, "MLGarage(%s): re-starting an overlapped transition ", self.name)
                self._start_transition()
                self._state_lastupdate = now
                return

        if self._transition_unsub is None:
            if open:
                if self._attr_state is not STATE_OPEN:
                    if (now - self._state_lastupdate) > self._transition_duration:
                        # the polling period is likely too long..we skip the transition
                        self.update_state(STATE_OPEN)
                    else:
                        # when opening the contact will report open right after few inches
                        self._open_pending = open
                        self._start_transition()
            else: # when reporting 'closed' the transition would be ended (almost)
                self.update_state(STATE_CLOSED)
        else:
            transition_duration = now - self._transition_start
            if self._open_pending:
                if open and transition_duration > self._transition_duration:
                    self._cancel_transition()
                    self.update_state(STATE_OPEN)
            else: # not _open_pending
                """
                we can monitor the (sampled) exact time when the garage closes to
                estimate the transition_duration and dynamically update it since
                during the transition the state will be closed only at the end
                while during opening the garagedoor contact will open right at the beginning
                and so will be unuseful
                Also to note: if we're on HTTP this sampled time could happen anyway after the 'real'
                state switched to 'closed' so we're likely going to measure in exceed of real transition duration
                """
                if not open:
                    # autoregression filtering applying 20% of last updated sample
                    self._transition_duration = int((4 * self._transition_duration + transition_duration) / 5)
                    if self._transition_duration > PARAM_GARAGEDOOR_TRANSITION_MAXDURATION:
                        self._transition_duration = PARAM_GARAGEDOOR_TRANSITION_MAXDURATION
                    elif self._transition_duration < PARAM_GARAGEDOOR_TRANSITION_MINDURATION:
                        self._transition_duration = PARAM_GARAGEDOOR_TRANSITION_MINDURATION
                    self._attr_extra_state_attributes[EXTRA_ATTR_TRANSITION_DURATION] = self._transition_duration
                    self.device.log(DEBUG, 0, "MLGarage(%s): updated transition_duration to %d sec", self.name, self._transition_duration)
                    self._cancel_transition()
                    self.update_state(STATE_CLOSED)

        self._state_lastupdate = now


    def update_onoff(self, onoff) -> None:
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


    def _start_transition(self):
        self._transition_start = time()
        self.update_state(STATE_OPENING if self._open_pending else STATE_CLOSING)
        # this callback will get called some secs after the estimated transition occur
        # in order for the estimation algorithm to always/mostly work (see '_set_open')
        # especially on MQTT where we would expect real time status updates.
        # Also, the _transition_duration we estimate is shorter of the real duration
        # because the garage contact will close before actually finishing the transition
        # so , this couple secs, will not be that wrong anyway
        self._transition_unsub = async_track_point_in_utc_time(
            self.hass,
            self._transition_end_job,
            datetime.fromtimestamp(self._transition_start + self._transition_duration + 5)
        )


    def _cancel_transition(self):
        if self._transition_unsub is not None:
            self._transition_unsub()
            self._transition_unsub = None
        self._open_pending = None


    @callback
    def _transition_end_callback(self, _now: datetime) -> None:
        """
        called by the event loop some 'self._transition_duration' after starting
        a transition
        """
        self._transition_unsub = None
        # transition ended: set the state according to our last known hardware status
        self.update_state(STATE_OPEN if self._open else STATE_CLOSED)
        if not self._open_pending:
            # when closing we expect this callback not to be called since
            # the transition should be terminated by '_set_open' provided it gets
            # called on time (on polling this is not guaranteed).
            # If we're here, we still havent received a proper 'physical close'
            # because our estimate is too short or the garage didnt close at all
            if self._transition_duration < PARAM_GARAGEDOOR_TRANSITION_MAXDURATION:
                self._transition_duration = self._transition_duration + 1
                self._attr_extra_state_attributes[EXTRA_ATTR_TRANSITION_DURATION] = self._transition_duration

        if self._open_pending == self._open:
            self._attr_extra_state_attributes.pop(EXTRA_ATTR_TRANSITION_TIMEOUT, None)
            self._attr_extra_state_attributes.pop(EXTRA_ATTR_TRANSITION_TARGET, None)
            """self.hass.components.persistent_notification.async_dismiss(
                notification_id=NOTIFICATION_ID_TIMEOUT + '.' + self.unique_id
            )"""
        else:
            state_pending = (STATE_OPEN if self._open_pending else STATE_CLOSED)
            self._attr_extra_state_attributes[EXTRA_ATTR_TRANSITION_TARGET] = state_pending
            self._attr_extra_state_attributes[EXTRA_ATTR_TRANSITION_TIMEOUT] = str(_now)
            """self.hass.components.persistent_notification.async_create(
                title=self.entryname,
                message=f"Garage door didn't reach the '{state_pending}' state on time",
                notification_id=NOTIFICATION_ID_TIMEOUT + '.' + self.unique_id,
            )"""
        self._open_pending = None


class MLGarageConfigNumber(MLConfigNumber):
    """
    Helper entity to configure MRS open/close duration
    """
    multiplier = 1000

    # these are ok for 2 of the 3 config numbers
    # customize those when instantiating
    _attr_min_value = 1
    _attr_max_value = 60
    _attr_step = 1

    def __init__(self, device, key: str):
        self._key = key
        super().__init__(device, None, f"config_{key}")


    @property
    def name(self) -> str:
        return f"{super().name} - {self._key}"


    async def async_set_value(self, value: float) -> None:
        config = dict(self.device.garageDoor_config)
        config[self._key] = int(value * self.multiplier)

        def _ack_callback():
            self.device.garageDoor_config[self._key] = config[self._key]

        self.device.request(
            mc.NS_APPLIANCE_GARAGEDOOR_CONFIG,
            mc.METHOD_SET,
            { mc.KEY_CONFIG: config },
            _ack_callback
        )


class MLGarageConfigSwitch(MLConfigSwitch):


    def __init__(self, device, key: str):
        self._key = key
        super().__init__(device, None, f"config_{key}", None, None)


    @property
    def name(self) -> str:
        return f"{super().name} - {self._key}"


    def request_onoff(self, onoff):
        config = dict(self.device.garageDoor_config)
        config[self._key] = onoff

        def _ack_callback():
            self.device.garageDoor_config[self._key] = config[self._key]

        self.device.request(
            mc.NS_APPLIANCE_GARAGEDOOR_CONFIG,
            mc.METHOD_SET,
            { mc.KEY_CONFIG: config },
            _ack_callback
        )



class GarageMixin:

    def __init__(self, api, descriptor, entry) -> None:
        super().__init__(api, descriptor, entry)
        if mc.NS_APPLIANCE_GARAGEDOOR_CONFIG in descriptor.ability:
            self.garageDoor_config =  {}
            self.config_signalDuration = MLGarageConfigNumber(self, mc.KEY_SIGNALDURATION)
            self.config_signalDuration._attr_step = 0.1 # 100 msec step in UI
            self.config_signalDuration._attr_min_value = 0.1 # 100 msec minimum duration
            self.config_buzzerEnable = MLGarageConfigSwitch(self, mc.KEY_BUZZERENABLE)
            self.config_doorOpenDuration = MLGarageConfigNumber(self, mc.KEY_DOOROPENDURATION)
            self.config_doorCloseDuration = MLGarageConfigNumber(self, mc.KEY_DOORCLOSEDURATION)
            self.polling_dictionary.add(mc.NS_APPLIANCE_GARAGEDOOR_CONFIG)


    def _init_garageDoor(self, payload: dict):
        MLGarage(self, payload[mc.KEY_CHANNEL])


    def _handle_Appliance_GarageDoor_State(self,
    namespace: str, method: str, payload: dict, header: dict):
        self._parse__generic(mc.KEY_STATE, payload.get(mc.KEY_STATE))


    def _handle_Appliance_GarageDoor_Config(self,
    namespace: str, method: str, payload: dict, header: dict):
        #{"config": {"signalDuration": 1000, "buzzerEnable": 0, "doorOpenDuration": 30000, "doorCloseDuration": 30000}}
        # no channel here ?!..need to parse the manual way
        payload = payload.get(mc.KEY_CONFIG)
        if isinstance(payload, dict):
            self.garageDoor_config.update(payload)
            if mc.KEY_SIGNALDURATION in payload:
                self.config_signalDuration.update_value(payload[mc.KEY_SIGNALDURATION])
            if mc.KEY_BUZZERENABLE in payload:
                self.config_buzzerEnable.update_onoff(payload[mc.KEY_BUZZERENABLE])
            if mc.KEY_DOOROPENDURATION in payload:
                self.config_doorOpenDuration.update_value(payload[mc.KEY_DOOROPENDURATION])
            if mc.KEY_DOORCLOSEDURATION in payload:
                self.config_doorCloseDuration.update_value(payload[mc.KEY_DOORCLOSEDURATION])
        return


    def _parse_garageDoor(self, payload):
        self._parse__generic(mc.KEY_STATE, payload)



class MLRollerShutter(_MerossEntity, CoverEntity):

    PLATFORM = PLATFORM_COVER

    def __init__(self, device: 'MerossDevice', channel: object):
        super().__init__(device, channel, None, DEVICE_CLASS_SHUTTER)
        self._number_signalOpen = MLRollerShutterConfigNumber(self, mc.KEY_SIGNALOPEN)
        self._number_signalClose = MLRollerShutterConfigNumber(self, mc.KEY_SIGNALCLOSE)
        self._position_native = None # as reported by the device
        self._signalOpen: int = 30000 # msec to fully open (config'd on device)
        self._signalClose: int = 30000 # msec to fully close (config'd on device)
        self._position_timed = None # estimate based on timings
        self._position_start = None # set when when we're controlling a timed position
        self._position_starttime = None # epoch of transition start
        self._position_endtime = None # epoch of 'target position reached'
        self._transition_unsub = None
        self._transition_job = HassJob(self._transition_callback) # job to follow transition
        self._stop_unsub = None
        self._stop_job = HassJob(self._stop_callback) # job to terminate transition
        self._attr_extra_state_attributes = dict()


    @property
    def assumed_state(self) -> bool:
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


    @property
    def current_cover_position(self):
        return self._position_timed if self._position_timed is not None else self._position_native


    @property
    def is_position_native(self) -> bool:
        return self._position_timed is None


    async def async_open_cover(self, **kwargs) -> None:
        self.request_position(POSITION_FULLY_OPENED)


    async def async_close_cover(self, **kwargs) -> None:
        self.request_position(POSITION_FULLY_CLOSED)


    async def async_set_cover_position(self, **kwargs):
        if ATTR_POSITION in kwargs:
            position = kwargs[ATTR_POSITION]
            if self.is_position_native\
                or (position == POSITION_FULLY_OPENED)\
                or (position == POSITION_FULLY_CLOSED):
                # ensure a full 'untimed' run when asked for
                # fully opened/closed (#170)
                self.request_position(position)
            else:
                if position > self._position_timed:
                    self.request_position(
                        100,
                        ((position - self._position_timed) * self._signalOpen) / 100000
                    )
                elif position < self._position_timed:
                    self.request_position(
                        0,
                        ((self._position_timed - position) * self._signalClose) / 100000
                    )


    async def async_stop_cover(self, **kwargs):
        self.request_position(-1)


    def request_position(self, command, timeout = None):
        self.device.log(DEBUG, 0, "MLRollerShutter(0): request_position(%s, %s)", str(command), str(timeout))
        def _ack_callback():
            self.device.log(DEBUG, 0, "MLRollerShutter(0): _ack_callback")
            if timeout is not None:
                self._position_endtime = time() + timeout
                self._stop_unsub = async_track_point_in_utc_time(
                    self.hass,
                    self._stop_job,
                    datetime.fromtimestamp(self._position_endtime)
                )
            self.device.request_get(mc.NS_APPLIANCE_ROLLERSHUTTER_STATE)

        self._stop_cancel()
        # WARNING: on MQTT we'll loose the ack callback since
        # it's not (yet) implemented and the option to correctly
        # update the state will be loosed since the ack payload is empty
        # right now 'force' http proto even tho that could be disabled in config
        self.hass.async_create_task(
            self.device.async_http_request(
                mc.NS_APPLIANCE_ROLLERSHUTTER_POSITION,
                mc.METHOD_SET,
                {mc.KEY_POSITION: {mc.KEY_CHANNEL: self.channel, mc.KEY_POSITION: command}},
                _ack_callback
            )
        )


    def set_unavailable(self) -> None:
        self._transition_cancel()
        self._stop_cancel()
        self._position_native = None
        super().set_unavailable()


    def _parse_position(self, payload: dict):
        position = payload.get(mc.KEY_POSITION)
        if position != self._position_native:
            self._position_native = position
            self._attr_extra_state_attributes[EXTRA_ATTR_POSITION_NATIVE] = position
            if self.hass and self.enabled:
                self.async_write_ha_state()


    def _parse_state(self, payload: dict):
        state = payload.get(mc.KEY_STATE)
        self.device.log(DEBUG, 0, "MLRollerShutter(0): _parse_state(%s)", str(state))
        epoch = time()
        if self._attr_state == STATE_OPENING:
            self._position_timed = int(self._position_start + ((epoch - self._position_starttime) * 100000) / self._signalOpen)
            if self._position_timed > POSITION_FULLY_OPENED:
                self._position_timed = POSITION_FULLY_OPENED
            if (state == mc.ROLLERSHUTTER_STATE_OPENING) and self.hass and self.enabled:
                self.async_write_ha_state()

        elif self._attr_state == STATE_CLOSING:
            self._position_timed = int(self._position_start - ((epoch - self._position_starttime) * 100000) / self._signalClose)
            if self._position_timed < POSITION_FULLY_CLOSED:
                self._position_timed = POSITION_FULLY_CLOSED
            if (state == mc.ROLLERSHUTTER_STATE_CLOSING) and self.hass and self.enabled:
                self.async_write_ha_state()

        if state == mc.ROLLERSHUTTER_STATE_OPENING:
            if self._attr_state != STATE_OPENING:
                self._position_start = self._position_timed\
                    if self._position_timed is not None else POSITION_FULLY_CLOSED
                self._position_starttime = epoch
                self.update_state(STATE_OPENING)
        elif state == mc.ROLLERSHUTTER_STATE_CLOSING:
            if self._attr_state != STATE_CLOSING:
                self._position_start = self._position_timed\
                    if self._position_timed is not None else POSITION_FULLY_OPENED
                self._position_starttime = epoch
                self.update_state(STATE_CLOSING)
        else: # state == mc.ROLLERSHUTTER_STATE_IDLE:
            self._stop_cancel()
            self._transition_cancel()
            self.update_state(STATE_OPEN if self.current_cover_position else STATE_CLOSED)
            return

        # here the cover is moving so...
        if self._position_endtime is not None:
            # in case our _close_calback has not been called or failed
            if epoch >= self._position_endtime:
                self.request_position(-1)

        if self._transition_unsub is None:
            # ensure we 'follow' cover movement
            self._transition_callback(None)


    def _parse_config(self, payload: dict) -> None:
        # payload = {"channel": 0, "signalOpen": 50000, "signalClose": 50000}
        if mc.KEY_SIGNALOPEN in payload:
            self._signalOpen = payload[mc.KEY_SIGNALOPEN] # time to fully open cover in msec
            self._number_signalOpen.update_value(self._signalOpen)
            self._attr_extra_state_attributes[EXTRA_ATTR_DURATION_OPEN] = self._signalOpen
        if mc.KEY_SIGNALCLOSE in payload:
            self._signalClose = payload[mc.KEY_SIGNALCLOSE] # time to fully close cover in msec
            self._number_signalClose.update_value(self._signalClose)
            self._attr_extra_state_attributes[EXTRA_ATTR_DURATION_CLOSE] = self._signalClose


    @callback
    def _transition_callback(self, _now: datetime) -> None:
        self.device.log(DEBUG, 0, "MLRollerShutter(0): _transition_callback")
        self._transition_unsub = async_track_point_in_utc_time(
            self.hass,
            self._transition_job,
            datetime.fromtimestamp(time() + 1)
        )
        self.device.request_get(mc.NS_APPLIANCE_ROLLERSHUTTER_STATE)


    def _transition_cancel(self):
        self.device.log(DEBUG, 0, "MLRollerShutter(0): _transition_cancel")
        if self._transition_unsub is not None:
            self._transition_unsub()
            self._transition_unsub = None


    @callback
    def _stop_callback(self, _now: datetime) -> None:
        self.device.log(DEBUG, 0, "MLRollerShutter(0): _stop_callback")
        self._stop_unsub = None
        self.request_position(-1)


    def _stop_cancel(self):
        self.device.log(DEBUG, 0, "MLRollerShutter(0): _stop_cancel")
        self._position_endtime = None
        if self._stop_unsub is not None:
            self._stop_unsub()
            self._stop_unsub = None



class MLRollerShutterConfigNumber(MLConfigNumber):
    """
    Helper entity to configure MRS open/close duration
    """
    multiplier = 1000

    _attr_min_value = 1
    _attr_max_value = 60
    _attr_step = 1

    def __init__(self, cover: MLRollerShutter, key: str):
        self._cover = cover
        self._key = key
        super().__init__(cover.device, cover.channel, f"config_{key}")


    @property
    def name(self) -> str:
        return f"{self._cover.name} - {self._key}"


    async def async_set_value(self, value: float) -> None:
        config = {
            mc.KEY_CHANNEL: self.channel,
            mc.KEY_SIGNALOPEN: self._cover._signalOpen,
            mc.KEY_SIGNALCLOSE: self._cover._signalClose
            }
        config[self._key] = int(value * 1000)

        def _ack_callback():
            self._cover._parse_config(config)

        self.device.request(
            mc.NS_APPLIANCE_ROLLERSHUTTER_CONFIG,
            mc.METHOD_SET,
            { mc.KEY_CONFIG: [config] },
            _ack_callback
        )



class RollerShutterMixin:


    def __init__(self, api, descriptor, entry):
        super().__init__(api, descriptor, entry)

        try:
            # looks like digest (in NS_ALL) doesn't carry state
            # so we're not implementing _init_xxx and _parse_xxx methods here
            MLRollerShutter(self, 0)
            self.polling_dictionary.add(mc.NS_APPLIANCE_ROLLERSHUTTER_POSITION)
            self.polling_dictionary.add(mc.NS_APPLIANCE_ROLLERSHUTTER_STATE)
            self.polling_dictionary.add(mc.NS_APPLIANCE_ROLLERSHUTTER_CONFIG)

        except Exception as e:
            LOGGER.warning("RollerShutterMixin(%s) init exception:(%s)", self.device_id, str(e))


    def _handle_Appliance_RollerShutter_Position(self,
    namespace: str, method: str, payload: dict, header: dict):
        self._parse__generic(mc.KEY_POSITION, payload.get(mc.KEY_POSITION))


    def _handle_Appliance_RollerShutter_State(self,
    namespace: str, method: str, payload: dict, header: dict):
        self._parse__generic(mc.KEY_STATE, payload.get(mc.KEY_STATE))


    def _handle_Appliance_RollerShutter_Config(self,
    namespace: str, method: str, payload: dict, header: dict):
        self._parse__generic(mc.KEY_CONFIG, payload.get(mc.KEY_CONFIG))

