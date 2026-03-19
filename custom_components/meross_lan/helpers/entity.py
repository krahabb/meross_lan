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
    from ..merossclient.protocol.types import JsonDict, JsonList, PayloadIndexType
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
            # HA core entity attributes:
            device_class: NotRequired[str | None]
            device_entry: NotRequired[DeviceEntry]
            entity_category: NotRequired[entity.EntityCategory | None]
            entity_registry_enabled_default: NotRequired[bool]
            name: NotRequired[str | None]
            translation_key: NotRequired[str]
            icon: NotRequired[str]

        EntityCategory: Final

        PLATFORM: ClassVar[str]
        HA_ENTITY_ATTRIBUTES: ClassVar[tuple[str, ...]]
        """Provides a list of HA core entity attributes (i.e. 'standard' attributes like 'name')
        which are actually used in meross_lan and so can be set through kwargs in the constructor.
        this class initializer will scan the actual class definition in order to correctly extract
        the eventually provided attributes in kwargs."""
        is_diagnostic: ClassVar[bool]  # TODO: type uppercase
        """Tells if this entity has been created as part of the 'create_diagnostic_entities' config"""

        parent: Final[EntityManager]  # type: ignore[override]
        channel: (
            ChannelType | None
        )  # TODO: maybe remove since it might only be relevant in ParserEntity
        init_entity_key: ClassVar[str | None]
        entity_key: Final[str | None]
        # used to speed-up checks if entity is enabled and loaded
        hass_connected: Final[bool]  # public ReadOnly attribute

        # HA core entity attributes:
        # Annotated here are those which are actually used in meross_lan.
        # Their management is a mixture of HA core Entity initialization or
        # explicit initialization in our constructor(s) where needed.
        # For example, when any of these is passed in as kwarg we explicitly set it else
        # we'll let the default HA mechanics handle it (i.e. _attr_... class attributes or HA defaults).
        _attr_assumed_state: ClassVar[bool]
        _attr_available: ClassVar[bool]
        _attr_device_class: ClassVar[str | None]
        force_update: Final[Literal[False]]
        has_entity_name: Final[Literal[True]]
        _attr_name: ClassVar[str | None]
        _attr_entity_category: ClassVar[entity.EntityCategory | None]
        _attr_entity_registry_enabled_default: ClassVar[bool]
        _attr_icon: ClassVar[str]
        should_poll: Final[Literal[False]]
        _attr_supported_features: ClassVar[int | None]
        _attr_translation_key: ClassVar[str | None]

        extra_state_attributes: dict[str, Any]

    EntityCategory = entity.EntityCategory

    HA_ENTITY_ATTRIBUTES = (
        "device_class",
        "entity_category",
        "entity_registry_enabled_default",
        "translation_key",
        "icon",
    )
    is_diagnostic = False

    # HA core entity attributes:
    _attr_available = False  # TODO: review the availability mechanics

    init_entity_key = None
    __slots__ = (
        "entity_key",
        "hass_connected",
    )

    # TODO: re-organize channel initialization by moving to a kwarg so that we can use base Parser semantics
    # in related hierarchy tree.
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
        - entity_key: is added to provide additional 'uniqueness' should the device have multiple
        entities for the same channel and usually equal to device_class (but might not be)
        - device_class: used by HA to set some soft 'class properties' for the entity
        """
        entity_key = kwargs.pop("entity_key", self.__class__.init_entity_key)
        if type(channel) is mn.Namespace:
            # TODO: ugly trick...let's see if this can be 'linearized' through some future refactoring.
            # this is a special case for 'EntityNamespaceMixin' entities which are also NamespaceHandlers
            # and so they get initialized with the namespace as channel because of constructor layout
            # and general coding in our inheritance scheme.
            # In this case we set the channel to None and store the namespace in a dedicated
            # variable for later use in parsing and so on.
            id = channel  # ns
            channel = None
        else:
            id = (
                channel
                if entity_key is None
                else entity_key if channel is None else f"{channel}_{entity_key}"
            )
            assert (
                id is not None
            ), "provide at least channel or entity_key (cannot be 'None' together)"
        assert (
            id not in manager.entities
        ), f"id:{id} is not unique inside parent.entities"
        self.channel = channel
        self.entity_key = entity_key
        self.hass_connected = False
        # HA core: rather constant
        self.available = self._attr_available or manager.is_connected
        self.force_update = False
        self.has_entity_name = True
        self.should_poll = False
        # HA core special handling
        try:
            self.device_entry = kwargs.pop("device_entry")
        except KeyError:
            self.device_entry = manager.get_device_entry(channel)
        try:
            self.name = kwargs.pop("name")
        except KeyError:
            try:
                self.name = self._attr_name
            except AttributeError:
                if entity_key:
                    self.name = entity_key.replace("_", " ").capitalize()
                else:
                    # as it is now implemented this will instruct HA core
                    # to use device name when it can't provide an entity name
                    self.use_device_name = True
        # simple setting of HA core attributes if provided in kwargs
        # else fallback to HA core mechanics
        for _attr_name in tuple(
            _attr_name
            for _attr_name in kwargs
            if _attr_name in self.__class__.HA_ENTITY_ATTRIBUTES
        ):
            setattr(self, _attr_name, kwargs.pop(_attr_name))

        super().__init__(id, manager, **kwargs)
        manager.entities[id] = self
        manager.async_shutdown_broadcast.add(self.async_shutdown)
        try:
            manager.platforms[self.PLATFORM]([self])  # type: ignore
        except KeyError:
            # platform never registered before for this manager
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

    class EntityDef[_T: Entity](dict):
        """Descriptor class used when populating maps used to dynamically instantiate
        entities based on their appearance in a payload key (typically in sensor payloads
        but more use cases are implemented)."""

        type: "Final[type[_T]]"

        __slots__ = ("type",)

        def __init__(self, type: "type[_T]", **kwargs: "Unpack[Entity.Args]"):
            dict.__init__(self, **kwargs)
            self.type = type

        def __call__(self, *args, **kwargs: "Unpack[Entity.Args]") -> _T:
            return self.type(*args, **(self | kwargs))

    @classmethod
    def ENTITY_DEF(cls, **kwargs: "Unpack[Args]") -> type["Self"]:
        # This method returns a special class 'EntityDef' but
        # type hinting suggests it is still self.cls so that the
        # 'hidden' EntityDef works like a wrapper for constructor
        # keyword arguments and this semantic allows to chain different
        # calls each one adding its own custom set of kwargs.
        # In the end, the return type works exactly as a standard
        # constructor in term of syntax and semantics (unless we inspect it ofc)
        # This 'funny' semantic allows us to define ENTITY_DEFS maps wherever needed
        # where both simple class types and EntityDef instances can work as consistent
        # callables with the same syntax as the class constructor.
        # TODO: This technique is very useful except we should still find a way to
        # automatically 'infer' the kwargs unpacking for the relevant cls.
        # This is actually overcomed with typing overwrites in child classes where the Args
        # type differs from the base Entity.Args.
        return Entity.EntityDef(cls, **kwargs)  # type: ignore[return-value]


class ParserEntity(parser.NamespaceParser, Entity):
    """Base class for entities directly linked to a device and not to a namespace.
    This is actually not used that much since most of the entities are linked to namespaces but it can be useful
    for some 'general' entities like 'DeviceInfo' or so."""

    if TYPE_CHECKING:
        parent: Final[BaseDevice]  # type: ignore[override]
        handler_ns: NamespaceHandler  # override

        _parse_togglex: Callable[[JsonDict], Any]

        class Args(Entity.Args, parser.NamespaceParser.Args):
            pass

        def __init__(
            self,
            channel: PayloadIndexType | None,
            parent: BaseDevice,
            /,
            **kwargs: Unpack[Args],
        ): ...

        @classmethod
        def ENTITY_DEF(cls, **kwargs: "Unpack[Args]") -> type["Self"]: ...

    NamespaceValue = parser.NamespaceValue
    NamespaceGroupValue = parser.NamespaceGroupValue

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


class ValueParser(parser.NamespaceValue, ParserEntity):
    """Specialization for 'simple' parser entities where the HA entity state is a function
    of a single data point in the json ns payload. This provides a common implementation
    for setting the value (using parser.NamespaceValue.async_request_value) and for parsing the
    value from the payload (using parser.NamespaceValue.update_device_value).
    Examples of such entities are sensors/numbers, binary_sensors/switches."""

    if TYPE_CHECKING:

        class Args(ParserEntity.Args, parser.NamespaceValue.Args):
            pass

        def __init__(
            self,
            channel: ChannelType | None,
            parent: BaseDevice,
            /,
            **kwargs: Unpack[Args],
        ): ...

    def set_unavailable(self):
        self.device_value = None
        super().set_unavailable()

    @override
    def update_device_value(self, device_value, /) -> bool | None:
        if self.device_value != device_value:
            self.device_value = device_value
            self.flush_state()
            return True


class NumericEntity(Entity):
    """Common base class for sensors and numbers."""

    if TYPE_CHECKING:

        DEVICECLASS_TO_UNIT_MAP: ClassVar[dict[str | None, str | None]]

        # HA core entity attributes:
        _attr_native_unit_of_measurement: ClassVar[str | None]
        native_value: int | float | None

        class Args(Entity.Args):
            native_value: NotRequired[int | float]
            native_unit_of_measurement: NotRequired[str | None]

        def __init__(
            self,
            channel: ChannelType | None,
            parent: EntityManager,
            /,
            **kwargs: Unpack[Args],
        ): ...

    # We rely on our sensor entity to be initialized for sure since it's going to provide the symbols for
    # device classes which are nevertheless shared between sensor and number entities. Tha mapping should
    # be done by str value despite the fact numbers and sensors each defines their own enums.
    HA_ENTITY_ATTRIBUTES = Entity.HA_ENTITY_ATTRIBUTES + ("native_unit_of_measurement",)
    SLOTS_AUTO_INIT = ("native_value",)
    __slots__ = ("native_value",)

    def update_native_value(self, native_value: int | float | None, /):
        if self.native_value != native_value:
            self.native_value = native_value
            self.flush_state()
            return True

    @cached_property
    def native_unit_of_measurement(self):
        try:
            return self._attr_native_unit_of_measurement
        except AttributeError:
            try:
                return self.DEVICECLASS_TO_UNIT_MAP[self.device_class]
            except KeyError:
                return None


class NumericParser(ValueParser, NumericEntity):
    """Specialized parser for numeric entities like Number and Sensor."""

    if TYPE_CHECKING:

        init_device_scale: ClassVar[int | float]
        device_scale: int | float
        """device_scale type need to follow the type supported for device_value.
        This is used in Number entity to do the correct roundings when converting between
        native_value and device_value."""
        device_value: int | float | None

        class Args(ValueParser.Args, NumericEntity.Args):
            device_scale: NotRequired[int | float]

    init_device_scale = 1
    SLOTS_AUTO_INIT = ("device_scale",)

    def __init__(
        self,
        channel: "ChannelType | None",
        parent: "BaseDevice",
        /,
        **kwargs: "Unpack[Args]",
    ):
        try:
            kwargs["native_value"] = kwargs["device_value"] / kwargs.get("device_scale", self.init_device_scale)  # type: ignore
        except KeyError:
            pass
        super().__init__(channel, parent, **kwargs)

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

        is_on: Any

        class Args(Entity.Args):
            is_on: NotRequired[Any]

        def __init__(
            self,
            channel: "ChannelType | None",
            manager: "EntityManager",
            /,
            **kwargs: "Unpack[Args]",
        ): ...

    SLOTS_AUTO_INIT = ("is_on",)
    __slots__ = ("is_on",)

    def update_boolean_value(self, is_on: "Any", /) -> bool | None:
        if self.is_on != is_on:
            self.is_on = is_on
            self.flush_state()
            return True


class BinaryParser(parser.NamespaceBoolean, ValueParser, BinaryEntity):
    """Base parsing class for Switches and BinarySensors linked to a namespace."""

    if TYPE_CHECKING:

        class Args(parser.NamespaceBoolean.Args, ValueParser.Args, BinaryEntity.Args):
            pass

        @classmethod
        def ENTITY_DEF(cls, **kwargs: "Unpack[Args]") -> type["Self"]: ...

    def __init__(
        self,
        channel: "ChannelType | None",
        parent: "BaseDevice",
        /,
        **kwargs: "Unpack[Args]",
    ):
        # This is due for entities which are created after entry setup when device is already loaded
        # and we want to flush the initial state during HA entry adding (which happens in Entity constructor)
        # without having to flush twice (UNKNOWN -> device state).
        try:
            # if device_value is not provided this code is unnecessary and
            # the eventual other arguments will be managed by bases.
            self.device_value = kwargs.pop("device_value")
            self.value_on = kwargs.pop("value_on", self.init_value_on)
            self.value_off = kwargs.pop("value_off", self.init_value_off)
            match self.device_value:
                case self.value_on:
                    kwargs["is_on"] = True
                case self.value_off:
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

        parent: Final[Device]  # type: ignore[override]
        handler_togglex: Final[NamespaceHandler | None]

        class Args(BinaryEntity.Args, ParserEntity.Args):
            pass

    __slots__ = ("handler_togglex",)

    def __init__(self, channel: int, parent: "Device", /, **kwargs: "Unpack[Args]"):
        super().__init__(channel, parent, **kwargs)
        self.handler_togglex = parent.register_togglex_channel(self, True)  # type: ignore

    def _parse_togglex(self, payload: dict, /):
        self.update_boolean_value(payload[mc.KEY_ONOFF])


class EntityNamespaceMixin(NamespaceHandler, ParserEntity):
    """
    Special 'polling enabler/disabler' mixin used with entities which are
    'single instance' for a namespace handler and so they'll disable polling
    should they're disabled in HA.
    """

    if TYPE_CHECKING:

        parent: Final[Device]  # type: ignore[override]

        class Args(ParserEntity.Args):
            pass

        def __init__(
            self,
            id: mn.Namespace,
            parent: "Device",
            /,
            **kwargs: "Unpack[Args]",
        ): ...

    def __init_subclass__(cls):
        super().__init_subclass__()
        # Since NamespaceHandler cannot be slotted itself because of mixin-ing with ParserEntity
        # in EntityNamespaceMixin we try this trick to provide automatic slotting for all the subclasses
        # which are not mixed with parsers and which don't define their own __slots__.
        cls.__slots__ = cls._calc_slots()

    @classmethod
    def namespace_init(cls, ns: mn.Namespace, device: "Device", /):
        ns_entity = cls(ns, device, ns=ns)
        ns_entity.handler_ns = ns_entity
        ns_entity.polling_strategy = None
        return ns_entity

    @cached_property
    def unique_id(self) -> str | None:
        # MIGRATE: This is to mantain unique_id compatibility with legacy versions
        # since in v6.x.x entity.id initialization is different (at least for EntityNamespaceMixin entities)
        # and is not based on channel/entity_key but just on NamespaceHandler.id (mn.Namespace).
        # keep in mind these entities were already init'ed with channel = None
        return f"{self.parent.id}_{self.entity_key}"

    async def async_added_to_hass(self):
        self.polling_strategy = self.POLLING_CONFIG_DEFAULT[-1]
        return await super().async_added_to_hass()

    async def async_will_remove_from_hass(self):
        self.polling_strategy = None
        return await super().async_will_remove_from_hass()

    def _handle(self, message: "MerossMessage", /):
        self._parse(message.payload[self.id.key])
