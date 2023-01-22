"""
 Base-Common behaviour for all Meross-LAN entities

 actual HA custom platform entities will be derived like this:
 MLSwitch(_MerossToggle, SwitchEntity)

 we also try to 'commonize' HA core symbols import in order to better manage
 versioning
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.helpers.typing import StateType
from homeassistant.helpers.device_registry import CONNECTION_NETWORK_MAC
from homeassistant.helpers.entity import Entity
from homeassistant.const import (
    STATE_ON, STATE_OFF,
)
try:# 2022.2 new symbols
    from homeassistant.helpers.entity import EntityCategory
except:
    class EntityCategory:
        CONFIG = "config"
        DIAGNOSTIC = "diagnostic"

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
class _MerossEntity(Entity if TYPE_CHECKING else object):

    PLATFORM: str

    _attr_state: StateType = None
    _attr_device_class: str | None
    _attr_name: str | None = None
    _attr_entity_category: EntityCategory | None = None

    def __init__(
        self,
        device: "MerossDevice",
        channel: object,
        entitykey: str = None,
        device_class: str = None,
        subdevice: 'MerossSubDevice' = None
        ):
        """
        - channel: historically used to create an unique id for this entity inside the device
        and also related to the physical channel used in various api for some kind of entities.
        For entities in subdevices (hub paired devices) the channel is usually the Id of the
        subdevice itself since 'HA wise' and 'meross_lan wise' we still group the entities under
        the same (hub) device
        - entitykey: is added to provide additional 'uniqueness' should the device have multiple
        entities for the same channel and usually equal to device_class (but might not be)
        - device_class: used by HA to set some soft 'class properties' for the entity
        """
        self.device = device
        self.channel = channel
        self._attr_device_class = device_class
        if self._attr_name is None:
            self._attr_name = entitykey or device_class
        self.subdevice = subdevice
        self.id = channel if entitykey is None else entitykey if channel is None else f"{channel}_{entitykey}"
        assert (self.id is not None) and (device.entities.get(self.id) is None),\
        "provide a unique (channel, entitykey) in order to correctly identify this entity inside device.entities"
        device.entities[self.id] = self
        async_add_devices = device.platforms.setdefault(self.PLATFORM)
        if async_add_devices is not None:
            async_add_devices([self])


    def __del__(self):
        LOGGER.debug("MerossEntity(%s) destroy", self.unique_id)
        return


    @property
    def unique_id(self):
        return f"{self.device.device_id}_{self.id}"


    @property
    def has_entity_name(self) -> bool:
        return True


    @property
    def name(self) -> str:
        if hasattr(Entity, "has_entity_name"):
            # newer api...return just the 'local' name
            return self._attr_name
        # compatibility layer....
        if (subdevice := self.subdevice) is not None:
            if self._attr_name is not None:
                return f"{subdevice.name} - {self._attr_name}"
            else:
                return subdevice.name
        if self._attr_name is not None:
            return f"{self.device.descriptor.productname} - {self._attr_name}"
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
            "connections": {(CONNECTION_NETWORK_MAC, _desc.macAddress)},
            "manufacturer": mc.MANUFACTURER,
            "name": _desc.productname,
            "model": _desc.productmodel,
            "sw_version": _desc.firmware.get(mc.KEY_VERSION)
            }


    @property
    def device_class(self) -> str | None:
        return self._attr_device_class


    @property
    def entity_category(self) -> EntityCategory | None:
        return self._attr_entity_category


    @property
    def should_poll(self) -> bool:
        return False


    @property
    def available(self) -> bool:
        return self._attr_state is not None


    @property
    def assumed_state(self) -> bool:
        return False


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


    def _parse_undefined(self, payload):
        # this is a default handler for any message (in protocol routing)
        # for which we haven't defined a specific handler (see MerossDevice._parse__generic)
        pass


    # even though these are toggle/binary_sensor properties
    # we provide a base-implement-all

    @property
    def is_on(self) -> bool:
        return self._attr_state == STATE_ON


    def update_onoff(self, onoff) -> None:
        self.update_state(STATE_ON if onoff else STATE_OFF)



class _MerossToggle(_MerossEntity):

    # customize the request payload for differen
    # devices api. see 'request_onoff' to see how
    namespace: str
    key_namespace: str
    key_channel: str = mc.KEY_CHANNEL
    key_onoff: str = mc.KEY_ONOFF

    def __init__(
        self,
        device: 'MerossDevice',
        channel: object,
        entitykey: str,
        device_class: str,
        subdevice: 'MerossSubDevice',
        namespace: str,
        ):
        super().__init__(device, channel, entitykey, device_class, subdevice)
        self.namespace = namespace
        self.key_namespace = None if namespace is None else get_namespacekey(namespace)


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
            {self.key_namespace: {self.key_channel: self.channel, self.key_onoff: onoff}},
            _ack_callback)


    def _parse_toggle(self, payload: dict):
        self.update_onoff(payload.get(self.key_onoff))


    def _parse_togglex(self, payload: dict):
        self.update_onoff(payload.get(self.key_onoff))


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
