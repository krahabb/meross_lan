from typing import Any, Callable, Dict, List, Optional

from homeassistant.helpers.entity import Entity

from .const import DOMAIN, CONF_DEVICE_ID
from .meross_entity import _MerossEntity
from .logger import LOGGER

async def async_setup_entry(hass: object, config_entry: object, async_add_devices):
    device_id = config_entry.data[CONF_DEVICE_ID]
    device = hass.data[DOMAIN].devices[device_id]
    async_add_devices([entity for entity in device.entities.values() if isinstance(entity, MerossLanSensor)])
    LOGGER.debug("async_setup_entry device_id = %s - platform = sensor", device_id)
    return

async def async_unload_entry(hass: object, config_entry: object) -> bool:
    LOGGER.debug("async_unload_entry device_id = %s - platform = sensor", config_entry.data[CONF_DEVICE_ID])
    return True

class MerossLanSensor(_MerossEntity, Entity):
    def __init__(self, meross_device: object, device_class: str, unit_of_measurement: str):
        super().__init__(meross_device, None, device_class)
        self._unit_of_measurement = unit_of_measurement
        meross_device.has_sensors = True

    @property
    def unique_id(self) -> Optional[str]:
        """Return a unique id identifying the entity."""
        return f"{self._meross_device.device_id}_{self.device_class}"

    @property
    def unit_of_measurement(self) -> Optional[str]:
        return self._unit_of_measurement

