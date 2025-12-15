"""
Base-Common behaviour for all Meross-LAN entities

actual HA custom platform entities will be derived like this:
MLSwitch(MerossToggle, SwitchEntity)

we also try to 'commonize' HA core symbols import in order to better manage
versioning
"""

from functools import partial
from typing import TYPE_CHECKING, final

try:
    from homeassistant.components.recorder import get_instance as r_get_instance
    from homeassistant.components.recorder.history import get_last_state_changes
except ImportError:
    get_last_state_changes = None

from homeassistant.helpers import entity

from . import Loggable
from .namespaces import NamespaceParser, mc, mn

if TYPE_CHECKING:
    from typing import (
        Any,
        Callable,
        ClassVar,
        Final,
        Mapping,
        NotRequired,
        TypedDict,
        Unpack,
    )

    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

    from .device import BaseDevice
    from .manager import ConfigEntryManager, EntityManager


#
# helper function to 'commonize' platform setup
#
def platform_setup_entry(
    hass: "HomeAssistant",
    config_entry: "ConfigEntry[ConfigEntryManager]",
    async_add_devices,
    platform: str,
):
    manager = config_entry.runtime_data
    manager.log(manager.DEBUG, "platform_setup_entry { platform: %s }", platform)
    manager.platforms[platform] = async_add_devices
    async_add_devices(manager.managed_entities(platform))


