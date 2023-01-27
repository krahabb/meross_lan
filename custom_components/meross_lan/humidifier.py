from __future__ import annotations
import typing

try:
    from homeassistant.components.humidifier import (
        DOMAIN as PLATFORM_HUMIDIFIER,
        HumidifierDeviceClass,
        HumidifierEntity,
    )
    from homeassistant.components.humidifier.const import (
        HumidifierEntityFeature,
        MODE_ECO,
        MODE_NORMAL,
    )

    DEVICE_CLASS_HUMIDIFIER = HumidifierDeviceClass.HUMIDIFIER
    SUPPORT_MODES = HumidifierEntityFeature.MODES
except:
    from homeassistant.components.humidifier import (
        DOMAIN as PLATFORM_HUMIDIFIER,
        DEVICE_CLASS_HUMIDIFIER,
        HumidifierEntity,
    )
    from homeassistant.components.humidifier.const import (
        SUPPORT_MODES,
        MODE_ECO,
        MODE_NORMAL,
    )

from homeassistant.const import STATE_OFF, STATE_ON

from .merossclient import const as mc  # mEROSS cONST
from . import meross_entity as me

if typing.TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from homeassistant.config_entries import ConfigEntry
    from .meross_device import MerossDevice


async def async_setup_entry(
    hass: HomeAssistant, config_entry: ConfigEntry, async_add_devices
):
    me.platform_setup_entry(hass, config_entry, async_add_devices, PLATFORM_HUMIDIFIER)


async def async_unload_entry(hass: HomeAssistant, config_entry: ConfigEntry):
    return me.platform_unload_entry(hass, config_entry, PLATFORM_HUMIDIFIER)


MODE_CONTINUOUS = MODE_NORMAL
MODE_INTERMITTENT = MODE_ECO

MODE_TO_SPRAY_MODE_MAP = {
    MODE_CONTINUOUS: mc.SPRAY_MODE_CONTINUOUS,
    MODE_INTERMITTENT: mc.SPRAY_MODE_INTERMITTENT,
}

SPRAY_MODE_TO_MODE_MAP = {
    mc.SPRAY_MODE_CONTINUOUS: MODE_CONTINUOUS,
    mc.SPRAY_MODE_INTERMITTENT: MODE_INTERMITTENT,
}


class MerossLanSpray(me.MerossEntity, HumidifierEntity):

    PLATFORM = PLATFORM_HUMIDIFIER

    _attr_available_modes: list[str] = list(MODE_TO_SPRAY_MODE_MAP.keys())
    # _attr_max_humidity: int = DEFAULT_MAX_HUMIDITY
    # _attr_min_humidity: int = DEFAULT_MAX_HUMIDITY

    _spray_mode: int | None = None

    def __init__(self, device: 'MerossDevice', channel: object):
        super().__init__(device, channel, None, DEVICE_CLASS_HUMIDIFIER)

    @property
    def supported_features(self):
        return SUPPORT_MODES

    @property
    def mode(self):
        return SPRAY_MODE_TO_MODE_MAP.get(self._spray_mode)  # type: ignore

    async def async_turn_on(self, **kwargs):
        await self.async_request_spray(self._spray_mode or mc.SPRAY_MODE_CONTINUOUS)

    async def async_turn_off(self, **kwargs):
        await self.async_request_spray(mc.SPRAY_MODE_OFF)

    async def async_set_humidity(self, humidity: int):
        pass

    async def async_set_mode(self, mode: str):
        await self.async_request_spray(
            MODE_TO_SPRAY_MODE_MAP.get(mode, mc.SPRAY_MODE_CONTINUOUS)
        )

    async def async_request_spray(self, spray_mode: int):
        def _ack_callback(acknowledge: bool, header: dict, payload: dict):
            if acknowledge:
                self.update_mode(spray_mode)

        await self.device.async_request(
            mc.NS_APPLIANCE_CONTROL_SPRAY,
            mc.METHOD_SET,
            {mc.KEY_SPRAY: {mc.KEY_CHANNEL: self.channel, mc.KEY_MODE: spray_mode}},
            _ack_callback,
        )

    def update_mode(self, spray_mode: int | None):
        if spray_mode == mc.SPRAY_MODE_OFF:
            self.update_state(STATE_OFF)
        else:
            if (self._attr_state != STATE_ON) or (self._spray_mode != spray_mode):
                self._attr_state = STATE_ON
                self._spray_mode = spray_mode
                if self.hass and self.enabled:
                    self.async_write_ha_state()

    def _parse_spray(self, payload: dict):
        self.update_mode(payload.get(mc.KEY_MODE))
