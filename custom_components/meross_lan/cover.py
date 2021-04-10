
from typing import Any, Callable, Dict, List, Optional

from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.typing import HomeAssistantType
#from homeassistant.components.switch import SwitchEntity, DEVICE_CLASS_OUTLET
from homeassistant.components.cover import (
    CoverEntity,
    DEVICE_CLASS_GARAGE,
    SUPPORT_OPEN, SUPPORT_CLOSE,
    STATE_OPEN, STATE_OPENING, STATE_CLOSED, STATE_CLOSING
)

from .const import (
    DOMAIN,
    CONF_DEVICE_ID,
    METHOD_SET, METHOD_GET,
    NS_APPLIANCE_GARAGEDOOR_STATE, NS_APPLIANCE_SYSTEM_ALL
)
from .meross_entity import _MerossEntity
from .logger import LOGGER

async def async_setup_entry(hass: HomeAssistantType, config_entry: ConfigEntry, async_add_devices):
    device_id = config_entry.data[CONF_DEVICE_ID]
    device = hass.data[DOMAIN].devices[device_id]
    async_add_devices([entity for entity in device.entities.values() if isinstance(entity, MerossLanCover)])
    LOGGER.debug("async_setup_entry device_id = %s - platform = cover", device_id)
    return

async def async_unload_entry(hass: object, config_entry: object) -> bool:
    LOGGER.debug("async_unload_entry device_id = %s - platform = cover", config_entry.data[CONF_DEVICE_ID])
    return True


class MerossLanCover(_MerossEntity, CoverEntity):
    def __init__(self, meross_device: object, channel: int):
        super().__init__(meross_device, channel, DEVICE_CLASS_GARAGE)
        meross_device.has_covers = True
        self._payload = {"state": {"channel": channel, "open": 0, "uuid": meross_device.device_id } }


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


    def open_cover(self, **kwargs: Any) -> None:
        self._set_state(STATE_OPENING)
        self._payload["state"]["open"] = 1
        self._meross_device.mqtt_publish(
            namespace=NS_APPLIANCE_GARAGEDOOR_STATE,
            method=METHOD_SET,
            payload=self._payload)
        return


    def close_cover(self, **kwargs: Any) -> None:
        self._set_state(STATE_CLOSING)
        self._payload["state"]["open"] = 0
        self._meross_device.mqtt_publish(
            namespace=NS_APPLIANCE_GARAGEDOOR_STATE,
            method=METHOD_SET,
            payload=self._payload)
        return


    def _set_open(self, open) -> None:
        self._set_state(STATE_OPEN if open else STATE_CLOSED)
        return