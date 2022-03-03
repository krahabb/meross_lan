"""
 Base-Common behaviour for all Meross-LAN entities

 actual HA custom platform entities will be derived like this:
 MLSwitch(_MerossToggle, SwitchEntity)

 we also try to 'commonize' HA core symbols import in order to better manage
 versioning
"""
from __future__ import annotations

from functools import partial

from homeassistant.helpers.typing import StateType
from homeassistant.helpers import device_registry as dr
from homeassistant.const import (
    STATE_ON, STATE_OFF,
)
try:# 2022.2 new symbols
    from homeassistant.helpers.entity import EntityCategory
    ENTITY_CATEGORY_CONFIG = EntityCategory.CONFIG
    ENTITY_CATEGORY_DIAGNOSTIC = EntityCategory.DIAGNOSTIC
except:
    ENTITY_CATEGORY_CONFIG = 'config'
    ENTITY_CATEGORY_DIAGNOSTIC = 'diagnostic'


from .merossclient import const as mc, get_namespacekey, get_productnameuuid
from .helpers import LOGGER
from .const import CONF_DEVICE_ID, DOMAIN



class MerossFakeEntity:
    """
    a 'dummy' class we'll use as a placeholder to reduce optional and/or
    disabled entities access overhead
    """
    enabled = False



# pylint: disable=no-member
class _MerossEntity:

    PLATFORM: str

    _attr_state: StateType = None

    def __init__(
        self,
        device: "MerossDevice",
        channel: object,
        entitykey: str = None,
        device_class: str = None,
        subdevice: 'MerossSubDevice' = None
        ):
        self.device = device
        self.channel = channel
        self._attr_device_class = device_class
        self.subdevice = subdevice
        self.id = channel if entitykey is None else entitykey if channel is None else f"{channel}_{entitykey}"
        device.entities[self.id] = self
        async_add_devices = device.platforms.setdefault(self.PLATFORM)
        if async_add_devices is not None:
            async_add_devices([self])


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
        return f"{self.device.device_id}_{self.id}"


    @property
    def name(self) -> str:
        if (subdevice := self.subdevice) is not None:
            if self._attr_device_class is not None:
                return f"{subdevice.name} - {self._attr_device_class}"
            else:
                return subdevice.name
        if self._attr_device_class:
            return f"{self.device.descriptor.productname} - {self._attr_device_class}"
        return self.device.descriptor.productname


    @property
    def device_info(self):
        if (subdevice := self.subdevice) is not None:
            _id = subdevice.id
            _type = subdevice.type
            return {
                "via_device": (DOMAIN, self.device.device_id),
                "identifiers": {(DOMAIN, _id)},
                "manufacturer": mc.MANUFACTURER,
                "name": get_productnameuuid(_type, _id),
                "model": _type
                }
        _desc = self.device.descriptor
        return {
            "identifiers": {(DOMAIN, self.device.device_id)},
            "connections": {(dr.CONNECTION_NETWORK_MAC, _desc.macAddress)},
            "manufacturer": mc.MANUFACTURER,
            "name": _desc.productname,
            "model": _desc.productmodel,
            "sw_version": _desc.firmware.get(mc.KEY_VERSION)
            }


    @property
    def device_class(self) -> str | None:
        return self._attr_device_class

    """ moved to sensor to comply with HA development
    @property
    def unit_of_measurement(self) -> str | None:
        return self._attr_unit_of_measurement
    """

    @property
    def should_poll(self) -> bool:
        return False


    @property
    def available(self) -> bool:
        return self._attr_state is not None


    @property
    def assumed_state(self) -> bool:
        return False


    @property
    def state(self) -> StateType:
        return self._attr_state


    async def async_added_to_hass(self) -> None:
        return


    async def async_will_remove_from_hass(self) -> None:
        return


    def update_state(self, state: str):
        if self._attr_state != state:
            self._attr_state = state
            if self.hass and self.enabled:
                self.async_write_ha_state()


    def set_unavailable(self):
        self.update_state(None)


    @property
    def entryname(self) -> str: # ATTR friendly_name in HA api
        return (self.registry_entry.name if self.registry_entry is not None else None) or self.name


    """
    even though these are toggle/binary_sensor properties
    we provide a base-implement-all
    """
    @property
    def is_on(self) -> bool:
        return self._attr_state == STATE_ON


    def update_onoff(self, onoff) -> None:
        self.update_state(STATE_ON if onoff else STATE_OFF)



class _MerossToggle(_MerossEntity):


    def __init__(
        self,
        device: 'MerossDevice',
        channel: object,
        entitykey: str,
        device_class: str,
        namespace: str):
        super().__init__(device, channel, entitykey, device_class)
        self.namespace = namespace
        self.key = None if namespace is None else get_namespacekey(namespace)


    async def async_turn_on(self, **kwargs) -> None:
        self.request_onoff(1)


    async def async_turn_off(self, **kwargs) -> None:
        self.request_onoff(0)


    def request_onoff(self, onoff):
        # this is the meross executor code
        # override for switches not implemented
        # by a toggle like api
        def _ack_callback():
            self.update_onoff(onoff)

        self.device.request(
            self.namespace,
            mc.METHOD_SET,
            {self.key: {mc.KEY_CHANNEL: self.channel, mc.KEY_ONOFF: onoff}},
            _ack_callback)


    def _parse_toggle(self, payload: dict):
        self.update_onoff(payload.get(mc.KEY_ONOFF))


    def _parse_togglex(self, payload: dict):
        self.update_onoff(payload.get(mc.KEY_ONOFF))


"""
 helper functions to 'commonize' platform setup/unload
"""
def platform_setup_entry(hass: object, config_entry: object, async_add_devices, platform: str):
    device_id = config_entry.data[CONF_DEVICE_ID]
    device = hass.data[DOMAIN].devices[device_id]
    device.platforms[platform] = async_add_devices
    async_add_devices([entity for entity in device.entities.values() if entity.PLATFORM is platform])
    LOGGER.debug("async_setup_entry device_id = %s - platform = %s", device_id, platform)

def platform_unload_entry(hass: object, config_entry: object, platform: str) -> bool:
    device_id = config_entry.data[CONF_DEVICE_ID]
    device = hass.data[DOMAIN].devices[device_id]
    device.platforms[platform] = None
    LOGGER.debug("async_unload_entry device_id = %s - platform = %s", device_id, platform)
    return True
