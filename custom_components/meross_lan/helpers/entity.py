"""
Base-Common behaviour for all Meross-LAN entities
We also try to 'commonize' HA core symbols import in order to better manage
versioning
"""

from functools import cached_property
from typing import TYPE_CHECKING, final, overload, override

from homeassistant.helpers import entity, restore_state
from homeassistant.helpers.entity_platform import async_get_current_platform

from .. import const as mlc
from ..merossclient.device import handler
from ..merossclient.logging import Loggable
from ..merossclient.protocol import const as mc, namespaces as mn

if TYPE_CHECKING:
    from typing import (
        Any,
        Callable,
        ClassVar,
        Final,
        Iterable,
        Literal,
        Mapping,
        NotRequired,
        Protocol,
        Self,
        TypedDict,
        Unpack,
    )

    from homeassistant.config_entries import ConfigEntry
    from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

    from ..merossclient.protocol.types import JsonDict
    from .device import Device
    from .manager import ConfigEntryManager


class Entity(Loggable, entity.Entity if TYPE_CHECKING else object):
    """
    Mixin style base class for all of the entity platform(s)
    This class must prepend the HA entity class in our custom
    entity class definitions.
    """

    if TYPE_CHECKING:

        class DeviceInfo(TypedDict):
            identifiers: set[tuple[str, str]]

        class Sibling(Protocol):
            """Protocol for building sibling entities. This is used when we want to create multiple entities
            for the same channel/subid (i.e. entities with a common device_info/entry) so that they share the
            same HA device_entry. The 'sibling' is used as a source reference for index/device_info/parent(device)
            settings in the created entity. The sibling itself is likely another entity acting as a kind of 'root'
            entity for the same channel. This leads to similarities with SubDevice classes which also acts as reference
            for the same kind of settings but in that case we have a real 'device' instead ofan entity.
            """

            index: Final[mn.IndexValue]
            device_info: Final[Entity.DeviceInfo | None]
            parent: Final[Device]

        type InitArgs = tuple[Sibling | Any, *tuple[ConfigEntryManager, ...]]

        class Args(Loggable.Args):
            entity_key: NotRequired[str | None]
            index: NotRequired[mn.IndexValue]
            # HA core entity attributes:
            device_class: NotRequired[str | None]
            device_info: NotRequired[Entity.DeviceInfo | None]
            entity_category: NotRequired[entity.EntityCategory | None]
            entity_registry_enabled_default: NotRequired[bool]
            name: NotRequired[str | None]
            translation_key: NotRequired[str | None]
            icon: NotRequired[str]

        @classmethod
        def DEF(cls, **kwargs: Unpack[Args]) -> type[Self]: ...

        EntityCategory: Final

        PLATFORM: ClassVar[str]
        HA_ENTITY_ATTRIBUTES: ClassVar[tuple[str, ...]]
        """Provides a list of HA core entity attributes (i.e. 'standard' attributes like 'name')
        which are actually used in meross_lan and so can be set through kwargs in the constructor.
        this class initializer will scan the actual class definition in order to correctly extract
        the eventually provided attributes in kwargs."""
        is_diagnostic: bool
        """Tells if this entity has been created as part of the 'create_diagnostic_entities' config"""

        id: Final[str | int | mn.Namespace]  # type: ignore[override]
        parent: Final[ConfigEntryManager]  # type: ignore[override]
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
        device_info: Final[DeviceInfo | None]
        _attr_entity_category: ClassVar[entity.EntityCategory | None]
        _attr_entity_registry_enabled_default: ClassVar[bool]
        force_update: Final[Literal[False]]
        has_entity_name: Final[Literal[True]]
        _attr_name: ClassVar[str | None]
        _attr_icon: ClassVar[str]
        should_poll: Final[Literal[False]]
        _attr_supported_features: ClassVar[int | None]
        _attr_translation_key: ClassVar[str | None]

        extra_state_attributes: dict[str, Any]

    EntityCategory = entity.EntityCategory
    RestoreEntity = restore_state.RestoreEntity

    HA_ENTITY_ATTRIBUTES = (
        "device_class",
        "entity_category",
        "entity_registry_enabled_default",
        "name",
        "icon",
    )

    # This works as a default for all the entities which are not NamespaceParsers.
    index = mn.IndexType.none.get()

    is_diagnostic = False

    # HA core entity attributes:
    _attr_available = False  # TODO: review the availability mechanics

    init_entity_key = None
    __slots__ = (
        "entity_key",
        "hass_connected",
        # HA core entity attributes
        "translation_key",
    )

    @overload
    def __init__(self, sibling: "Sibling", /, **kwargs: "Unpack[Args]"): ...

    @overload
    def __init__(
        self, id, manager: "ConfigEntryManager", /, **kwargs: "Unpack[Args]"
    ): ...

    def __init__(self, *args: "*InitArgs", **kwargs: "Unpack[Args]"):
        """
        - id (formerly 'channel'): historically used to create an unique id for this entity inside the device
        and also related to the physical channel used in various api for some kind of entities.
        For entities in subdevices (hub paired devices) the channel is usually the Id of the
        subdevice itself since 'HA wise' and 'meross_lan wise' we still group the entities under
        the same (hub) device
        - entity_key: is added to provide additional 'uniqueness' should the device have multiple
        entities for the same channel and usually equal to device_class (but might not be)
        """
        # FIXME: generalize automatic entity_key generation for this pattern:
        # entity_key=f"{ns.slug}__{key_value}",

        entity_key = kwargs.pop("entity_key", self.__class__.init_entity_key)
        if len(args) == 1:
            sibling: "Entity.Sibling" = args[0]
            parent = sibling.parent
            self.device_info = sibling.device_info
            # intercept 'index' kwarg used by NamespaceParser mixin
            # BEWARE: we're relying on the fact that NamespaceParser is effectively initialized only
            # in Loggable.__init__ since it has no constructor defined.
            # Also, actually, index is managed by default auto init Loggable mechanics..
            index = kwargs.setdefault("index", sibling.index)
            if index:
                id = f"{index.slug}_{entity_key}" if entity_key else index.slug
            else:
                id = entity_key
        else:
            parent: "ConfigEntryManager"
            id, parent = args
            # FIXME/REMOVE:
            # Setting up a bunch of '_legacy' attributes to check that the new 'id' and 'entity_key'
            # semantics are consistent with the old ones. This is especially needed to
            # mantain unique_id compatibility with previous meross_lan versions.
            if type(id) is mn.Namespace:
                # TODO: ugly trick...let's see if this can be 'linearized' through some future refactoring.
                # this is a special case for 'EntityNamespaceMixin' entities which are also NamespaceHandlers
                # and so they get initialized with the namespace as channel because of constructor layout
                # and general coding in our inheritance scheme.
                # In this case we set the channel to None and store the namespace in a dedicated
                # variable for later use in parsing and so on.
                if TYPE_CHECKING:
                    assert isinstance(parent, Device)
                self.device_info = parent.device_info
                assert (
                    "index" not in kwargs and "device_info" not in kwargs
                ), "index should not be provided for NamespaceHandler entities since it is fixed to None"
            else:
                # We're receiving either the channel or the subdevice id in this 'id' variable and
                # we use it to get the correct device entry for this entity.
                try:
                    self.device_info = kwargs.pop("device_info")  # type: ignore
                except KeyError:
                    self.device_info = parent.get_device_entry_info(id)
                try:
                    # REMOVE
                    # intercept 'index' arg targeting NamespaceParser mixin
                    # BEWARE: we're relying on the fact that NamespaceParser is effectively initialized only
                    # in Loggable.__init__ since it has no constructor defined.
                    index = kwargs["index"]  # type: ignore
                    if index:
                        id = f"{index.slug}_{entity_key}" if entity_key else index.slug
                    else:
                        id = entity_key
                except KeyError:
                    id = entity_key

        assert (
            id not in parent.entities
        ), f"id:{id} is not unique inside parent.entities"

        self.entity_key = entity_key
        self.hass_connected = False
        # HA core: rather constant
        self.available = self._attr_available or parent.is_connected
        self.force_update = False
        self.has_entity_name = True
        self.should_poll = False
        self.translation_key = (
            kwargs["translation_key"]
            if "translation_key" in kwargs
            else getattr(self, "_attr_translation_key", entity_key)
        )
        # unique_id is by default computed internally so to have a consistent layout.
        # Not all entities should or will adhere to this but since
        # they should be rare we're using local overrides here and there in descendants.
        # The unique_id can be either overwritten after this constructor or (maybe better
        # in terms of design) implemented through a property.
        # Considerations about unique_id migration:
        # The unique_id should follow this format (at least for parsers):
        # "{uuid}_{ns.slug}_{key_value}_{channel}" where channel is optional ofc
        # but is always related to the KEY_CHANNEL in the payload and not to the subdevice id
        # For subdevices we should move to a format where we get rid of the parent hub.id
        # and use instead the subdev id in the unique_id since it is already unique.
        # "{subdev_id}_{ns.slug}_{key_value}_{channel}" where channel is optional ofc
        # This way we could maybe get rid of entity_key
        self.unique_id = f"{parent.id}_{id}"
        # simple setting of HA core attributes if provided in kwargs
        # else fallback to HA core mechanics
        for _attr in tuple(
            _attr for _attr in kwargs if _attr in self.__class__.HA_ENTITY_ATTRIBUTES
        ):
            setattr(self, _attr, kwargs.pop(_attr))
        super().__init__(id, parent, **kwargs)
        parent.entities[id] = self
        if parent.platforms:
            # this entity is being created after entry setup
            parent.add_entity(self)

    # interface: entity.Entity
    def _name_internal(
        self,
        device_class_name: str | None,
        platform_translations: dict[str, str],
    ) -> str | entity.UndefinedType | None:
        """Return the name of the entity. This is a (dangerous?!) patch overriding
        the HA core Entity name mechanics in order to provide more flexible naming mechanics.
        """
        if hasattr(self, "_attr_name"):
            return self._attr_name

        translation_key = f"component.{self.platform_data.platform_name}.entity.{self.platform_data.domain}.{self.translation_key}.name"
        if translation_key in platform_translations:
            return self._substitute_name_placeholders(
                platform_translations[translation_key]
            )

        if self._default_to_device_class_name():
            return device_class_name

        if entity_key := self.entity_key:
            entity_key_split = entity_key.split("_")
            if len(entity_key_split) > 2:
                # For 'new style' entity_key(s) in the order of 'ns_slug__key_value' we want to use
                # only the (last split) key_value part as name.
                return entity_key_split[-1].capitalize()
            else:
                return entity_key.replace("_", " ").capitalize()
        return entity.UNDEFINED

    async def async_added_to_hass(self):
        await super().async_added_to_hass()
        self.log(self.VERBOSE, "Added to HomeAssistant")
        self.hass_connected = True  # type: ignore

    async def async_will_remove_from_hass(self):
        await super().async_will_remove_from_hass()
        self.log(self.VERBOSE, "Removed from HomeAssistant")
        self.hass_connected = False  # type: ignore

    # interface: self
    @final
    def register_state_callback(self, state_callback: "Callable[[], Any]", /):
        """Registers a callback to be called when flush_state is called."""

        old_flush = self.flush_state

        def _wrapper():
            old_flush()
            state_callback()

        self.flush_state = _wrapper

        def _cleanup():
            del self.flush_state

        self.shutdown_broadcast.add(_cleanup)

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

    def set_available(self):
        """Expected to be called by device/subdevice on_connect."""
        self.available = True
        self.flush_state()

    def set_unavailable(self):
        """Expected to be called by device/subdevice on_disconnect."""
        if not self._attr_available:
            self.available = False
            self.flush_state()

    def update_device_value(self, device_value, /) -> bool | None:
        raise NotImplementedError(
            "update_device_value must be implemented by subclasses if used in parsing scheme"
        )

    @classmethod
    async def platform_setup_entry(
        cls,
        hass,
        config_entry: "ConfigEntry[ConfigEntryManager]",
        async_add_entities: "AddConfigEntryEntitiesCallback",
    ):
        platform = cls.PLATFORM
        manager = config_entry.runtime_data
        manager.log(manager.DEBUG, "platform_setup_entry { platform: %s }", platform)
        manager.platforms[platform] = async_get_current_platform()
        async_add_entities(
            [
                entity
                for entity in manager.entities_iterable
                if (entity.PLATFORM is platform) and not entity.platform
            ]
        )


