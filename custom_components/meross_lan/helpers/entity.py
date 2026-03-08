"""
Base-Common behaviour for all Meross-LAN entities
We also try to 'commonize' HA core symbols import in order to better manage
versioning
"""

from functools import cached_property, partial
from typing import TYPE_CHECKING, final, override

try:
    from homeassistant.components.recorder import get_instance as r_get_instance
    from homeassistant.components.recorder.history import get_last_state_changes
except ImportError:
    get_last_state_changes = None

from homeassistant.helpers import entity

from ..merossclient.device import parser
from ..merossclient.logging import Loggable
from ..merossclient.protocol import const as mc, namespaces as mn
from .namespaces import NamespaceHandler

if TYPE_CHECKING:
    from typing import (
        Any,
        Callable,
        ClassVar,
        Final,
        Iterable,
        Literal,
        NotRequired,
        Self,
        TypedDict,
        Unpack,
    )

    from homeassistant.config_entries import ConfigEntry
    from homeassistant.helpers.device_registry import DeviceEntry

    from ..merossclient.protocol.message import MerossMessage
    from ..merossclient.protocol.types import JsonDict, JsonMapping, PayloadIndexType
    from .device import BaseDevice, Device
    from .manager import ConfigEntryManager, EntityManager

    type ChannelType = PayloadIndexType