class MLEntity(NamespaceParser, Loggable, entity.Entity if TYPE_CHECKING else object):
    """
    Mixin style base class for all of the entity platform(s)
    This class must prepend the HA entity class in our custom
    entity classe definitions like:
    from homeassistant.components.switch import Switch
    class MyCustomSwitch(MLEntity, Switch)
    """

    if TYPE_CHECKING:

        type StateCallback = Callable[[], Any]

        class Args(TypedDict):
            name: NotRequired[str]
            translation_key: NotRequired[str]
            entity_category: NotRequired[entity.EntityCategory | None]
            state_callback: NotRequired["MLEntity.StateCallback"]

        EntityCategory: Final

        PLATFORM: ClassVar[str]

        is_diagnostic: ClassVar[bool]
        """Tells if this entity has been created as part of the 'create_diagnostic_entities' config"""

        state_callbacks: set[StateCallback] | None
        # These 'placeholder' definitions support generalization of
        # Meross protocol message build/parsing when related to the
        # current entity. These are usually relevant when this entity
        # is strictly related to a namespace payload key value.
        # See MLConfigNumber or MerossToggle as basic implementations
        # supporting this semantic. They're generally set as class definitions
        # in inherited entities but could nonetheless be set 'per instance'.
        # These also come handy when generalizing parsing of received payloads
        # for simple enough entities (like sensors, numbers or switches)
        # Starting around 2025 some namespaces seems to enrich their payloads structure
        # by nesting the actual 'key_value' inside 'group_key(s)' (see Appliance.Config.DeviceCfg)
        ns: mn.Namespace  # no default
        key_group: str  # no default
        key_value: str  # defaulted to 'value'

        # used to speed-up checks if entity is enabled and loaded
        hass_connected: Final[bool]  # public ReadOnly attribute

        # HA core entity attributes:
        # These are constants throughout our model
        force_update: Final[bool]
        has_entity_name: Final[bool]
        should_poll: Final[bool]
        # These may be customized here and there per class
        _attr_available: ClassVar[bool]
        # These may be customized here and there per class or instance
        assumed_state: bool = False
        entity_category: entity.EntityCategory | None
        entity_registry_enabled_default: bool
        extra_state_attributes: dict[str, object]
        icon: str | None
        translation_key: str | None
        # These are actually per instance
        available: bool
        name: str | None
        suggested_object_id: str | None
        unique_id: str

    EntityCategory = entity.EntityCategory

    is_diagnostic = False

    key_value = mc.KEY_VALUE

    # HA core entity attributes:
    force_update = False
    has_entity_name = True
    should_poll = False
    _attr_available = False
    assumed_state = False
    entity_category = None
    entity_registry_enabled_default = True
    extra_state_attributes = {}
    icon = None
    translation_key = None

    __slots__ = (
        # slotting also base Entity frequently used attributes...
        "entity_id",
        "hass",
        "platform",
        "registry_entry",
        "device_entry",
        "_context",
        "_context_set",
        # meross_lan managed attributes
        "manager",
        "channel",
        "entitykey",
        "state_callbacks",
        "hass_connected",
        # HA core
        "available",
        "device_class",
        "device_info",
        "name",
        "suggested_object_id",
        "unique_id",
    )

    def __init__(
        self,
        manager: "EntityManager",
        channel: object | None,
        entitykey: str | None = None,
        device_class: str | None = None,
        /,
        **kwargs: "Unpack[Args]",
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
        # init these first since Loggable init could call configure_logger which 'sometimes'
        # could rely on these
        self.manager = manager
        self.channel = channel
        self.entitykey = entitykey
        id = (
            channel
            if entitykey is None
            else entitykey if channel is None else f"{channel}_{entitykey}"
        )
        Loggable.__init__(self, id, logger=manager)
        # init before raising exceptions so that the Loggable is
        # setup before any exception is raised
        if id is None:
            raise AssertionError(
                "provide at least channel or entitykey (cannot be 'None' together)"
            )
        if id in manager.entities:
            raise AssertionError(f"id:{id} is not unique inside manager.entities")

        if "state_callback" in kwargs:
            self.state_callbacks = set()
            self.state_callbacks.add(kwargs.pop("state_callback"))
        else:
            self.state_callbacks = None
        self.hass_connected = False

        self.entity_id = entity.Entity.entity_id
        self.hass = entity.Entity.hass
        self.platform = entity.Entity.platform
        self.registry_entry = None
        self.device_entry = None

        self._context = None
        self._context_set = None
        self.available = self._attr_available or manager.online
        self.device_class = device_class
        self.device_info = self.manager.deviceentry_id  # type: ignore

        if "name" in kwargs:
            name = kwargs.pop("name")
        elif entitykey:
            name = entitykey.replace("_", " ").capitalize()
        elif device_class:
            name = str(device_class).capitalize()
        else:
            name = None
        # when channel == 0 it might be the only one so skip it
        # when channel is already in device name it also may be skipped
        if channel and (channel is not manager.id):
            # (channel is manager.id) means this is the 'main' entity of an hub subdevice
            # so we skip adding the subdevice.id to the entity name
            name = f"{name} {channel}" if name else str(channel)
        self.suggested_object_id = self.name = name

        # by default all of our entities have unique_id so they're registered
        # there could be some exceptions though (MLUpdate)
        self.unique_id = self._generate_unique_id()
        for _attr_name, _attr_value in kwargs.items():
            setattr(self, _attr_name, _attr_value)

        manager.entities[id] = self
        async_add_devices = manager.platforms.setdefault(self.PLATFORM)
        if async_add_devices:
            async_add_devices([self])

    # interface: Entity
    async def async_added_to_hass(self):
        self.log(self.VERBOSE, "Added to HomeAssistant")
        self.hass_connected = True  # type: ignore
        return await super().async_added_to_hass()

    async def async_will_remove_from_hass(self):
        self.log(self.VERBOSE, "Removed from HomeAssistant")
        self.hass_connected = False  # type: ignore
        return await super().async_will_remove_from_hass()

    # interface: self
    async def async_shutdown(self):
        await NamespaceParser.async_shutdown(self)
        self.state_callbacks = None
        self.manager.entities.pop(self.id)
        self.manager: "EntityManager" = None  # type: ignore

    @final
    def register_state_callback(self, state_callback: "StateCallback", /):
        if not self.state_callbacks:
            self.state_callbacks = set()
        self.state_callbacks.add(state_callback)

    def flush_state(self):
        """Actually commits a state change to HA."""
        if self.state_callbacks:
            for state_callback in self.state_callbacks:
                state_callback()
        if self.hass_connected:
            self.async_write_ha_state()

    def set_available(self):
        self.available = True
        # we don't flush here since we'll wait for actual device readings

    def set_unavailable(self):
        self.available = False
        self.flush_state()

    def update_device_value(self, device_value, /) -> bool | None:
        """This is a stub definition. It will be called by _parse (when namespace dispatching
        is configured so) or directly as a short path inside other parsers to forward the
        incoming device value to the underlyinh HA entity state."""
        raise NotImplementedError("Called 'update_device_value' on wrong entity type")

    def update_native_value(self, native_value, /) -> bool | None:
        """This is a stub definition. It will usually be called by update_device_value
        with the result of the conversion from the incoming device value (from Meross protocol)
        to the proper HA type/value for the entity class."""
        raise NotImplementedError("Called 'update_native_value' on wrong entity type")

    async def async_request_value(self, device_value, /):
        """Sends the actual request to the device. This needs to be overloaded in entities
        actually supporting the method SET on their namespace. Since the syntax for the payload
        is almost generalized we have some defaults implementations based on mixins ready to be
        included in actual entity implementation
        """
        raise NotImplementedError("Called 'async_request_value' on wrong entity type")

    async def get_last_state_available(self):
        """
        Recover the last known good state from recorder in order to
        restore transient state information when restarting HA.
        If the device/entity was disconnected before restarting and we need
        the last good reading from the device, we need to skip the last
        state since it is 'unavailable'
        """

        if not get_last_state_changes:
            raise Exception("Cannot find history.get_last_state_changes api")

        _last_state = await r_get_instance(self.hass).async_add_executor_job(
            partial(
                get_last_state_changes,
                self.hass,
                2,
                self.entity_id,
            )
        )
        if states := _last_state.get(self.entity_id):
            for state in reversed(states):
                if state.state not in (
                    MLEntity.hac.STATE_UNKNOWN,
                    MLEntity.hac.STATE_UNAVAILABLE,
                ):
                    return state
        return None

    def _generate_unique_id(self):
        return self.manager.generate_unique_id(self)

    # interface: NamespaceParser
    def _parse(self, payload: "Mapping[str, Any]", /):
        """Default parsing for entities. Set the proper
        key_group/key_value in class/instance definition to make it work."""
        try:
            # statistically more common case
            self.update_device_value(payload[self.key_value])
        except KeyError:
            self.update_device_value(payload[self.key_group][self.key_value])


class MENoChannelMixin(MLEntity if TYPE_CHECKING else object):
    """
    Implementation for protocol method 'SET' on entities/namespaces not backed by a channel.
    Actual examples: Appliance.Control.Toggle, Appliance.GarageDoor.Config, and so on..
    """

    manager: "BaseDevice"

    # interface: MLEntity
    async def async_request_value(self, device_value, /):
        """sends the actual request to the device. this is likely to be overloaded"""
        ns = self.ns
        return await self.manager.async_request_ack(
            ns.name,
            mc.METHOD_SET,
            {ns.key: {self.key_value: device_value}},
        )


class MEDictChannelMixin(MLEntity if TYPE_CHECKING else object):
    """
    Implementation for protocol method 'SET' on entities/namespaces backed by a channel
    where the command payload must be enclosed in a plain dict (without enclosing list).
    Actual examples: Appliance.Control.ToggleX, Appliance.RollerShutter.Config, and so on..
    """

    manager: "BaseDevice"

    # interface: MLEntity
    async def async_request_value(self, device_value, /):
        """sends the actual request to the device. this is likely to be overloaded"""
        ns = self.ns
        return await self.manager.async_request_ack(
            ns.name,
            mc.METHOD_SET,
            {
                ns.key: {
                    ns.key_channel: self.channel,
                    self.key_value: device_value,
                }
            },
        )


class MEListChannelMixin(MLEntity if TYPE_CHECKING else object):
    """
    Implementation for protocol method 'SET' on entities/namespaces backed by a channel
    where the command payload must be enclosed in a list
    Actual examples: Appliance.Control.ToggleX and so on..
    """

    manager: "BaseDevice"

    # interface: MLEntity
    async def async_request_value(self, device_value, /):
        """sends the actual request to the device. this is likely to be overloaded"""
        ns = self.ns
        return await self.manager.async_request_ack(
            ns.name,
            mc.METHOD_SET,
            {ns.key: [{ns.key_channel: self.channel, self.key_value: device_value}]},
        )


class MEGroupListChannelMixin(MLEntity if TYPE_CHECKING else object):
    """
    Implementation for protocol method 'SET' on entities/namespaces backed by a channel
    list and the actual entity value is embedded in a 'group' key (see Appliance.Config.DeviceCfg).
    """

    manager: "BaseDevice"

    # interface: MLEntity
    async def async_request_value(self, device_value, /):
        """sends the actual request to the device. this is likely to be overloaded"""
        ns = self.ns
        return await self.manager.async_request_ack(
            ns.name,
            mc.METHOD_SET,
            {
                ns.key: [
                    {
                        ns.key_channel: self.channel,
                        self.key_group: {self.key_value: device_value},
                    }
                ]
            },
        )


class MEAutoChannelMixin(MLEntity if TYPE_CHECKING else object):
    """
    Implementation for protocol method 'SET' on entities/namespaces backed by a channel
    where the command payload could be either a list or a dict. This mixin actually
    tries to learn the correct format at runtime buy 'sensing' it
    Actual examples: None.
    """

    if TYPE_CHECKING:
        manager: "BaseDevice"
        _set_format: type | None

    _set_format = None

    # interface: MLEntity
    async def async_request_value(self, device_value, /):
        """sends the actual request to the device. this is likely to be overloaded"""
        ns = self.ns
        if self._set_format is None:
            # check if the list format works first
            if response_set := await self.manager.async_request_ack(
                ns.name,
                mc.METHOD_SET,
                {
                    ns.key: [
                        {ns.key_channel: self.channel, self.key_value: device_value}
                    ]
                },
            ):
                if response_get := await self.manager.async_request_ack(
                    ns.name,
                    mc.METHOD_GET,
                    {ns.key: [{ns.key_channel: self.channel}]},
                ):
                    if response_get[ns.key][0][self.key_value] == device_value:
                        self._set_format = list
                        return response_set
            # something didnt work: try with dict format
            if response_set := await self.manager.async_request_ack(
                ns.name,
                mc.METHOD_SET,
                {ns.key: {ns.key_channel: self.channel, self.key_value: device_value}},
            ):
                # even if dict was used we assume response to be in list format
                if response_get := await self.manager.async_request_ack(
                    ns.name,
                    mc.METHOD_GET,
                    {ns.key: [{ns.key_channel: self.channel}]},
                ):
                    if response_get[ns.key][0][self.key_value] == device_value:
                        self._set_format = dict
            return response_set
        elif self._set_format is list:
            return await self.manager.async_request_ack(
                ns.name,
                mc.METHOD_SET,
                {
                    ns.key: [
                        {ns.key_channel: self.channel, self.key_value: device_value}
                    ]
                },
            )
        else:
            return await self.manager.async_request_ack(
                ns.name,
                mc.METHOD_SET,
                {ns.key: {ns.key_channel: self.channel, self.key_value: device_value}},
            )


class MEAlwaysAvailableMixin(MLEntity if TYPE_CHECKING else object):
    """
    Mixin class for entities which should always be available
    disregarding current device connection state.
    """

    # HA core entity attributes:
    _attr_available = True

    def set_available(self):
        pass

    def set_unavailable(self):
        pass


class MEPartialAvailableMixin(MLEntity if TYPE_CHECKING else object):
    """
    Mixin class for entities which should be available when device is connected
    but their state needs to be preserved since they're representing a state not directly
    carried by the device ('emulated' configuration params like MLEmulatedNumber or so).
    """

    def set_available(self):
        self.available = True
        self.flush_state()

    def set_unavailable(self):
        self.available = False
        self.flush_state()


class MLBinaryEntity(MLEntity):
    """Partially abstract common base class for ToggleEntity and BinarySensor.
    The initializer is skipped."""

    if TYPE_CHECKING:

        class Args(MLEntity.Args):
            device_value: NotRequired[Any]

        # HA core entity attributes:
        is_on: bool | None

    key_value = mc.KEY_ONOFF

    __slots__ = ("is_on",)

    def __init__(
        self,
        manager: "BaseDevice",
        channel: object,
        entitykey: str | None = None,
        device_class: str | None = None,
        /,
        **kwargs: "Unpack[Args]",
    ):
        self.is_on = kwargs.pop("device_value", None)
        super().__init__(manager, channel, entitykey, device_class, **kwargs)

    def set_unavailable(self):
        self.is_on = None
        super().set_unavailable()

    def update_onoff(self, onoff):
        if self.is_on != onoff:
            self.is_on = onoff
            self.flush_state()

    def _parse(self, payload: dict):
        """Default parsing for toggles and binary sensors. Set the proper
        key_value in class/instance definition to make it work."""
        self.update_onoff(payload[self.key_value])


class MLNumericEntity(MLEntity):
    """Common base class for (numeric) sensors and numbers."""

    if TYPE_CHECKING:

        class Args(MLEntity.Args):
            device_value: NotRequired[int | float]
            device_scale: NotRequired[int | float]
            native_unit_of_measurement: NotRequired[str]

        DEVICECLASS_TO_UNIT_MAP: ClassVar[dict[Any | None, str | None]]

        _attr_device_scale: ClassVar[int | float]
        device_scale: int | float
        device_value: int | float | None
        """The 'native' device value carried in protocol messages."""

        # HA core entity attributes:
        native_value: int | float | None
        native_unit_of_measurement: str | None

    """To be init in derived classes with their DeviceClass own types."""
    _attr_device_scale = 1

    __slots__ = (
        "device_scale",
        "device_value",
        "native_value",
        "native_unit_of_measurement",
    )

    def __init__(
        self,
        manager: "EntityManager",
        channel: object,
        entitykey: str | None = None,
        device_class: str | None = None,
        /,
        **kwargs: "Unpack[Args]",
    ):
        self.device_scale = kwargs.pop("device_scale", self._attr_device_scale)
        if "device_value" in kwargs:
            self.device_value = kwargs.pop("device_value")
            self.native_value = self.device_value / self.device_scale
        else:
            self.device_value = None
            self.native_value = None
        self.native_unit_of_measurement = kwargs.pop(
            "native_unit_of_measurement", None
        ) or self.DEVICECLASS_TO_UNIT_MAP.get(device_class)
        super().__init__(manager, channel, entitykey, device_class, **kwargs)

    def set_unavailable(self):
        self.device_value = None
        self.native_value = None
        super().set_unavailable()

    def update_device_value(self, device_value: int | float, /):
        if self.device_value != device_value:
            self.device_value = device_value
            self.native_value = device_value / self.device_scale
            self.flush_state()
            return True

    def update_native_value(self, native_value: int | float | None, /):
        if self.native_value != native_value:
            self.native_value = native_value
            self.flush_state()
            return True
