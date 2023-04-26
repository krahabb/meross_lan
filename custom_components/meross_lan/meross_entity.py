"""
 Base-Common behaviour for all Meross-LAN entities

 actual HA custom platform entities will be derived like this:
 MLSwitch(MerossToggle, SwitchEntity)

 we also try to 'commonize' HA core symbols import in order to better manage
 versioning
"""
from __future__ import annotations

import typing

from homeassistant.const import STATE_OFF, STATE_ON
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.typing import StateType

from .helpers import LOGGER, ApiProfile, Loggable, StrEnum
from .merossclient import const as mc, get_namespacekey

if typing.TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

    from .meross_device import MerossDevice
    from .meross_device_hub import MerossSubDevice


try:  # 2022.2 new symbols
    from homeassistant.helpers.entity import EntityCategory  # type: ignore
except Exception:

    class EntityCategory(StrEnum):
        CONFIG = "config"
        DIAGNOSTIC = "diagnostic"


CORE_HAS_ENTITY_NAME = hasattr(Entity, "has_entity_name")


class MerossFakeEntity:
    """
    a 'dummy' class we'll use as a placeholder to reduce optional and/or
    disabled entities access overhead
    """

    enabled = False


class MerossEntity(Loggable, Entity if typing.TYPE_CHECKING else object):
    PLATFORM: str

    _attr_device_class: object | str | None
    _attr_entity_category: EntityCategory | str | None
    # provides a class empty default since the state writing api
    # would create an empty anyway....
    _attr_extra_state_attributes: dict[str, object] = {}
    _attr_name: str | None = None
    _attr_state: StateType
    _attr_translation_key: str | None = None
    _attr_unique_id: str

    # used to speed-up checks if entity is enabled and loaded
    _hass_connected: bool

    __slots__ = (
        "id",
        "device",
        "channel",
        "subdevice",
        "_attr_device_class",
        "_attr_entity_category",
        "_attr_state",
        "_attr_unique_id",
        "_hass_connected",
    )

    def __init__(
        self,
        device: MerossDevice,
        channel: object | None,
        entitykey: str | None = None,
        device_class: object | str | None = None,
        subdevice: MerossSubDevice | None = None,
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
        assert (channel is not None) or (
            entitykey is not None
        ), "provide at least channel or entitykey (cannot be 'None' together)"
        self.id = _id = (
            channel
            if entitykey is None
            else entitykey
            if channel is None
            else f"{channel}_{entitykey}"
        )
        assert (
            device.entities.get(_id) is None
        ), "(channel, entitykey) is not unique inside device.entities"
        self.device = device
        self.channel = channel
        self.subdevice = subdevice
        self._attr_device_class = device_class
        self._attr_entity_category = None
        if self._attr_name is None:
            self._attr_name = entitykey or device_class  # type: ignore
        self._attr_state = None
        self._attr_unique_id = f"{device.id}_{_id}"
        self._hass_connected = False
        device.entities[_id] = self
        async_add_devices = device.platforms.setdefault(self.PLATFORM)
        if async_add_devices is not None:
            async_add_devices([self])

    def __del__(self):
        LOGGER.debug("MerossEntity(%s): destroy", self.unique_id)

    def log(self, level: int, msg: str, *args, **kwargs):
        self.device.log(
            level, f"{self.__class__.__name__}({self.entity_id}) {msg}", *args, **kwargs
        )

    def warning(self, msg: str, *args, **kwargs):
        self.device.warning(
            f"{self.__class__.__name__}({self.entity_id}) {msg}", *args, **kwargs
        )

    @property
    def assumed_state(self):
        return False

    @property
    def available(self):
        return self._attr_state is not None

    @property
    def device_class(self):
        return self._attr_device_class

    @property
    def device_info(self):
        # device is already registered/updated at the
        # device/subdevice level so we just pass identifiers
        # to reduce overload
        if (subdevice := self.subdevice) is not None:
            return subdevice.deviceentry_id
        return self.device.deviceentry_id

    @property
    def entity_category(self):
        return self._attr_entity_category

    @property
    def extra_state_attributes(self):
        return self._attr_extra_state_attributes

    @property
    def force_update(self):
        return False

    @property
    def has_entity_name(self):
        return True

    @property
    def name(self):
        if CORE_HAS_ENTITY_NAME:
            # newer api...return just the 'local' name
            return self._attr_name
        # compatibility layer....
        if (subdevice := self.subdevice) is None:
            devname = self.device.name
        else:
            devname = subdevice.name
        if self._attr_name is not None:
            return f"{devname} - {self._attr_name}"
        return devname

    @property
    def should_poll(self):
        return False

    @property
    def translation_key(self) -> str | None:
        return self._attr_translation_key

    @property
    def unique_id(self):
        return self._attr_unique_id

    async def async_added_to_hass(self):
        self._hass_connected = True

    async def async_will_remove_from_hass(self):
        self._hass_connected = False

    def update_state(self, state: StateType):
        if self._attr_state != state:
            self._attr_state = state
            if self._hass_connected:
                # optimize hass checks since we're (pretty)
                # sure they're ok (DANGER)
                self._async_write_ha_state()

    def set_unavailable(self):
        self.update_state(None)

    # @property
    # def entryname(self): # ATTR friendly_name in HA api
    #    return (
    #        self.registry_entry.name if self.registry_entry is not None else None) or self.name

    def _parse_undefined(self, payload):
        # this is a default handler for any message (in protocol routing)
        # for which we haven't defined a specific handler (see MerossDevice._parse__generic)
        pass

    # even though these are toggle/binary_sensor properties
    # we provide a base-implement-all
    @property
    def is_on(self):
        return self._attr_state == STATE_ON

    def update_onoff(self, onoff):
        self.update_state(STATE_ON if onoff else STATE_OFF)


class MerossToggle(MerossEntity):
    """
    Base toggle-like behavior used as a base class for
    effective switches or the likes (light for example)
    """

    # customize the request payload for different
    # devices api. see 'request_onoff' to see how
    namespace: str | None
    key_namespace: str | None
    key_channel: str | None = mc.KEY_CHANNEL
    key_onoff: str | None = mc.KEY_ONOFF

    def __init__(
        self,
        device: "MerossDevice",
        channel: object,
        entitykey: str | None,
        device_class: object | None,
        subdevice: "MerossSubDevice" | None,
        namespace: str | None,
    ):
        super().__init__(device, channel, entitykey, device_class, subdevice)
        if namespace is not None:
            self.namespace = namespace
            self.key_namespace = get_namespacekey(namespace)

    async def async_turn_on(self, **kwargs):
        await self.async_request_onoff(1)

    async def async_turn_off(self, **kwargs):
        await self.async_request_onoff(0)

    async def async_request_onoff(self, onoff: int):
        assert (
            self.namespace is not None
        ), "either set a nemaspace or override MerossToggle.async_request_onoff"

        # this is the meross executor code
        # override for switches not implemented
        # by a toggle like api
        def _ack_callback(acknowledge: bool, header: dict, payload: dict):
            if acknowledge:
                self.update_onoff(onoff)

        await self.device.async_request(
            self.namespace,
            mc.METHOD_SET,
            {
                self.key_namespace: {
                    self.key_channel: self.channel,
                    self.key_onoff: onoff,
                }
            },
            _ack_callback,
        )

    def _parse_toggle(self, payload: dict):
        self.update_onoff(payload.get(self.key_onoff))

    def _parse_togglex(self, payload: dict):
        self.update_onoff(payload.get(self.key_onoff))


#
# helper functions to 'commonize' platform setup
#
def platform_setup_entry(
    hass: HomeAssistant, config_entry: ConfigEntry, async_add_devices, platform: str
):
    device: MerossDevice = ApiProfile.devices[config_entry.unique_id]  # type: ignore
    device.platforms[platform] = async_add_devices
    async_add_devices(
        [entity for entity in device.entities.values() if entity.PLATFORM is platform]
    )
    LOGGER.debug(
        "async_setup_entry device_id = %s - platform = %s", device.id, platform
    )
