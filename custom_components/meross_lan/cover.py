from __future__ import annotations
from time import time
from datetime import datetime
from logging import DEBUG, WARNING

from homeassistant.components.cover import (
    CoverEntity,
    DEVICE_CLASS_GARAGE, DEVICE_CLASS_SHUTTER,
    ATTR_POSITION,
    SUPPORT_OPEN, SUPPORT_CLOSE, SUPPORT_SET_POSITION, SUPPORT_STOP,
    STATE_OPEN, STATE_OPENING, STATE_CLOSED, STATE_CLOSING
)
from homeassistant.core import HassJob, callback
from homeassistant.helpers import event

from .merossclient import const as mc
from .meross_entity import _MerossEntity, platform_setup_entry, platform_unload_entry
from .const import (
    PLATFORM_COVER,
    PARAM_GARAGEDOOR_TRANSITION_MAXDURATION,
    PARAM_GARAGEDOOR_TRANSITION_MINDURATION,
)

NOTIFICATION_ID_TIMEOUT = 'garagedoor_timeout'
EXTRA_ATTR_TRANSITION_DURATION = 'transition_duration'
EXTRA_ATTR_TRANSITION_TIMEOUT = 'transition_timeout' # the time at which the transition timeout occurred
EXTRA_ATTR_TRANSITION_TARGET = 'transition_target' # the target state which was not reached

async def async_setup_entry(hass: object, config_entry: object, async_add_devices):
    platform_setup_entry(hass, config_entry, async_add_devices, PLATFORM_COVER)

async def async_unload_entry(hass: object, config_entry: object) -> bool:
    return platform_unload_entry(hass, config_entry, PLATFORM_COVER)