class Entity(Loggable, entity.Entity if TYPE_CHECKING else object):
    """
    Mixin style base class for all of the entity platform(s)
    This class must prepend the HA entity class in our custom
    entity class definitions.
    """

    if TYPE_CHECKING:

        type StateCallback = Callable[[], Any]

        class Args(Loggable.Args):
            entity_key: NotRequired[str | None]
            name: NotRequired[str | None]
            translation_key: NotRequired[str]
            device_class: NotRequired[str | None]
            device_entry: NotRequired[DeviceEntry | None]
            entity_category: NotRequired[entity.EntityCategory | None]
            entity_registry_enabled_default: NotRequired[bool]

        EntityCategory: Final

        PLATFORM: ClassVar[str]
        ENTITY_KEY: ClassVar[str | None]

        is_diagnostic: ClassVar[bool]  # TODO: type uppercase
        """Tells if this entity has been created as part of the 'create_diagnostic_entities' config"""

        parent: Final[EntityManager]  # type: ignore[override]
        channel: (
            ChannelType | None
        )  # TODO: maybe remove since it might only be relevant in ParserEntity
        entitykey: Final[str | None]
        # used to speed-up checks if entity is enabled and loaded
        hass_connected: Final[bool]  # public ReadOnly attribute

        # HA core entity attributes:
        # These are constants throughout our model
        # TODO: migrate to compliancy with HA core standards
        force_update: Final[bool]
        _attr_has_entity_name: Final[Literal[True]]
        should_poll: Final[bool]
        # These may be customized here and there per class
        _attr_available: ClassVar[bool]
        _attr_entity_registry_enabled_default: ClassVar[bool]
        _attr_device_class: ClassVar[str | None]
        _attr_name: ClassVar[str | None]
        # These may be customized here and there per class or instance
        assumed_state: bool = False
        entity_category: entity.EntityCategory | None
        extra_state_attributes: dict[str, Any]
        icon: str | None
        translation_key: str | None
        # These are actually per instance
        available: bool
        device_entry: DeviceEntry | None
        entity_registry_enabled_default: bool
        name: str | None

    EntityCategory = entity.EntityCategory

    ENTITY_KEY = None
    is_diagnostic = False

    # HA core entity attributes:
    force_update = False
    _attr_has_entity_name = True
    should_poll = False
    _attr_available = False  # TODO: review the availability mechanics
    _attr_device_class = None
    _attr_entity_registry_enabled_default = True
    assumed_state = False
    entity_category = None
    extra_state_attributes = {}
    icon = None
    translation_key = None

    __slots__ = Loggable._calc_slots(
        # meross_lan managed attributes
        "entitykey",
        "hass_connected",
        # HA core
        "available",
        "device_class",
        "device_entry",
        "entity_registry_enabled_default",
        "has_entity_name",
    )

    def __init__(
        self,
        channel: "ChannelType | None",
        manager: "EntityManager",
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
        if type(channel) is mn.Namespace:
            # TODO: ugly trick...let's see if this can be 'linearized' through some future refactoring.
            # this is a special case for 'EntityNamespaceMixin' entities which are also NamespaceHandlers
            # and so they get initialized with the namespace as channel because of constructor layout
            # and general coding in our inheritance scheme.
            # In this case we set the channel to None and store the namespace in a dedicated
            # variable for later use in parsing and so on.
            id = channel  # ns
            channel = None
            entitykey = self.__class__.ENTITY_KEY
            assert "entity_key" not in kwargs
        else:
            entitykey = kwargs.pop("entity_key", self.__class__.ENTITY_KEY)
            id = (
                channel
                if entitykey is None
                else entitykey if channel is None else f"{channel}_{entitykey}"
            )
            assert (
                id is not None
            ), "provide at least channel or entitykey (cannot be 'None' together)"
        assert (
            id not in manager.entities
        ), f"id:{id} is not unique inside parent.entities"
        super().__init__(id, manager)
        self.channel = channel
        self.ns_payload = mn.EMPTY_DICT
        self.device_value = kwargs.pop("device_value", None)
        self.entitykey = entitykey
        self.hass_connected = False

        self.available = self._attr_available or manager.is_connected
        self.device_class = kwargs.pop("device_class", self._attr_device_class)
        self.device_entry = kwargs.pop(
            "device_entry", None
        ) or manager.get_device_entry(channel)
        self.entity_registry_enabled_default = kwargs.pop(
            "entity_registry_enabled_default",
            self._attr_entity_registry_enabled_default,
        )

        # TODO: entity naming is slowly migrating to a more comfortable
        # HA core Entity class semantics/mechanics in order to
        # gain translation capabilities
        self.has_entity_name = self._attr_has_entity_name
        try:
            self.name = kwargs.pop("name") or self._attr_name
        except (KeyError, AttributeError):
            if entitykey:
                self.name = entitykey.replace("_", " ").capitalize()
            else:
                # as it is now implemented this will instruct HA core
                # to use device name when it can't provide an entity name
                self.use_device_name = True

        # some attributes can be set via kwargs
        for _attr_name, _attr_value in kwargs.items():
            setattr(self, _attr_name, _attr_value)

        manager.entities[id] = self
        manager.async_shutdown_broadcast.add(self.async_shutdown)
        try:
            manager.platforms[self.PLATFORM]([self])  # type: ignore
        except KeyError:
            manager.platforms[self.PLATFORM] = None
        except TypeError:
            pass  # platform setup not yet done

    def shutdown(self):
        super().shutdown()
        self.parent.async_shutdown_broadcast.remove(self.async_shutdown)
        try:
            del self.flush_state  # remove any possible state callback registration
        except AttributeError:
            pass
        del self.parent.entities[self.id]

    # interface: entity.Entity
    @cached_property
    def unique_id(self) -> str | None:
        return self.parent.generate_unique_id(self)

    async def async_added_to_hass(self):
        self.log(self.VERBOSE, "Added to HomeAssistant")
        self.hass_connected = True  # type: ignore
        return await super().async_added_to_hass()

    async def async_will_remove_from_hass(self):
        self.log(self.VERBOSE, "Removed from HomeAssistant")
        self.hass_connected = False  # type: ignore
        return await super().async_will_remove_from_hass()

    # interface: self
    @final
    def register_state_callback(self, state_callback: "StateCallback", /):
        """Registers a callback to be called when flush_state is called."""

        old_flush = self.flush_state

        def _wrapper():
            old_flush()
            state_callback()

        self.flush_state = _wrapper

    def flush_state(self):
        """Actually commits a state change to HA."""
        if self.hass_connected:
            self.async_write_ha_state()

    def schedule_flush_state(self, delay: float = 0):
        """Schedules a state change to HA after a delay."""
        if self.hass_connected:
            self.schedule_callback(delay, self.flush_state)
        else:
            self.cancel_callback(self.flush_state)

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
                    entity.STATE_UNKNOWN,
                    entity.STATE_UNAVAILABLE,
                ):
                    return state
        return None

    def set_available(self):
        if self._attr_available:
            return  # this entity is always available, no need to flush
        self.available = True
        self.flush_state()

    def set_unavailable(self):
        if self._attr_available:
            return  # this entity is always available, no need to flush
        self.available = False
        self.flush_state()

    def update_device_value(self, device_value, /) -> bool | None:
        raise NotImplementedError(
            "update_device_value must be implemented by subclasses if used in parsing scheme"
        )

    @staticmethod
    def platform_setup_entry(
        hass,
        config_entry: "ConfigEntry[ConfigEntryManager]",
        async_add_devices,
        platform: str,
    ):
        manager = config_entry.runtime_data
        manager.log(manager.DEBUG, "platform_setup_entry { platform: %s }", platform)
        manager.platforms[platform] = async_add_devices
        async_add_devices(manager.managed_entities(platform))

    class EntityDef[_T: Entity]:
        """Descriptor class used when populating maps used to dynamically instantiate (sensor)
        entities based on their appearance in a payload key."""

        type: "Final[type[_T]]"
        kwargs: "Final[Any]"

        __slots__ = ("type", "kwargs")

        def __init__(self, type: "type[_T]", **kwargs: "Unpack[Entity.Args]"):
            self.type = type
            self.kwargs = kwargs

    @classmethod
    def ENTITY_DEF(
        cls,
        **kwargs: "Unpack[Args]",
    ) -> "Entity.EntityDef[Self]":
        return Entity.EntityDef["Self"](cls, **kwargs)


