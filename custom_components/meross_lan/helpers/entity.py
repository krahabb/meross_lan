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

from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity

from ..merossclient.device.handler import NamespaceParser
from ..merossclient.protocol import MerossError, const as mc, namespaces as mn
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

    from ..merossclient.protocol.types import JsonDict, JsonMapping, PayloadIndexType
    from .device import BaseDevice, Device, MerossResponse
    from .manager import ConfigEntryManager, EntityManager

    type ChannelType = PayloadIndexType


class MLEntity(NamespaceParser, entity.Entity if TYPE_CHECKING else object):
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

        is_diagnostic: ClassVar[bool]
        """Tells if this entity has been created as part of the 'create_diagnostic_entities' config"""

        handler_ns: NamespaceHandler  # override NamespaceParser typing
        key_value: str  # defaulted to 'value'
        _parse_togglex: Callable[[JsonDict], Any]

        manager: EntityManager  # Final
        channel: Final[ChannelType | None]
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
    NS_CHANNELS = None  # scan digests for channels
    NS_CHANNELS_SINGLE = (0,)

    is_diagnostic = False

    key_value = mc.KEY_VALUE

    # HA core entity attributes:
    force_update = False
    _attr_has_entity_name = True
    should_poll = False
    _attr_available = False
    _attr_device_class = None
    _attr_entity_registry_enabled_default = True
    assumed_state = False
    entity_category = None
    extra_state_attributes = {}
    icon = None
    translation_key = None

    __slots__ = (
        # meross_lan managed attributes
        "manager",
        "channel",
        "entitykey",
        "hass_connected",
        "_payload_ns",  # inherited from NamespaceParser
        # HA core
        "available",
        "device_class",
        "device_entry",
        "entity_registry_enabled_default",
        "has_entity_name",
        "_schedule_flush_state_unsub",
    ) + NamespaceParser.__SLOTS__

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
        # init these first since Loggable init could call configure_logger which 'sometimes'
        # could rely on these
        manager.objects.add(self)
        self.manager = manager
        self.channel = channel
        self.entitykey = entitykey = kwargs.pop("entity_key", self.__class__.ENTITY_KEY)
        self._payload_ns = mn.EMPTY_DICT
        self._schedule_flush_state_unsub = None
        id = (
            channel
            if entitykey is None
            else entitykey if channel is None else f"{channel}_{entitykey}"
        )
        super().__init__(id, manager)
        # init before raising exceptions so that the Loggable is
        # setup before any exception is raised
        assert (
            id is not None
        ), "provide at least channel or entitykey (cannot be 'None' together)"
        assert (
            id not in manager.entities
        ), f"id:{id} is not unique inside manager.entities"

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
        try:
            manager.platforms[self.PLATFORM]([self])  # type: ignore
        except KeyError:
            manager.platforms[self.PLATFORM] = None
        except TypeError:
            pass  # platform setup not yet done

    # interface: Entity
    @cached_property
    def unique_id(self) -> str | None:
        return self.manager.generate_unique_id(self)

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
        if self._schedule_flush_state_unsub:
            self._schedule_flush_state_unsub.cancel()
            self._schedule_flush_state_unsub = None
        await super().async_shutdown()
        try:
            del self.flush_state  # remove any possible state callback registration
        except AttributeError:
            pass
        del self.manager.entities[self.id]
        del self.manager

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
        if self._schedule_flush_state_unsub:
            self._schedule_flush_state_unsub.cancel()
        if self.hass_connected:
            self._schedule_flush_state_unsub = self.manager.schedule_callback(
                delay, self.flush_state
            )
        else:
            self._schedule_flush_state_unsub = None

    def set_available(self):
        self.available = True
        # we don't flush here since we'll wait for actual device readings

    def set_unavailable(self):
        if self._attr_available:
            return  # this entity is always available, no need to set unavailable
        self.available = False
        self._payload_ns = mn.EMPTY_DICT
        self.flush_state()

    def update_native_value(self, native_value, /) -> bool | None:
        """This is a stub definition. It will usually be called by update_device_value
        with the result of the conversion from the incoming device value (from Meross protocol)
        to the proper HA type/value for the entity class."""
        raise NotImplementedError("Called 'update_native_value' on wrong entity type")

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

    # TODO: move to a subclass kind of MLDeviceEntity
    # interface: device communication
    def update_device_value(self, device_value, /) -> bool | None:
        """This is a stub definition. It will be called by _parse (when namespace dispatching
        is configured so) or directly as a short path inside other parsers to forward the
        incoming device value to the underlyinh HA entity state."""
        raise NotImplementedError("Called 'update_device_value' on wrong entity type")

    async def async_request_value(self, device_value, /) -> None:
        """Issues a command SET to update the device and also updates
        the entity state if the command was acknowledged by the device.
        Raises exception on connection/protocol errors."""
        await self.async_request_payload({self.key_value: device_value})
        self.update_device_value(device_value)

    @override  # NamespaceParser
    def _parse(self, payload: "JsonMapping", /):
        """Default parsing for entities. Set the proper
        key_value in class/instance definition to make it work."""
        self.update_device_value(payload[self.key_value])

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

    @staticmethod
    def ha_action(func):
        """
        Decorator to wrap HA service calls and raise HomeAssistantError on failure.
        This will prevent dumping the full stack trace in the logs and instead log a concise error message
        on selected exceptions.
        """

        def _ha_action(*args, **kwargs):
            try:
                return func(*args, **kwargs)
            except MerossError as error:
                # Meross protocol error, typically due to a device communication issue.
                raise HomeAssistantError(str(error)) from error

        return _ha_action

    @classmethod
    def namespace_init(cls, ns: mn.Namespace, device: "Device", /):
        """Helper to register a specialized entity class to the proper namespace.
        This is going to be used on Device initialization fo various entities sharing
        common semantics in namespace parsing/handling."""
        assert ns is cls.ns
        NamespaceHandler(ns, device).register_entity_class(cls, cls.NS_CHANNELS)

    class EntityDef[_T: MLEntity]:
        """Descriptor class used when populating maps used to dynamically instantiate (sensor)
        entities based on their appearance in a payload key."""

        type: "Final[type[_T]]"
        kwargs: "Final[Any]"

        __slots__ = ("type", "kwargs")

        def __init__(self, type: "type[_T]", **kwargs: "Unpack[MLEntity.Args]"):
            self.type = type
            self.kwargs = kwargs

    @classmethod
    def ENTITY_DEF(
        cls,
        **kwargs: "Unpack[Args]",
    ) -> "MLEntity.EntityDef[Self]":
        return MLEntity.EntityDef["Self"](cls, **kwargs)

    class PartialAvailableMixin:
        """
        Mixin class for entities which should be available when device is connected
        but their state needs to be preserved since they're representing a state not directly
        carried by the device ('emulated' configuration params like MLEmulatedNumber or so).
        """

        if TYPE_CHECKING:

            def flush_state(self): ...

        def set_available(self):
            self.available = True
            self.flush_state()

        def set_unavailable(self):
            self.available = False
            self.flush_state()

    class GroupListChannelMixin(NamespaceParser if TYPE_CHECKING else object):
        """
        Implementation for protocol method 'SET' on entities/namespaces backed by a channel
        list and the actual entity value is embedded in a 'group' key (see Appliance.Config.DeviceCfg).
        """

        if TYPE_CHECKING:
            manager: BaseDevice
            key_group: str
            key_value: str

            def update_device_value(self, device_value, /) -> bool | None: ...

        # interface: MLEntity
        async def async_request_value(self, device_value, /):
            (
                await self.manager.async_request(
                    *self.ns.request_set(
                        {self.key_group: {self.key_value: device_value}}, self.channel
                    )
                )
            )
            self.update_device_value(device_value)

        def _parse(self, payload, /):
            self.update_device_value(payload[self.key_group][self.key_value])


