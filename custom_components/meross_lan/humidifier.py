from __future__ import annotations

import typing

from homeassistant.components import humidifier

try:
    from homeassistant.components.humidifier import HumidifierDeviceClass
    from homeassistant.components.humidifier.const import (
        MODE_ECO,
        MODE_NORMAL,
        HumidifierEntityFeature,
    )

    DEVICE_CLASS_HUMIDIFIER = HumidifierDeviceClass.HUMIDIFIER
    SUPPORT_MODES = HumidifierEntityFeature.MODES
except Exception:
    from homeassistant.components.humidifier import (
        DEVICE_CLASS_HUMIDIFIER,
    )
    from homeassistant.components.humidifier.const import (
        SUPPORT_MODES,
        MODE_ECO,
        MODE_NORMAL,
    )

from . import meross_entity as me
from .merossclient import const as mc  # mEROSS cONST

if typing.TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

    from .meross_device import MerossDevice


async def async_setup_entry(
    hass: HomeAssistant, config_entry: ConfigEntry, async_add_devices
):
    me.platform_setup_entry(hass, config_entry, async_add_devices, humidifier.DOMAIN)


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


class MerossLanSpray(me.MerossEntity, humidifier.HumidifierEntity):
    PLATFORM = humidifier.DOMAIN

    manager: MerossDevice
    _attr_available_modes: list[str] = list(MODE_TO_SPRAY_MODE_MAP.keys())
    # _attr_max_humidity: int = DEFAULT_MAX_HUMIDITY
    # _attr_min_humidity: int = DEFAULT_MAX_HUMIDITY

    _spray_mode: int | None = None

    def __init__(self, manager: MerossDevice, channel: object):
        super().__init__(manager, channel, None, DEVICE_CLASS_HUMIDIFIER)

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
        if await self.manager.async_request_ack(
            mc.NS_APPLIANCE_CONTROL_SPRAY,
            mc.METHOD_SET,
            {mc.KEY_SPRAY: {mc.KEY_CHANNEL: self.channel, mc.KEY_MODE: spray_mode}},
        ):
            self.update_mode(spray_mode)

    def update_mode(self, spray_mode: int | None):
        if spray_mode == mc.SPRAY_MODE_OFF:
            self.update_state(self.STATE_OFF)
        else:
            if (self._attr_state != self.STATE_ON) or (self._spray_mode != spray_mode):
                self._attr_state = self.STATE_ON
                self._spray_mode = spray_mode
                if self._hass_connected:
                    self._async_write_ha_state()

    def _parse_spray(self, payload: dict):
        self.update_mode(payload.get(mc.KEY_MODE))