class ParserEntity(handler.NamespaceParser, Entity):
    """Base class for entities directly linked to a device and not to a namespace.
    This is actually not used that much since most of the entities are linked to namespaces but it can be useful
    for some 'general' entities like 'DeviceInfo' or so."""

    if TYPE_CHECKING:
        parent: Final[Device]  # type: ignore[override]
        device_info: Final[Entity.DeviceInfo]  # type: ignore[override]
        handler_ns: handler.NamespaceHandler  # override

        _parse_togglex: Callable[[JsonDict], Any]

        type Sibling = Entity.Sibling
        type InitArgs = tuple[Sibling | Any, *tuple[Device, ...]]

        class Args(Entity.Args, handler.NamespaceParser.Args):
            pass

        def __init__(self, *args: *InitArgs, **kwargs: Unpack[Args]): ...

        @classmethod
        def DEF(cls, **kwargs: Unpack[Args]) -> type[Self]: ...

    @override
    def set_available(self):
        self.available = True
        # we don't flush here since we'll wait for actual device readings

    @override
    def set_unavailable(self):
        self.available = False
        self.ns_value = self.__class__.init_ns_value
        self.flush_state()


class ValueParser(handler.ValueParser, ParserEntity):
    """Specialization for 'simple' parser entities where the HA entity state is a function
    of a single data point in the json ns payload. This provides a common implementation
    for setting the value (using handler.ValueParser.async_request_value) and for parsing the
    value from the payload (using handler.ValueParser.update_device_value).
    Examples of such entities are sensors/numbers, binary_sensors/switches."""

    if TYPE_CHECKING:

        type InitArgs = ParserEntity.InitArgs

        class Args(ParserEntity.Args, handler.ValueParser.Args):
            pass

        def __init__(self, *args: *InitArgs, **kwargs: Unpack[Args]): ...

    @override
    def update_device_value(self, device_value, /) -> bool | None:
        if self.ns_value != device_value:
            self.ns_value = device_value
            self.flush_state()
            return True


