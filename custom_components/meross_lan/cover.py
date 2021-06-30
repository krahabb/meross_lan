

from time import time
from datetime import datetime

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
from .meross_device import MerossDevice
from .meross_entity import _MerossEntity, platform_setup_entry, platform_unload_entry
from .const import (
    PLATFORM_COVER,
    PARAM_GARAGEDOOR_TRANSITION_MAXDURATION,
    PARAM_GARAGEDOOR_TRANSITION_MINDURATION,
)

async def async_setup_entry(hass: object, config_entry: object, async_add_devices):
    platform_setup_entry(hass, config_entry, async_add_devices, PLATFORM_COVER)

async def async_unload_entry(hass: object, config_entry: object) -> bool:
    return platform_unload_entry(hass, config_entry, PLATFORM_COVER)


class MerossLanGarage(_MerossEntity, CoverEntity):

    PLATFORM = PLATFORM_COVER

    def __init__(self, device: 'MerossDevice', id: object):
        super().__init__(device, id, DEVICE_CLASS_GARAGE)
        self._payload = {mc.KEY_STATE: {mc.KEY_OPEN: 0, mc.KEY_CHANNEL: id, mc.KEY_UUID: device.device_id } }
        self._transition_duration = (PARAM_GARAGEDOOR_TRANSITION_MAXDURATION + PARAM_GARAGEDOOR_TRANSITION_MINDURATION) / 2
        self._transition_start = 0
        self._transition_end_job = HassJob(self._transition_end_callback)
        self._state_lastupdate = 0
        self._state_pending = None # used when we start a transition to prevent 'async polling incursion'


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
        self._start_transition(STATE_OPEN)
        self._payload[mc.KEY_STATE][mc.KEY_OPEN] = 1
        self._device.request(
            namespace=mc.NS_APPLIANCE_GARAGEDOOR_STATE,
            method=mc.METHOD_SET,
            payload=self._payload)


    async def async_close_cover(self, **kwargs) -> None:
        self._start_transition(STATE_CLOSED)
        self._payload[mc.KEY_STATE][mc.KEY_OPEN] = 0
        self._device.request(
            namespace=mc.NS_APPLIANCE_GARAGEDOOR_STATE,
            method=mc.METHOD_SET,
            payload=self._payload)


    async def async_will_remove_from_hass(self) -> None:
        self._cancel_transition()


    def _set_unavailable(self) -> None:
        self._cancel_transition()
        super()._set_unavailable()


    def _set_open(self, open) -> None:
        now = time()

        if self._transition_unsub is not None:
            if (now - self._transition_start) > self._transition_duration:
                self._cancel_transition()
                self._set_state(STATE_OPEN if open else STATE_CLOSED)
            else:
                self._state_pending = STATE_OPEN if open else STATE_CLOSED
        else:
            # this state 'out of transition' likely represent an externally
            # triggered movement (scenario is device on HTTP and controlled by other means)
            if (now - self._state_lastupdate) > self._transition_duration:
                # the polling period is likely too long..we skip the transition
                self._set_state(STATE_OPEN if open else STATE_CLOSED)
            else:
                if open:
                    if self._state is not STATE_OPEN:
                        self._start_transition(STATE_OPEN)
                else:
                    if self._state is not STATE_CLOSED:
                        self._start_transition(STATE_CLOSED)

        self._state_lastupdate = now


    def _set_onoff(self, onoff) -> None:
        """
        MSG100 exposes a 'togglex' interface so my code interprets that as a switch state
        Here we'll intercept that behaviour and right now the guess is:
        The toggle state represents the contact of the garagedoor which is likely a short
        pulse so we'll use it to guess state transitions in our cover
        """
        if onoff:
            if self._state == STATE_CLOSED:
                self._start_transition(STATE_OPEN)
            elif self._state == STATE_OPEN:
                self._start_transition(STATE_CLOSED)
        #else: RIP!


    def _start_transition(self, state):
        self._cancel_transition()
        self._transition_start = time()
        self._state_pending = state
        self._set_state(STATE_OPENING if state is STATE_OPEN else STATE_CLOSING)
        self._transition_unsub = event.async_track_point_in_utc_time(
            self.hass,
            self._transition_end_job,
            datetime.fromtimestamp(self._transition_start + self._transition_duration)
        )


    def _cancel_transition(self):
        if self._transition_unsub is not None:
            self._transition_unsub()
            self._transition_unsub = None
        self._state_pending = None


    @callback
    def _transition_end_callback(self, _now: datetime) -> None:
        """
        called by the event loop some 'self._transition_duration' after starting
        a transition
        """
        self._set_state(self._state_pending)
        self._transition_unsub = None
        self._state_pending = None



class MerossLanRollerShutter(_MerossEntity, CoverEntity):

    PLATFORM = PLATFORM_COVER

    def __init__(self, device: MerossDevice, id: object):
        super().__init__(device, id, DEVICE_CLASS_SHUTTER)
        self._payload = {mc.KEY_POSITION: {mc.KEY_POSITION: 0, mc.KEY_CHANNEL: id } }
        self._position = None


    @property
    def supported_features(self):
        """Flag supported features."""
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
        self._set_state(STATE_OPENING)
        self._payload[mc.KEY_POSITION][mc.KEY_POSITION] = 100
        self._device.request(
            namespace=mc.NS_APPLIANCE_ROLLERSHUTTER_POSITION,
            method=mc.METHOD_SET,
            payload=self._payload)


    async def async_close_cover(self, **kwargs) -> None:
        self._set_state(STATE_CLOSING)
        self._payload[mc.KEY_POSITION][mc.KEY_POSITION] = 0
        self._device.request(
            namespace=mc.NS_APPLIANCE_ROLLERSHUTTER_POSITION,
            method=mc.METHOD_SET,
            payload=self._payload)


    async def async_set_cover_position(self, **kwargs):
        if ATTR_POSITION in kwargs:
            newpos = kwargs[ATTR_POSITION]
            if self._position is not None:
                self._set_state(STATE_CLOSING if newpos < self._position else STATE_OPENING)
            self._payload[mc.KEY_POSITION][mc.KEY_POSITION] = newpos
            self._device.request(
                namespace=mc.NS_APPLIANCE_ROLLERSHUTTER_POSITION,
                method=mc.METHOD_SET,
                payload=self._payload)


    async def async_stop_cover(self, **kwargs):
        #self._set_state(STATE_CLOSING)
        self._payload[mc.KEY_POSITION][mc.KEY_POSITION] = -1
        self._device.request(
            namespace=mc.NS_APPLIANCE_ROLLERSHUTTER_POSITION,
            method=mc.METHOD_SET,
            payload=self._payload)


    def _set_unavailable(self) -> None:
        self._position = None
        super()._set_unavailable()


    def _set_rollerstate(self, state) -> None:
        if state == 1:
            self._set_state(STATE_CLOSING)
        elif state == 2:
            self._set_state(STATE_OPENING)


    def _set_rollerposition(self, position) -> None:
        self._position = position
        if position == 0:
            self._set_state(STATE_CLOSED)
        else:
            self._set_state(STATE_OPEN)