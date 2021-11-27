from __future__ import annotations

from homeassistant.components.select import (
    DOMAIN as PLATFORM_SELECT,
    SelectEntity
)

from .merossclient import const as mc  # mEROSS cONST
from .meross_entity import _MerossEntity, platform_setup_entry, platform_unload_entry


async def async_setup_entry(hass: object, config_entry: object, async_add_devices):
    platform_setup_entry(hass, config_entry, async_add_devices, PLATFORM_SELECT)

async def async_unload_entry(hass: object, config_entry: object) -> bool:
    return platform_unload_entry(hass, config_entry, PLATFORM_SELECT)


from homeassistant.const import (
    STATE_OFF as OPTION_SPRAY_MODE_OFF,
    STATE_ON as OPTION_SPRAY_MODE_CONTINUOUS,
    STATE_UNKNOWN
)
try:
    from homeassistant.components.humidifier.const import MODE_ECO as OPTION_SPRAY_MODE_INTERMITTENT
except:
    OPTION_SPRAY_MODE_INTERMITTENT = 'intermittent'

OPTION_TO_SPRAY_MODE_MAP = {
    OPTION_SPRAY_MODE_OFF: mc.SPRAY_MODE_OFF,
    OPTION_SPRAY_MODE_CONTINUOUS: mc.SPRAY_MODE_CONTINUOUS,
    OPTION_SPRAY_MODE_INTERMITTENT: mc.SPRAY_MODE_INTERMITTENT
}

"""
    This code is an alternative implementation for SPRAY/humidifier
    since the meross SPRAY doesnt support target humidity and
    the 'semantics' for HA humidifier are a bit odd for this device
"""
class MerossLanSpray(_MerossEntity, SelectEntity):

    PLATFORM = PLATFORM_SELECT

    _attr_options: list[str] = [
        OPTION_SPRAY_MODE_OFF,
        OPTION_SPRAY_MODE_CONTINUOUS,
        OPTION_SPRAY_MODE_INTERMITTENT
    ]

    _attr_current_option: str | None = None


    def __init__(self, device: 'MerossDevice', _id: object):
        super().__init__(device, _id, None)


    async def async_select_option(self, option: str) -> None:

        spray_mode = OPTION_TO_SPRAY_MODE_MAP[option]

        def _ack_callback():
            self.update_mode(spray_mode)


        self.device.request(
            mc.NS_APPLIANCE_CONTROL_SPRAY,
            mc.METHOD_SET,
            {mc.KEY_SPRAY: {mc.KEY_CHANNEL: self.id, mc.KEY_MODE: spray_mode}},
            _ack_callback
        )


    def update_mode(self, spray_mode: int) -> None:
        try:
            self._attr_current_option = self._attr_options[spray_mode]
            self.update_state(self._attr_current_option)
        except:
            self._attr_current_option = None
            self.update_state(STATE_UNKNOWN)