class NumericEntity(Entity):
    """Common base class for sensors and numbers."""

    if TYPE_CHECKING:

        DEVICECLASS_TO_UNIT_MAP: ClassVar[dict[str | None, str | None]]

        # HA core entity attributes:
        _attr_native_unit_of_measurement: ClassVar[str | None]
        native_value: int | float | None

        type InitArgs = Entity.InitArgs

        class Args(Entity.Args):
            native_value: NotRequired[int | float]
            native_unit_of_measurement: NotRequired[str | None]

        def __init__(self, *args: InitArgs, **kwargs: Unpack[Args]): ...

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
        """device_scale type need to follow the type supported for ns_value.
        This is used in Number entity to do the correct roundings when converting between
        native_value and ns_value."""
        ns_value: int | float | None

        type InitArgs = ValueParser.InitArgs

        class Args(ValueParser.Args, NumericEntity.Args):
            device_scale: NotRequired[int | float]

    init_device_scale = 1
    SLOTS_AUTO_INIT = ("device_scale",)

    def __init__(self, *args: "*InitArgs", **kwargs: "Unpack[Args]"):
        try:
            kwargs["native_value"] = kwargs["ns_value"] / kwargs.get("device_scale", self.init_device_scale)  # type: ignore
        except KeyError:
            pass
        super().__init__(*args, **kwargs)

    @override
    def update_device_value(self, device_value: int | float, /):
        if self.ns_value != device_value:
            self.ns_value = device_value
            self.native_value = device_value / self.device_scale
            self.flush_state()
            return True


