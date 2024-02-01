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
from homeassistant.helpers.entity import Entity, EntityCategory
from homeassistant.helpers.typing import StateType

from .helpers import Loggable
from .helpers.manager import ApiProfile
from .merossclient import NAMESPACE_TO_KEY, const as mc

if typing.TYPE_CHECKING:
    from typing import ClassVar, Final
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

    from .helpers.manager import EntityManager
    from .helpers.namespaces import NamespaceHandler
    from .meross_device import MerossDeviceBase


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

    # HA core entity attributes:
    # These are constants throughout our model
    force_update: Final[bool] = False
    has_entity_name: Final[bool] = True
    should_poll: Final[bool] = False
    # These may be customized here and there per class or instance
    assumed_state: bool = False
    entity_category: EntityCategory | None = None
    extra_state_attributes: dict[str, object] = {}
    _attr_name: str | None = None
    translation_key: str | None = None
    # These are actually per instance
    device_class: Final[object | str | None]
    _attr_state: StateType
    unique_id: str

    # used to speed-up checks if entity is enabled and loaded
    _hass_connected: bool

    __slots__ = (
        "manager",
        "channel",
        "entitykey",
        "namespace_handlers",
        "device_class",
        "_attr_state",
        "unique_id",
        "_hass_connected",
    )

    def __init__(
        self,
        manager: EntityManager,
        channel: object | None,
        entitykey: str | None = None,
        device_class: object | str | None = None,
        *,
        state: StateType = None,
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
        id = (
            channel
            if entitykey is None
            else entitykey
            if channel is None
            else f"{channel}_{entitykey}"
        )
        assert (
            manager.entities.get(id) is None
        ), f"(channel:{channel}, entitykey:{entitykey}) is not unique inside manager.entities"
        self.manager = manager
        self.channel = channel
        self.entitykey = entitykey
        self.namespace_handlers: set[NamespaceHandler] = set()
        self.device_class = device_class
        Loggable.__init__(self, id, logger=manager)
        attr_name = self._attr_name
        if attr_name is None and (entitykey or device_class):
            attr_name = f"{entitykey or device_class}".capitalize()
        # when channel == 0 it might be the only one so skip it
        # when channel is already in device name it also may be skipped
        if channel and (channel is not manager.id):
            # (channel is manager.id) means this is the 'main' entity of an hub subdevice
            # so we skip adding the subdevice.id to the entity name
            attr_name = f"{attr_name} {channel}" if attr_name else str(channel)
        self._attr_name = attr_name
        self._attr_state = state
        self._hass_connected = False
        # by default all of our entities have unique_id so they're registered
        # there could be some exceptions though (MLUpdate)
        self.unique_id = manager.generate_unique_id(self)
        manager.entities[id] = self
        async_add_devices = manager.platforms.setdefault(self.PLATFORM)
        if async_add_devices:
            async_add_devices([self])

    # interface: Entity
    """REMOVE(attr)
    @property
    def assumed_state(self):
        return False
    """

    @property
    def available(self):
        # TODO: refactor available property to slotted attribute
        # by also adding a set_available method
        return self._attr_state is not None

    """REMOVE(attr)
    @property
    def device_class(self):
        return self._attr_device_class
    """

    @property
    def device_info(self):
        return self.manager.deviceentry_id

    """REMOVE(attr)
    @property
    def entity_category(self):
        return self._attr_entity_category
    """

    """REMOVE(attr)
    @property
    def extra_state_attributes(self):
        return self._attr_extra_state_attributes
    """
    """REMOVE(attr)
    @property
    def force_update(self):
        return False
    """
    """REMOVE(attr)
    @property
    def has_entity_name(self):
        return True
    """

    @property
    def name(self):
        # TODO: remove compatibility
        if CORE_HAS_ENTITY_NAME:
            # newer api...return just the 'local' name
            return self._attr_name
        # compatibility layer....
        if self._attr_name is not None:
            return f"{self.manager.name} - {self._attr_name}"
        return self.manager.name

    """REMOVE(attr)
    @property
    def should_poll(self):
        return False
    """
    """REMOVE(attr)
    @property
    def translation_key(self) -> str | None:
        return self._attr_translation_key
    """
    """REMOVE(attr)
    @property
    def unique_id(self):
        return self._attr_unique_id
    """

    async def async_added_to_hass(self):
        self.log(self.VERBOSE, "Added to HomeAssistant")
        self._hass_connected = True
        return await super().async_added_to_hass()

    async def async_will_remove_from_hass(self):
        self.log(self.VERBOSE, "Removed from HomeAssistant")
        self._hass_connected = False
        return await super().async_will_remove_from_hass()

    # interface: self
    @property
    def is_diagnostic(self):
        """Means this entity has been created as part of the 'create_diagnostic_entities' config"""
        return False

    async def async_shutdown(self):
        for handler in set(self.namespace_handlers):
            handler.unregister(self)
        self.manager.entities.pop(self.id)
        self.manager: EntityManager = None  # type: ignore

    def update_state(self, state: StateType):
        """
        update the entity state and flush it if changed
        HA already has a system to catch non-changing state updates
        but we'd prefer avoid all of the overhead if we're already
        sure it didn't change
        """
        if self._attr_state != state:
            self._attr_state = state
            self.flush_state()

    def flush_state(self):
        """
        actually commits a state change to HA. This is more
        hard-core than update_state since some entities (say climates)
        have multi-dimensional state with HA core attributes that
        need to be updated. For these use-cases update_state is too simple
        to catch so we just let HA process it
        """
        if self._hass_connected:
            self.async_write_ha_state()

    def set_unavailable(self):
        self.update_state(None)

    def _parse_undefined(self, payload):
        # this is a default handler for any message (in protocol routing)
        # for which we haven't defined a specific handler
        self.log(
            self.WARNING,
            "Handler undefined for payload:(%s)",
            str(payload),
            timeout=14400,
        )

    # even though these are toggle/binary_sensor properties
    # we provide a base-implement-all
    # TODO: move these to the specialized MerossToggle and BianrySensor
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
    namespace: str
    key_namespace: str
    key_channel: str = mc.KEY_CHANNEL
    key_onoff: str = mc.KEY_ONOFF

    def __init__(
        self,
        manager: MerossDeviceBase,
        channel: object,
        entitykey: str | None = None,
        device_class: object | None = None,
        *,
        state: StateType = None,
        namespace: str | None = None,
    ):
        super().__init__(manager, channel, entitykey, device_class, state=state)
        if namespace:
            self.namespace = namespace
            self.key_namespace = NAMESPACE_TO_KEY[namespace]

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
    manager = ApiProfile.managers[config_entry.entry_id]
    manager.log(manager.DEBUG, "platform_setup_entry { platform: %s }", platform)
    manager.platforms[platform] = async_add_devices
    async_add_devices(manager.managed_entities(platform))
