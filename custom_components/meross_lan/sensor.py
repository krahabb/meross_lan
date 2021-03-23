from typing import Any, Callable, Dict, List, Optional

from homeassistant.helpers.entity import Entity

from .const import DOMAIN, CONF_DEVICE_ID


async def async_setup_entry(hass: object, config_entry: object, async_add_devices):
    device_id = config_entry.data[CONF_DEVICE_ID]
    async_add_devices(hass.data[DOMAIN].devices[device_id].sensors)
    return


class MerossLanSensor(Entity):
    def __init__(
        self, meross_device: object, device_class: str, unit_of_measurement: str
    ):
        self._meross_device = meross_device
        self._device_class = device_class
        self._unit_of_measurement = unit_of_measurement
        self._state = None

    @property
    def unique_id(self) -> Optional[str]:
        """Return a unique id identifying the entity."""
        return f"{self._meross_device.device_id}_{self.device_class}"

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
    def unit_of_measurement(self) -> Optional[str]:
        return self._unit_of_measurement

    @property
    def state(self):
        return self._state

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

    async def async_added_to_hass(self) -> None:
        # self._m_toggle_get(self._channel)
        return

    async def async_will_remove_from_hass(self) -> None:
        self._state = None
        return

    def _set_available(self) -> None:
        # if self.enabled:
        # self._m_toggle_get(self._channel)
        return

    def _set_unavailable(self) -> None:
        if self.enabled and self.available:
            self._state = None
            self.async_write_ha_state()
        return

    def _set_state(self, state: str) -> None:
        if self.enabled:
            self._state = state
            self.async_write_ha_state()
        return

    """
    def _set_is_on(self, is_on: Optional[bool]) -> None:
        if self.enabled:
            self._is_on = is_on
            self.async_write_ha_state()
        return

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