class BinaryEntity(Entity):
    """Base class for HA ToggleEntity."""

    if TYPE_CHECKING:

        is_on: Any

        type InitArgs = Entity.InitArgs

        class Args(Entity.Args):
            is_on: NotRequired[Any]

        def __init__(self, *args: *InitArgs, **kwargs: Unpack[Args]): ...

    SLOTS_AUTO_INIT = ("is_on",)
    __slots__ = ("is_on",)
    # TODO: fix _calc_slots in descendants by maybe adding an init_subclass in BinaryParser.

    def update_boolean_value(self, is_on: "Any", /) -> bool | None:
        if self.is_on != is_on:
            self.is_on = is_on
            self.flush_state()
            return True


class BinaryParser(handler.BooleanParser, ValueParser, BinaryEntity):
    """Base parsing class for HA core ToggleEntity linked to a namespace."""

    if TYPE_CHECKING:

        type InitArgs = ValueParser.InitArgs

        class Args(handler.BooleanParser.Args, ValueParser.Args, BinaryEntity.Args):
            pass

        @classmethod
        def DEF(cls, **kwargs: Unpack[Args]) -> type[Self]: ...

    def __init_subclass__(cls):
        super().__init_subclass__()
        cls.__slots__ = cls._calc_slots()

    def __init__(self, *args: "*InitArgs", **kwargs: "Unpack[Args]"):
        # This is due for entities which are created after entry setup when device is already loaded
        # and we want to flush the initial state during HA entry adding (which happens in Entity constructor)
        # without having to flush twice (UNKNOWN -> device state).
        try:
            # if ns_value is not provided this code is unnecessary and
            # the eventual other arguments will be managed by bases.
            self.ns_value = kwargs.pop("ns_value")
            self.value_on = kwargs.pop("value_on", self.init_value_on)
            self.value_off = kwargs.pop("value_off", self.init_value_off)
            match self.ns_value:
                case self.value_on:
                    kwargs["is_on"] = True
                case self.value_off:
                    kwargs["is_on"] = False
        except KeyError:
            pass
        super().__init__(*args, **kwargs)

    def set_unavailable(self):
        self.is_on = None
        super().set_unavailable()

    @override
    def update_device_value(self, device_value, /) -> bool | None:
        if super().update_device_value(device_value):
            self.flush_state()
            return True


