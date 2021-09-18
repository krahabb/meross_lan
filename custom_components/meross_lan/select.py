from __future__ import annotations

from homeassistant.components.select import (
    DOMAIN as PLATFORM_SELECT,
    SelectEntity
)
from homeassistant.const import STATE_OFF, STATE_ON, STATE_UNKNOWN

from .merossclient import const as mc  # mEROSS cONST
from .meross_entity import _MerossEntity, platform_setup_entry, platform_unload_entry


async def async_setup_entry(hass: object, config_entry: object, async_add_devices):
    platform_setup_entry(hass, config_entry, async_add_devices, PLATFORM_SELECT)

async def async_unload_entry(hass: object, config_entry: object) -> bool:
    return platform_unload_entry(hass, config_entry, PLATFORM_SELECT)


OPTION_SPRAY_MODE_OFF = STATE_OFF
OPTION_SPRAY_MODE_CONTINUOUS = STATE_ON
OPTION_SPRAY_MODE_INTERMITTENT = 'intermittent'

OPTION_TO_SPRAY_MODE_MAP = {
    OPTION_SPRAY_MODE_OFF: mc.SPRAY_MODE_OFF,
    OPTION_SPRAY_MODE_CONTINUOUS: mc.SPRAY_MODE_CONTINUOUS,
    OPTION_SPRAY_MODE_INTERMITTENT: mc.SPRAY_MODE_INTERMITTENT
}

class MerossLanSpray(_MerossEntity, SelectEntity):

    PLATFORM = PLATFORM_SELECT

    _attr_options: list[str] = [
        OPTION_SPRAY_MODE_OFF,
        OPTION_SPRAY_MODE_CONTINUOUS,
        OPTION_SPRAY_MODE_INTERMITTENT
    ]

    _attr_current_option: str | None = None


    def __init__(self, device: 'MerossDevice', id: object):
        super().__init__(device, id, None)


    async def async_select_option(self, option: str) -> None:

        mode = OPTION_TO_SPRAY_MODE_MAP[option]

        def _ack_callback():
            self._set_mode(mode)

        self._device.request(
            mc.NS_APPLIANCE_CONTROL_SPRAY,
            mc.METHOD_SET,
            {mc.KEY_SPRAY: {mc.KEY_CHANNEL: self._id, mc.KEY_MODE: mode}},
            _ack_callback
        )


    def _set_mode(self, mode) -> None:
        try:
            self._attr_current_option = self._attr_options[mode]
            self.set_state(self._attr_current_option)
        except:
            self._attr_current_option = None
            self.set_state(STATE_UNKNOWN)