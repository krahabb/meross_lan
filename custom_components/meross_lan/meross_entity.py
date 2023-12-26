"""
 Base-Common behaviour for all Meross-LAN entities

 actual HA custom platform entities will be derived like this:
 MLSwitch(MerossToggle, SwitchEntity)

 we also try to 'commonize' HA core symbols import in order to better manage
 versioning
"""
from __future__ import annotations

import typing

from homeassistant import const as hac
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.typing import StateType

from .helpers import LOGGER, ApiProfile, Loggable, StrEnum
from .merossclient import const as mc, get_namespacekey

if typing.TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

    from .helpers import EntityManager
    from .meross_device import MerossDeviceBase


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

    @staticmethod
    def update_state(state):
        pass


class MerossEntity(Loggable, Entity if typing.TYPE_CHECKING else object):
    """
    Mixin style base class for all of the entity platform(s)
    This class must prepend the HA entity class in our custom
    entity classe definitions like:
    from homeassistant.components.switch import Switch
    class MyCustomSwitch(MerossEntity, Switch)
    """

    PLATFORM: str

    EntityCategory = EntityCategory

    _attr_device_class: object | str | None
    _attr_entity_category: EntityCategory | str | None = None
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
        "manager",
        "channel",
        "_attr_device_class",
        "_attr_state",
        "_attr_unique_id",
        "_hass_connected",
    )

    def __init__(
        self,
        manager: EntityManager,
        channel: object | None,
        entitykey: str | None = None,
        device_class: object | str | None = None,
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
        assert channel is not None or (
            entitykey is not None
        ), "provide at least channel or entitykey (cannot be 'None' together)"

        _id = (
            channel
            if entitykey is None
            else entitykey
            if channel is None
            else f"{channel}_{entitykey}"
        )
        Loggable.__init__(self, _id, None, manager)
        assert (
            manager.entities.get(_id) is None
        ), f"(channel:{channel}, entitykey:{entitykey}) is not unique inside manager.entities"
        self.manager = manager
        self.channel = channel
        self._attr_device_class = device_class
        attr_name = self._attr_name
        if attr_name is None and (entitykey or device_class):
            attr_name = f"{entitykey or device_class}"
        # when channel == 0 it might be the only one so skip it
        # when channel is already in device name it also may be skipped
        if channel and manager.name.find(str(channel)) == -1:
            attr_name = f"{attr_name} {channel}" if attr_name else str(channel)
        if attr_name is not None:
            attr_name = attr_name.capitalize()
        self._attr_name = attr_name
        self._attr_state = None
        self._attr_unique_id = manager.generate_unique_id(self)
        self._hass_connected = False
        manager.entities[_id] = self
        async_add_devices = manager.platforms.setdefault(self.PLATFORM)
        if async_add_devices:
            async_add_devices([self])

    # interface: Entity
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
        return self.manager.deviceentry_id

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
        if self._attr_name is not None:
            return f"{self.manager.name} - {self._attr_name}"
        return self.manager.name

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
        self.logtag = f"{self.__class__.__name__}({self.entity_id})"
        self._hass_connected = True

    async def async_will_remove_from_hass(self):
        self._hass_connected = False

    # interface: self
    async def async_shutdown(self):
        pass

    def update_state(self, state: StateType):
        if self._attr_state != state:
            self._attr_state = state
            if self._hass_connected:
                self._async_write_ha_state()

    def set_unavailable(self):
        self.update_state(None)

    def _parse_undefined(self, payload):
        # this is a default handler for any message (in protocol routing)
        # for which we haven't defined a specific handler (see MerossDevice._parse__generic)
        self.warning("handler undefined for payload:(%s)", str(payload), timeout=14400)

    # even though these are toggle/binary_sensor properties
    # we provide a base-implement-all
    STATE_ON: typing.Final = hac.STATE_ON
    STATE_OFF: typing.Final = hac.STATE_OFF

    @property
    def is_on(self):
        return self._attr_state == self.STATE_ON

    def update_onoff(self, onoff):
        self.update_state(self.STATE_ON if onoff else self.STATE_OFF)


class MerossToggle(MerossEntity):
    """
    Base toggle-like behavior used as a base class for
    effective switches or the likes (light for example)
    """

    manager: MerossDeviceBase
    # customize the request payload for different
    # devices api. see 'request_onoff' to see how
    namespace: str | None
    key_namespace: str | None
    key_channel: str | None = mc.KEY_CHANNEL
    key_onoff: str | None = mc.KEY_ONOFF

    def __init__(
        self,
        manager: MerossDeviceBase,
        channel: object,
        entitykey: str | None,
        device_class: object | None,
        namespace: str | None = None,
    ):
        super().__init__(manager, channel, entitykey, device_class)
        if namespace:
            self.namespace = namespace
            self.key_namespace = get_namespacekey(namespace)

    async def async_turn_on(self, **kwargs):
        await self.async_request_onoff(1)

    async def async_turn_off(self, **kwargs):
        await self.async_request_onoff(0)

    async def async_request_onoff(self, onoff: int):
        assert self.namespace

        # this is the meross executor code
        # override for switches not implemented
        # by a toggle like api
        if await self.manager.async_request_ack(
            self.namespace,
            mc.METHOD_SET,
            {
                self.key_namespace: {
                    self.key_channel: self.channel,
                    self.key_onoff: onoff,
                }
            },
        ):
            self.update_onoff(onoff)

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
    LOGGER.debug(
        "platform_setup_entry { unique_id: %s, platform: %s }",
        config_entry.unique_id,
        platform,
    )
    manager = ApiProfile.managers[config_entry.entry_id]
    manager.platforms[platform] = async_add_devices
    async_add_devices(manager.managed_entities(platform))