class ParserEntity(parser.NamespaceParser, Entity):
    """Base class for entities directly linked to a device and not to a namespace.
    This is actually not used that much since most of the entities are linked to namespaces but it can be useful
    for some 'general' entities like 'DeviceInfo' or so."""

    if TYPE_CHECKING:
        parent: Final[BaseDevice]  # type: ignore[override]
        handler_ns: NamespaceHandler  # override

        class Args(Entity.Args):
            device_value: NotRequired[Any]

        NS_CHANNELS: ClassVar[tuple[int, ...] | None]
        """
        This is related to NamespaceHandler registration. For entity classes where we know
        the ns exposes fixed channel layouts (i.e. PhysicalLock) which are not exposed in any digest key
        we can set this to (0,) or more funny presets so that namespace initialization will also
        automatically build the needed entity(ies).
        Setting to None means 'scan digests for channels'.
        This is actually not mandatory though since only used for NamespaceHandler.register_entity_class.
        """
        NS_CHANNELS_SINGLE: Final[tuple[int, ...]]
        """Preset singleton for entities to be configured with a single channel in 0."""

        _parse_togglex: Callable[[JsonDict], Any]

    NamespaceValue = parser.NamespaceValue
    NamespaceGroupValue = parser.NamespaceGroupValue

    NS_CHANNELS = None  # scan digests for channels
    NS_CHANNELS_SINGLE = (0,)

    # __slots__ = parser.NamespaceParser._calc_slots()

    # TODO: add constructor with register_parser_entity ?

    @override
    def set_available(self):
        self.available = True
        # we don't flush here since we'll wait for actual device readings

    @override
    def set_unavailable(self):
        self.available = False
        self.ns_payload = mn.EMPTY_DICT
        self.flush_state()

    @classmethod
    def namespace_init(cls, ns: mn.Namespace, device: "Device", /):
        """Helper to register a specialized entity class to the proper namespace.
        This is going to be used on Device initialization fo various entities sharing
        common semantics in namespace parsing/handling."""
        assert ns is cls.ns
        NamespaceHandler(ns, device).register_entity_class(cls, cls.NS_CHANNELS)