class MerossLanGarage(_MerossEntity, CoverEntity):

    PLATFORM = PLATFORM_COVER

    def __init__(self, device: 'MerossDevice', id: object):
        super().__init__(device, id, DEVICE_CLASS_GARAGE)
        self._payload = {mc.KEY_STATE: {mc.KEY_CHANNEL: id, mc.KEY_OPEN: 0 } }
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
        return self._state == STATE_OPENING


    @property
    def is_closing(self):
        return self._state == STATE_CLOSING


    @property
    def is_closed(self):
        return self._state == STATE_CLOSED


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
        self._device.request(
            mc.NS_APPLIANCE_GARAGEDOOR_STATE,
            mc.METHOD_SET,
            self._payload,
            _ack_callback
            )


    async def async_will_remove_from_hass(self) -> None:
        self._cancel_transition()


    def _set_unavailable(self) -> None:
        self._open = None
        self._cancel_transition()
        super()._set_unavailable()


    def _set_open(self, open, execute) -> None:
        now = time()
        self._open = open

        if execute:
            if self._open_pending == open:
                self._device.log(DEBUG, 0, "MerossLanGarage(%s): ignoring start of ghost transition", self.name)
                #continue processing after this
            elif self._open_pending is not None:
                if self._transition_unsub is not None:
                    self._transition_unsub()
                    self._transition_unsub = None
                    self._device.log(WARNING, 0, "MerossLanGarage(%s): re-starting an overlapped transition ", self.name)
                self._start_transition()
                self._state_lastupdate = now
                return

        if self._transition_unsub is None:
            if open:
                if self._state is not STATE_OPEN:
                    if (now - self._state_lastupdate) > self._transition_duration:
                        # the polling period is likely too long..we skip the transition
                        self._set_state(STATE_OPEN)
                    else:
                        # when opening the contact will report open right after few inches
                        self._open_pending = open
                        self._start_transition()
            else: # when reporting 'closed' the transition would be ended (almost)
                self._set_state(STATE_CLOSED)
        else:
            transition_duration = now - self._transition_start
            if self._open_pending:
                if open and transition_duration > self._transition_duration:
                    self._cancel_transition()
                    self._set_state(STATE_OPEN)
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
                    self._device.log(DEBUG, 0, "MerossLanGarage(%s): updated transition_duration to %d sec", self.name, self._transition_duration)
                    self._cancel_transition()
                    self._set_state(STATE_CLOSED)

        self._state_lastupdate = now


    def _set_onoff(self, onoff) -> None:
        """
        MSG100 exposes a 'togglex' interface so my code interprets that as a switch state
        Here we'll intercept that behaviour and right now the guess is:
        The toggle state represents the contact of the garagedoor which is likely a short
        pulse so we'll use it to guess state transitions in our cover (disabled this until further knowledge)

        if onoff:
            if self._state == STATE_CLOSED:
                self._start_transition(STATE_OPEN)
            elif self._state == STATE_OPEN:
                self._start_transition(STATE_CLOSED)
        #else: RIP!

        """


    def _start_transition(self):
        self._transition_start = time()
        self._set_state(STATE_OPENING if self._open_pending else STATE_CLOSING)
        # this callback will get called some secs after the estimated transition occur
        # in order for the estimation algorithm to always/mostly work (see '_set_open')
        # especially on MQTT where we would expect real time status updates.
        # Also, the _transition_duration we estimate is shorter of the real duration
        # because the garage contact will close before actually finishing the transition
        # so , this couple secs, will not be that wrong anyway
        self._transition_unsub = event.async_track_point_in_utc_time(
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
        self._set_state(STATE_OPEN if self._open else STATE_CLOSED)
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



class MerossLanRollerShutter(_MerossEntity, CoverEntity):

    PLATFORM = PLATFORM_COVER

    def __init__(self, device: 'MerossDevice', id: object):
        super().__init__(device, id, DEVICE_CLASS_SHUTTER)
        self._payload = {mc.KEY_POSITION: {mc.KEY_CHANNEL: id, mc.KEY_POSITION: 0}}
        self._position = None
        self._transition_unsub = None
        self._transition_job = HassJob(self._transition_callback)


    @property
    def assumed_state(self) -> bool:
        """RollerShutter position is unreliable"""
        return True


    @property
    def supported_features(self):
        return SUPPORT_OPEN | SUPPORT_CLOSE | SUPPORT_STOP | SUPPORT_SET_POSITION


    @property
    def is_opening(self):
        return self._state == STATE_OPENING


    @property
    def is_closing(self):
        return self._state == STATE_CLOSING


    @property
    def is_closed(self):
        return self._state == STATE_CLOSED


    @property
    def current_cover_position(self):
        return self._position


    async def async_open_cover(self, **kwargs) -> None:
        self.request(100)


    async def async_close_cover(self, **kwargs) -> None:
        self.request(0)


    async def async_set_cover_position(self, **kwargs):
        if ATTR_POSITION in kwargs:
            position = kwargs[ATTR_POSITION]
            self.request(position)


    async def async_stop_cover(self, **kwargs):
        self.request(-1)


    def request(self, position):
        self._payload[mc.KEY_POSITION][mc.KEY_POSITION] = position
        self._device.request(
            mc.NS_APPLIANCE_ROLLERSHUTTER_POSITION,
            mc.METHOD_SET,
            self._payload
            )


    def _set_unavailable(self) -> None:
        self._cancel_transition()
        self._position = None
        super()._set_unavailable()


    def _set_rollerposition(self, position) -> None:
        self._position = position
        if self._state not in (STATE_OPENING, STATE_CLOSING):
            self._set_state(STATE_OPEN if self._position else STATE_CLOSED)


    def _set_rollerstate(self, state) -> None:

        self._cancel_transition()

        if state == 1:
            self._set_state(STATE_OPENING)
        elif state == 2:
            self._set_state(STATE_CLOSING)
        else:
            self._set_state(STATE_OPEN if self._position else STATE_CLOSED)
            return
        #when moving we'll ask an update sooner
        self._transition_unsub = event.async_track_point_in_utc_time(
            self.hass,
            self._transition_job,
            datetime.fromtimestamp(time() + 2)
        )


    @callback
    def _transition_callback(self, _now: datetime) -> None:
        """
        called by the event loop every so often to refresh movement state
        """
        self._transition_unsub = None
        self._device.request(mc.NS_APPLIANCE_ROLLERSHUTTER_POSITION,
            mc.METHOD_GET, { mc.KEY_POSITION : [] })
        self._device.request(mc.NS_APPLIANCE_ROLLERSHUTTER_STATE,
            mc.METHOD_GET, { mc.KEY_STATE : [] })


    def _cancel_transition(self):
        if self._transition_unsub is not None:
            self._transition_unsub()
            self._transition_unsub = None

