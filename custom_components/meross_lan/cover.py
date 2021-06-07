

from homeassistant.components.cover import (
    CoverEntity,
    DEVICE_CLASS_GARAGE, DEVICE_CLASS_SHUTTER,
    ATTR_POSITION,
    SUPPORT_OPEN, SUPPORT_CLOSE, SUPPORT_SET_POSITION, SUPPORT_STOP,
    STATE_OPEN, STATE_OPENING, STATE_CLOSED, STATE_CLOSING
)

from .merossclient import const as mc
from .meross_device import MerossDevice
from .meross_entity import _MerossEntity, platform_setup_entry, platform_unload_entry
from .const import (
    PLATFORM_COVER,
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


    @property
    def supported_features(self):
        """Flag supported features."""
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
        self._set_state(STATE_OPENING)
        self._payload[mc.KEY_STATE][mc.KEY_OPEN] = 1
        self._device.request(
            namespace=mc.NS_APPLIANCE_GARAGEDOOR_STATE,
            method=mc.METHOD_SET,
            payload=self._payload)
        return


    async def async_close_cover(self, **kwargs) -> None:
        self._set_state(STATE_CLOSING)
        self._payload[mc.KEY_STATE][mc.KEY_OPEN] = 0
        self._device.request(
            namespace=mc.NS_APPLIANCE_GARAGEDOOR_STATE,
            method=mc.METHOD_SET,
            payload=self._payload)
        return


    def _set_open(self, open) -> None:
        self._set_state(STATE_OPEN if open else STATE_CLOSED)
        return



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
        return


    async def async_close_cover(self, **kwargs) -> None:
        self._set_state(STATE_CLOSING)
        self._payload[mc.KEY_POSITION][mc.KEY_POSITION] = 0
        self._device.request(
            namespace=mc.NS_APPLIANCE_ROLLERSHUTTER_POSITION,
            method=mc.METHOD_SET,
            payload=self._payload)
        return


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

        return


    async def async_stop_cover(self, **kwargs):
        #self._set_state(STATE_CLOSING)
        self._payload[mc.KEY_POSITION][mc.KEY_POSITION] = -1
        self._device.request(
            namespace=mc.NS_APPLIANCE_ROLLERSHUTTER_POSITION,
            method=mc.METHOD_SET,
            payload=self._payload)
        return

    def _set_unavailable(self) -> None:
        self._position = None
        super()._set_unavailable()
        return

    def _set_rollerstate(self, state) -> None:
        if state == 1:
            self._set_state(STATE_CLOSING)
        elif state == 2:
            self._set_state(STATE_OPENING)
        return

    def _set_rollerposition(self, position) -> None:
        self._position = position
        if position == 0:
            self._set_state(STATE_CLOSED)
        else:
            self._set_state(STATE_OPEN)
        return