class MLBinaryEntity(MLEntity):
    """Partially abstract common base class for ToggleEntity and BinarySensor.
    The initializer is skipped."""

    if TYPE_CHECKING:

        class Args(MLEntity.Args):
            device_value: NotRequired[Any]

        # These work much like key_value in MLEntity so that they're generally class attributes
        native_on: Any
        """The actual device value representing the 'on' state."""
        native_off: Any
        """The actual device value representing the 'off' state."""
        # HA core entity attributes:
        is_on: Any | None

    key_value = mc.KEY_ONOFF
    native_on = 1
    native_off = 0

    __slots__ = ("is_on",)

    def __init__(
        self,
        channel: "ChannelType | None",
        manager: "BaseDevice",
        /,
        **kwargs: "Unpack[Args]",
    ):
        match kwargs.pop("device_value", None):
            case self.native_on:
                self.is_on = True
            case self.native_off:
                self.is_on = False
            case _:
                self.is_on = None
        super().__init__(channel, manager, **kwargs)

    def set_unavailable(self):
        self.is_on = None
        super().set_unavailable()

    @override
    def update_device_value(self, device_value, /) -> bool | None:
        """Default parsing for toggles and binary sensors. Set the proper
        key_value in class/instance definition to make it work."""
        match device_value:
            case self.native_on:
                return self.update_native_value(True)
            case self.native_off:
                return self.update_native_value(False)
            case _:
                return self.update_native_value(None)

    @override
    def update_native_value(self, onoff, /):
        if self.is_on != onoff:
            self.is_on = onoff
            self.flush_state()
            return True

    # provide a generalized toggle behavior for binary entities
    @MLEntity.ha_action
    async def async_turn_on(self, **kwargs):
        await self.async_request_value(self.native_on)

    @MLEntity.ha_action
    async def async_turn_off(self, **kwargs):
        await self.async_request_value(self.native_off)


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
        _attr_native_unit_of_measurement: ClassVar[str | None]
        native_unit_of_measurement: str | None

    """To be init in derived classes with their DeviceClass own types."""
    _attr_device_scale = 1
    _attr_native_unit_of_measurement = None

    __slots__ = (
        "device_scale",
        "device_value",
        "native_value",
        "native_unit_of_measurement",
    )

    def __init__(
        self,
        channel: "ChannelType | None",
        manager: "EntityManager",
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
        try:
            self.native_unit_of_measurement = kwargs["native_unit_of_measurement"]  # type: ignore
        except KeyError:
            self.native_unit_of_measurement = (
                self._attr_native_unit_of_measurement
                or self.DEVICECLASS_TO_UNIT_MAP.get(
                    kwargs.get("device_class", self._attr_device_class)
                )
            )

        super().__init__(channel, manager, **kwargs)

    def set_unavailable(self):
        self.device_value = None
        self.native_value = None
        super().set_unavailable()

    @override
    def update_device_value(self, device_value: int | float, /):
        if self.device_value != device_value:
            self.device_value = device_value
            self.native_value = device_value / self.device_scale
            self.flush_state()
            return True

    @override
    def update_native_value(self, native_value: int | float | None, /):
        if self.native_value != native_value:
            self.native_value = native_value
            self.flush_state()
            return True
