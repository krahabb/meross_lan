from __future__ import annotations

from homeassistant.components.humidifier import (
    DOMAIN as PLATFORM_HUMIDIFIER,
    DEVICE_CLASS_HUMIDIFIER,
    HumidifierEntity
)
from homeassistant.components.humidifier.const import SUPPORT_MODES, MODE_ECO, MODE_NORMAL
from homeassistant.const import STATE_OFF, STATE_ON

from .merossclient import const as mc  # mEROSS cONST
from .meross_entity import _MerossEntity, platform_setup_entry, platform_unload_entry


async def async_setup_entry(hass: object, config_entry: object, async_add_devices):
    platform_setup_entry(hass, config_entry, async_add_devices, PLATFORM_HUMIDIFIER)

async def async_unload_entry(hass: object, config_entry: object) -> bool:
    return platform_unload_entry(hass, config_entry, PLATFORM_HUMIDIFIER)


MODE_CONTINUOUS = MODE_NORMAL
MODE_INTERMITTENT = MODE_ECO

MODE_TO_SPRAY_MODE_MAP = {
    MODE_CONTINUOUS: mc.SPRAY_MODE_CONTINUOUS,
    MODE_INTERMITTENT: mc.SPRAY_MODE_INTERMITTENT
}

SPRAY_MODE_TO_MODE_MAP = {
    mc.SPRAY_MODE_CONTINUOUS: MODE_CONTINUOUS,
    mc.SPRAY_MODE_INTERMITTENT: MODE_INTERMITTENT
}

class MerossLanSpray(_MerossEntity, HumidifierEntity):

    PLATFORM = PLATFORM_HUMIDIFIER

    _attr_available_modes: list[str] = list(MODE_TO_SPRAY_MODE_MAP.keys())
    #_attr_max_humidity: int = DEFAULT_MAX_HUMIDITY
    #_attr_min_humidity: int = DEFAULT_MAX_HUMIDITY

    _spray_mode: int | None = None


    def __init__(self, device: 'MerossDevice', id: object):
        super().__init__(device, id, DEVICE_CLASS_HUMIDIFIER)


    @property
    def supported_features(self) -> int | None:
        return SUPPORT_MODES


    @property
    def mode(self) -> str | None:
        return SPRAY_MODE_TO_MODE_MAP.get(self._spray_mode)


    async def async_turn_on(self, **kwargs) -> None:
        self._internal_set_mode(self._spray_mode or mc.SPRAY_MODE_CONTINUOUS)


    async def async_turn_off(self, **kwargs) -> None:
        self._internal_set_mode(mc.SPRAY_MODE_OFF)


    async def async_set_humidity(self, humidity: int) -> None:
        pass


    async def async_set_mode(self, mode: str) -> None:
        self._internal_set_mode(MODE_TO_SPRAY_MODE_MAP.get(mode, mc.SPRAY_MODE_CONTINUOUS))


    def _internal_set_mode(self, spray_mode: int) -> None:
        def _ack_callback():
            self.update_mode(spray_mode)

        self.device.request(
            mc.NS_APPLIANCE_CONTROL_SPRAY,
            mc.METHOD_SET,
            {mc.KEY_SPRAY: {mc.KEY_CHANNEL: self.id, mc.KEY_MODE: spray_mode}},
            _ack_callback
        )


    def update_mode(self, spray_mode: int) -> None:
        if spray_mode == mc.SPRAY_MODE_OFF:
            self.update_state(STATE_OFF)
        else:
            if (self._attr_state != STATE_ON) or (self._spray_mode != spray_mode):
                self._attr_state = STATE_ON
                self._spray_mode = spray_mode
                if self.hass and self.enabled:
                    self.async_write_ha_state()