class ValueParser(parser.NamespaceValue, ParserEntity):
    """Specialization for 'simple' parser entities where the HA entity state is a function
    of a single data point in the json ns payload. This provides a common implementation
    for setting the value (using parser.NamespaceValue.async_request_value) and for parsing the
    value from the payload (using parser.NamespaceValue.update_device_value).
    Examples of such entities are sensors/numbers, binary_sensors/switches."""

    if TYPE_CHECKING:

        class Args(ParserEntity.Args):
            key_value: NotRequired[str]
            device_value: NotRequired[Any]

    def __init__(
        self,
        channel: "ChannelType | None",
        parent: "BaseDevice",
        /,
        **kwargs: "Unpack[Args]",
    ):
        try:
            # set instance attribute if provided else this should fallback to class attribute
            self.key_value = kwargs["key_value"]  # type: ignore
        except KeyError:
            pass
        self.device_value = kwargs.pop("device_value", None)
        super().__init__(channel, parent, **kwargs)

    def set_unavailable(self):
        self.device_value = None
        super().set_unavailable()

    @override
    def update_device_value(self, device_value, /) -> bool | None:
        if super().update_device_value(device_value):
            self.flush_state()


class NumericEntity(Entity):
    """Common base class for sensors and numbers."""

    if TYPE_CHECKING:

        class Args(Entity.Args):
            native_value: NotRequired[int | float]
            native_unit_of_measurement: NotRequired[str]

        DEVICECLASS_TO_UNIT_MAP: ClassVar[dict[Any | None, str | None]]

        # HA core entity attributes:
        _attr_native_unit_of_measurement: ClassVar[str | None]
        native_value: int | float | None
        native_unit_of_measurement: str | None

    _attr_native_unit_of_measurement = None

    __slots__ = (
        "native_value",
        "native_unit_of_measurement",
    )

    def __init__(
        self,
        channel: "ChannelType | None",
        parent: "EntityManager",
        /,
        **kwargs: "Unpack[Args]",
    ):
        self.native_value = kwargs.get("native_value", None)
        try:
            self.native_unit_of_measurement = kwargs["native_unit_of_measurement"]  # type: ignore
        except KeyError:
            self.native_unit_of_measurement = (
                self._attr_native_unit_of_measurement
                or self.DEVICECLASS_TO_UNIT_MAP.get(
                    kwargs.get("device_class", self._attr_device_class)
                )
            )
        super().__init__(channel, parent, **kwargs)

    def update_native_value(self, native_value: int | float | None, /):
        if self.native_value != native_value:
            self.native_value = native_value
            self.flush_state()
            return True


class NumericParser(ValueParser, NumericEntity):
    if TYPE_CHECKING:

        class Args(ValueParser.Args, NumericEntity.Args):
            device_scale: NotRequired[int | float]

        _attr_device_scale: ClassVar[int | float]
        device_scale: int | float
        device_value: int | float | None

    _attr_device_scale = 1

    def __init__(
        self,
        channel: "ChannelType | None",
        parent: "BaseDevice",
        /,
        **kwargs: "Unpack[Args]",
    ):
        self.device_scale = kwargs.pop("device_scale", self._attr_device_scale)
        try:
            kwargs["native_value"] = kwargs["device_value"] / self.device_scale  # type: ignore
        except KeyError:
            pass
        super().__init__(channel, parent, **kwargs)

    def set_unavailable(self):
        # likely useless override...
        self.native_value = None
        super().set_unavailable()

    @override
    def update_device_value(self, device_value: int | float, /):
        if self.device_value != device_value:
            self.device_value = device_value
            self.native_value = device_value / self.device_scale
            self.flush_state()
            return True


