"""
 Base-Common behaviour for all Meross-LAN entities

 actual HA custom platform entities will be derived like this:
 MerossLanSwitch(_MerossToggle, SwitchEntity)
"""
from typing import Any, Callable, Dict, List, Optional


from homeassistant.helpers.typing import HomeAssistantType, StateType
from homeassistant.const import STATE_UNKNOWN, STATE_ON, STATE_OFF

from .logger import LOGGER
from .const import DOMAIN, CONF_DEVICE_ID, NS_APPLIANCE_CONTROL_ELECTRICITY, METHOD_GET

# pylint: disable=no-member

class _MerossEntity:
    def __init__(self, meross_device: object, channel: Optional[int], device_class: str):  # pylint: disable=unsubscriptable-object
        self._meross_device = meross_device
        self._channel = channel
        self._device_class = device_class
        self._state = None
        meross_device.entities[channel if channel is not None else device_class] = self

    def __del__(self):
        LOGGER.debug("MerossEntity(%s) destroy", self.unique_id)
        return

    """
    @abstractmethod
    @property
    def enabled(self):
        pass

    @abstractmethod
    def async_write_ha_state(self):
        pass
    """

    @property
    def unique_id(self):
        return f"{self._meross_device.device_id}_{self._channel}"

    # To link this entity to the  device, this property must return an
    # identifiers value matching that used in the cover, but no other information such
    # as name. If name is returned, this entity will then also become a device in the
    # HA UI.
    @property
    def device_info(self):
        return {
            "identifiers": {
                # Serial numbers are unique identifiers within a specific domain
                (DOMAIN, self._meross_device.device_id)
            }
        }

    @property
    def device_class(self):
        return self._device_class

    @property
    def should_poll(self) -> bool:
        return False

    @property
    def available(self) -> bool:
        """Return True if entity is available."""
        return self._state != None

    @property
    def assumed_state(self) -> bool:
        """Return true if we do optimistic updates."""
        return False

    @property
    def state(self) -> StateType:
        """Return the state of the entity."""
        return self._state

    async def async_added_to_hass(self) -> None:
        return

    async def async_will_remove_from_hass(self) -> None:
        return

    def _set_state(self, state: str) -> None:
        if self._state != state:
            self._state = state
            if self.hass and self.enabled:
                self.async_write_ha_state()
        return

    def _set_available(self) -> None:
        return

    def _set_unavailable(self) -> None:
        self._set_state(None)
        return


class _MerossToggle(_MerossEntity):
    def __init__(self, meross_device: object, channel: Optional[int], device_class: str, m_toggle_set, m_toggle_get):  # pylint: disable=unsubscriptable-object
        super().__init__(meross_device, channel, device_class)
        self._m_toggle_set = m_toggle_set
        self._m_toggle_get = m_toggle_get


    async def async_turn_on(self, **kwargs) -> None:
        return self._m_toggle_set(self._channel, 1)


    async def async_turn_off(self, **kwargs) -> None:
        return self._m_toggle_set(self._channel, 0)


    @property
    def is_on(self) -> bool:
        return self._state == STATE_ON


    def _set_onoff(self, onoff) -> None:
        self._set_state(STATE_ON if onoff else STATE_OFF)
        return
