#import logging
from typing import Any, Callable, Dict, List, Optional

from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.typing import HomeAssistantType
from homeassistant.components.switch import SwitchEntity, DEVICE_CLASS_OUTLET

from .const import DOMAIN, CONF_DEVICE_ID


async def async_setup_entry(hass: HomeAssistantType, config_entry: ConfigEntry, async_add_devices):
    device_id = config_entry.data[CONF_DEVICE_ID]
    async_add_devices(hass.data[DOMAIN].devices[device_id].switches)
    return

class MerossLanSwitch(SwitchEntity):
    def __init__(self, meross_device: object, channel: int, m_toggle_set, m_toggle_get):
        self._meross_device = meross_device
        self._channel = channel
        self._is_on = None
        """ TAG_NOPOWERATTR
        disable attributes publishing to avoid unnecessary recording on switch entity
        power readings are now available as proper sensor entities
        this code will be removed once features are stabilized

        self._current_power_w = None
        self._current_voltage_v = None
        self._current_current_a = None
        self._today_energy_kwh = None
        """
        self._m_toggle_set = m_toggle_set
        self._m_toggle_get = m_toggle_get

    @property
    def unique_id(self) -> Optional[str]:
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
        return DEVICE_CLASS_OUTLET

    @property
    def should_poll(self) -> bool:
        return False

    @property
    def available(self) -> bool:
        """Return True if entity is available."""
        return self._is_on != None

    @property
    def assumed_state(self) -> bool:
        """Return true if we do optimistic updates."""
        return False

    @property
    def is_on(self) -> bool:
        """Return true if the switch is on."""
        return self._is_on

    """ TAG_NOPOWERATTR
    disable attributes publishing to avoid unnecessary recording on switch entity
    power readings are now available as proper sensor entities
    this code will be removed once features are stabilized

    @property
    def current_power_w(self):
        return self._current_power_w

    @property
    def today_energy_kwh(self):
        return self._today_energy_kwh

    @property
    def state_attributes(self):
        data = super().state_attributes
        if self._current_voltage_v is not None:
            data["current_voltage_v"] = self._current_voltage_v
        if self._current_current_a is not None:
            data["current_current_a"] = self._current_current_a
        return data
    """

    async def async_added_to_hass(self) -> None:
        self._m_toggle_get(self._channel)
        return

    async def async_will_remove_from_hass(self) -> None:
        self._is_on = None
        """ TAG_NOPOWERATTR
        self._current_current_a = None
        self._current_voltage_v = None
        self._current_power_w = None
        self._today_energy_kwh = None
        """
        return

    async def async_turn_on(self, **kwargs) -> None:
        return self._m_toggle_set(self._channel, 1)

    async def async_turn_off(self, **kwargs) -> None:
        return self._m_toggle_set(self._channel, 0)

    def _set_available(self) -> None:
        if self.enabled:
            self._m_toggle_get(self._channel)
        return

    def _set_unavailable(self) -> None:
        if self.enabled and self.available:
            self._is_on = None
            """ TAG_NOPOWERATTR
            self._current_current_a = None
            self._current_voltage_v = None
            self._current_power_w = None
            self._today_energy_kwh = None
            """
            self.async_write_ha_state()
        return

    def _set_is_on(self, is_on: Optional[bool]) -> None:
        if self.enabled:
            self._is_on = is_on
            self.async_write_ha_state()
        return

    """ TAG_NOPOWERATTR
    disable attributes publishing to avoid unnecessary recording on switch entity
    power readings are now available as proper sensor entities
    this code will be removed once features are stabilized

    def _set_power(self, power: float, voltage: float, current: float) -> None:
        if self.enabled:
            self._current_power_w = power
            self._current_voltage_v = voltage
            self._current_current_a = current
            self.async_write_ha_state()
        return

    def _set_energy(self, energy: float) -> None:
        if self.enabled:
            self._today_energy_kwh = energy
            self.async_write_ha_state()
        return
    """