class BinaryEntity(Entity):
    """Base class for HA binary entities (binary_sensors/toggle_entities)."""

    if TYPE_CHECKING:

        class Args(Entity.Args):
            is_on: NotRequired[Any]

        is_on: Any

    __slots__ = ("is_on",)

    def __init__(
        self,
        channel: "ChannelType | None",
        manager: "EntityManager",
        /,
        **kwargs: "Unpack[Args]",
    ):
        self.is_on = kwargs.pop("is_on", None)
        super().__init__(channel, manager, **kwargs)

    def update_boolean_value(self, is_on: "Any", /) -> bool | None:
        if self.is_on != is_on:
            self.is_on = is_on
            self.flush_state()
            return True


class BinaryParser(parser.NamespaceBoolean, ValueParser, BinaryEntity):
    """Base parsing class for Switches and BinarySensors linked to a namespace."""

    if TYPE_CHECKING:

        class Args(BinaryEntity.Args, ValueParser.Args):
            pass

    def __init__(
        self,
        channel: "ChannelType | None",
        parent: "BaseDevice",
        /,
        **kwargs: "Unpack[Args]",
    ):
        # TODO: maybe remove this init code:
        # This is due for entities which are created after entry setup when device is already loaded
        # and we want to flush the initial state during HA entry adding (which happens in Entity constructor)
        # without having to flush twice (UNKNOWN -> device state)
        try:
            match kwargs["device_value"]:  # type: ignore
                case self.native_on:
                    kwargs["is_on"] = True
                case self.native_off:
                    kwargs["is_on"] = False
        except KeyError:
            pass
        super().__init__(channel, parent, **kwargs)

    def set_unavailable(self):
        self.is_on = None
        super().set_unavailable()

    @override
    def update_device_value(self, device_value, /) -> bool | None:
        """Default parsing for toggles and binary sensors. Set the proper
        key_value in class/instance definition to make it work."""
        if super().update_device_value(device_value):
            self.flush_state()


class ToggleXParser(BinaryEntity, ParserEntity):
    """Special parser entity which is also linked to Appliance.Control.ToggleX namespace.
    This is intended to add Appliance.Control.ToggleX namespace handling/parsing
    to entities which are represented in HA as more sophisticated entities than simple toggles
    (like Fan-Light)."""

    if TYPE_CHECKING:

        class Args(BinaryEntity.Args, ParserEntity.Args):
            pass

        parent: Final[Device]  # type: ignore[override]
        handler_togglex: Final[NamespaceHandler | None]

    __slots__ = ("handler_togglex",)

    def __init__(self, channel: int, parent: "Device", /, **kwargs: "Unpack[Args]"):
        super().__init__(channel, parent, **kwargs)
        self.handler_togglex = parent.register_togglex_channel(self, True)

    def _parse_togglex(self, payload: dict, /):
        self.update_boolean_value(payload[mc.KEY_ONOFF])


class EntityNamespaceMixin(NamespaceHandler, ParserEntity):
    """
    Special 'polling enabler/disabler' mixin used with entities which are
    'single instance' for a namespace handler and so they'll disable polling
    should they're disabled in HA.
    """

    @classmethod
    def namespace_init(cls, ns: mn.Namespace, device: "Device", /):
        assert ns is cls.ns
        ns_entity = cls(ns, device)
        ns_entity.handler_ns = ns_entity
        ns_entity.polling_strategy = None
        return ns_entity

    @cached_property
    def unique_id(self) -> str | None:
        # MIGRATE: This is to mantain unique_id compatibility with legacy versions
        # since in v6.x.x entity.id initialization is different (at least for EntityNamespaceMixin entities)
        # and is not based on channel/entitykey but just on NamespaceHandler.id (mn.Namespace).
        # keep in mind these entities were already init'ed with channel = None
        return f"{self.parent.id}_{self.entitykey}"

    async def async_added_to_hass(self):
        self.polling_strategy = self.DEFAULT_CONFIG[-1]
        return await super().async_added_to_hass()

    async def async_will_remove_from_hass(self):
        self.polling_strategy = None
        return await super().async_will_remove_from_hass()

    def _handle(self, message: "MerossMessage", /):
        self._parse(message.payload[self.ns.key])