class ToggleXParser(BinaryEntity, ParserEntity):
    """Special parser entity which is also linked to Appliance.Control.ToggleX namespace.
    This is intended to add Appliance.Control.ToggleX namespace handling/parsing
    to entities which are represented in HA as more sophisticated entities than simple toggles
    (like Fan-Light).
    Since this entity is linked to multiple ns it would be better to use schedule_flush_state
    to avoid multiple flush in case both ns are present in the same payload."""

    if TYPE_CHECKING:

        parent: Final[Device]  # type: ignore[override]
        handler_togglex: Final[handler.NamespaceHandler | None]

        type InitArgs = ParserEntity.InitArgs

        class Args(BinaryEntity.Args, ParserEntity.Args):
            pass

    __slots__ = ("handler_togglex",)

    def __init__(self, *args: "*InitArgs", **kwargs: "Unpack[Args]"):
        super().__init__(*args, **kwargs)
        self.handler_togglex = self.parent.register_togglex_channel(self, True)  # type: ignore

    def _parse_togglex(self, payload: dict, /):
        is_on = bool(payload[mc.KEY_ONOFF])
        if self.is_on != is_on:
            self.is_on = is_on
            self.flush_state()


class EntityNamespaceMixin(ParserEntity, handler.ParserHandler):
    """
    Special 'polling enabler/disabler' mixin used with entities which are
    'single instance' for a namespace handler and so they'll disable polling
    should they're disabled in HA.
    """

    if TYPE_CHECKING:
        id: Final[mn.Namespace]  # type: ignore[override]

        type InitArgs = tuple[mn.Namespace, Device]

        class Args(ParserEntity.Args):
            pass

        def __init__(self, *args: *InitArgs, **kwargs: Unpack[Args]): ...

    def __init_subclass__(cls):
        super().__init_subclass__()
        # Since NamespaceHandler cannot be slotted itself because of mixin-ing with ParserEntity
        # in EntityNamespaceMixin we try this trick to provide automatic slotting for all the subclasses
        # which are not mixed with parsers and which don't define their own __slots__.
        cls.__slots__ = cls._calc_slots()

    @classmethod
    @override
    def namespace_init(cls, ns: mn.Namespace, device: "Device", /):
        ns_entity = cls(ns, device, ns=ns)
        ns_entity.unique_id = f"{device.id}_{ns_entity.entity_key}"
        ns_entity.handler_ns = ns_entity
        ns_entity.polling_strategy = None
        return ns_entity

    def shutdown(self):
        # Because of the mixin nature of this class the shutdown sequence could be invoked by
        # both Entity and NamespaceHandler shutdown mechanics so we need filter out the duplicate.
        super().shutdown()
        del self.parent.ns_handlers[self.id]

    async def async_added_to_hass(self):
        self.polling_strategy = self.POLLING_CONFIG_DEFAULT[-1]
        await ParserEntity.async_added_to_hass(self)

    async def async_will_remove_from_hass(self):
        self.polling_strategy = None
        await ParserEntity.async_will_remove_from_hass(self)
