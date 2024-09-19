"""
 Base-Common behaviour for all Meross-LAN entities

 actual HA custom platform entities will be derived like this:
 MLSwitch(MerossToggle, SwitchEntity)

 we also try to 'commonize' HA core symbols import in order to better manage
 versioning
"""

from functools import partial
import typing

try:
    from homeassistant.components.recorder import get_instance as r_get_instance
    from homeassistant.components.recorder.history import get_last_state_changes
except ImportError:
    get_last_state_changes = None

from homeassistant.helpers.entity import Entity, EntityCategory

from .helpers import Loggable
from .helpers.manager import ApiProfile
from .helpers.namespaces import NamespaceParser
from .merossclient import const as mc, namespaces as mn

if typing.TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

    from .helpers.manager import EntityManager
    from .helpers.namespaces import NamespaceHandler
    from .meross_device import MerossDeviceBase

    # optional arguments for MerossEntity init
    class MerossEntityArgs(typing.TypedDict):
        name: typing.NotRequired[str]
        translation_key: typing.NotRequired[str]

    # optional arguments for MerossBinaryEntity init
    class MerossBinaryEntityArgs(MerossEntityArgs):
        device_value: typing.NotRequired[typing.Any]

    # optional arguments for  MerossNumericEntity init
    class MerossNumericEntityArgs(MerossEntityArgs):
        device_value: typing.NotRequired[int | float]
        device_scale: typing.NotRequired[int | float]
        native_unit_of_measurement: typing.NotRequired[str]
        suggested_display_precision: typing.NotRequired[int]


class MerossEntity(
    NamespaceParser, Loggable, Entity if typing.TYPE_CHECKING else object
):
    """
    Mixin style base class for all of the entity platform(s)
    This class must prepend the HA entity class in our custom
    entity classe definitions like:
    from homeassistant.components.switch import Switch
    class MyCustomSwitch(MerossEntity, Switch)
    """

    EntityCategory = EntityCategory

    PLATFORM: typing.ClassVar[str]

    is_diagnostic: typing.ClassVar[bool] = False
    """Tells if this entity has been created as part of the 'create_diagnostic_entities' config"""

    state_callbacks: set[typing.Callable] | None
    # These 'placeholder' definitions support generalization of
    # Meross protocol message build/parsing when related to the
    # current entity. These are usually relevant when this entity
    # is strictly related to a namespace payload key value.
    # See MLConfigNumber or MerossToggle as basic implementations
    # supporting this semantic. They're generally set as class definitions
    # in inherited entities but could nonetheless be set 'per instance'.
    # These also come handy when generalizing parsing of received payloads
    # for simple enough entities (like sensors, numbers or switches)
    ns: mn.Namespace
    key_value: str = mc.KEY_VALUE

    # HA core entity attributes:
    # These are constants throughout our model
    force_update: typing.Final[bool] = False
    has_entity_name: typing.Final[bool] = True
    should_poll: typing.Final[bool] = False
    # These may be customized here and there per class
    _attr_available: typing.ClassVar[bool] = False
    # These may be customized here and there per class or instance
    assumed_state: bool = False
    entity_category: EntityCategory | None = None
    entity_registry_enabled_default: bool = True
    extra_state_attributes: dict[str, object] = {}
    icon: str | None = None
    translation_key: str | None = None
    # These are actually per instance
    available: bool
    device_class: typing.Final[object | str | None]
    name: str | None
    suggested_object_id: str | None
    unique_id: str

    # used to speed-up checks if entity is enabled and loaded
    _hass_connected: bool

    __slots__ = (
        "manager",
        "channel",
        "entitykey",
        "state_callbacks",
        "available",
        "device_class",
        "name",
        "suggested_object_id",
        "unique_id",
        "_hass_connected",
    )

    def __init__(
        self,
        manager: "EntityManager",
        channel: object | None,
        entitykey: str | None = None,
        device_class: object | str | None = None,
        **kwargs: "typing.Unpack[MerossEntityArgs]",
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
        id = (
            channel
            if entitykey is None
            else entitykey if channel is None else f"{channel}_{entitykey}"
        )
        self.manager = manager
        self.channel = channel
        self.entitykey = entitykey
        self.state_callbacks = None
        self.available = self._attr_available or manager.online
        self.device_class = device_class
        Loggable.__init__(self, id, logger=manager)
        # init before raising exceptions so that the Loggable is
        # setup before any exception is raised
        if id is None:
            raise AssertionError(
                "provide at least channel or entitykey (cannot be 'None' together)"
            )
        if id in manager.entities:
            raise AssertionError(f"id:{id} is not unique inside manager.entities")

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

        self._hass_connected = False
        manager.entities[id] = self
        async_add_devices = manager.platforms.setdefault(self.PLATFORM)
        if async_add_devices:
            async_add_devices([self])

    # interface: Entity
    @property
    def device_info(self):
        return self.manager.deviceentry_id

    async def async_added_to_hass(self):
        self.log(self.VERBOSE, "Added to HomeAssistant")
        self._hass_connected = True
        return await super().async_added_to_hass()

    async def async_will_remove_from_hass(self):
        self.log(self.VERBOSE, "Removed from HomeAssistant")
        self._hass_connected = False
        return await super().async_will_remove_from_hass()

    # interface: self
    async def async_shutdown(self):
        await NamespaceParser.async_shutdown(self)
        self.state_callbacks = None
        self.manager.entities.pop(self.id)
        self.manager: "EntityManager" = None  # type: ignore

    def register_state_callback(self, state_callback: typing.Callable):
        if not self.state_callbacks:
            self.state_callbacks = set()
        self.state_callbacks.add(state_callback)

    def flush_state(self):
        """Actually commits a state change to HA."""
        if self.state_callbacks:
            for state_callback in self.state_callbacks:
                state_callback()
        if self._hass_connected:
            self.async_write_ha_state()

    def set_available(self):
        self.available = True
        # we don't flush here since we'll wait for actual device readings

    def set_unavailable(self):
        if self.available:
            self.available = False
            self.flush_state()

    def update_device_value(self, device_value):
        """This is a stub definition. It will be called by _parse (when namespace dispatching
        is configured so) or directly as a short path inside other parsers to forward the
        incoming device value to the underlyinh HA entity state."""
        raise NotImplementedError("Called 'update_device_value' on wrong entity type")

    def update_native_value(self, native_value):
        """This is a stub definition. It will usually be called by update_device_value
        with the result of the conversion from the incoming device value (from Meross protocol)
        to the proper HA type/value for the entity class."""
        raise NotImplementedError("Called 'update_native_value' on wrong entity type")

    async def async_request_value(self, device_value):
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
                    MerossEntity.hac.STATE_UNKNOWN,
                    MerossEntity.hac.STATE_UNAVAILABLE,
                ):
                    return state
        return None

    def _generate_unique_id(self):
        return self.manager.generate_unique_id(self)

    # interface: NamespaceParser
    def _parse(self, payload: dict):
        """Default parsing for entities. Set the proper
        key_value in class/instance definition to make it work."""
        self.update_device_value(payload[self.key_value])

class MENoChannelMixin(MerossEntity if typing.TYPE_CHECKING else object):
    """
    Implementation for protocol method 'SET' on entities/namespaces not backed by a channel.
    Actual examples: Appliance.Control.Toggle, Appliance.GarageDoor.Config, and so on..
    """

    manager: "MerossDeviceBase"

    # interface: MerossEntity
    async def async_request_value(self, device_value):
        """sends the actual request to the device. this is likely to be overloaded"""
        ns = self.ns
        return await self.manager.async_request_ack(
            ns.name,
            mc.METHOD_SET,
            {ns.key: {self.key_value: device_value}},
        )


class MEDictChannelMixin(MerossEntity if typing.TYPE_CHECKING else object):
    """
    Implementation for protocol method 'SET' on entities/namespaces backed by a channel
    where the command payload must be enclosed in a plain dict (without enclosing list).
    Actual examples: Appliance.Control.ToggleX, Appliance.RollerShutter.Config, and so on..
    """

    manager: "MerossDeviceBase"

    # interface: MerossEntity
    async def async_request_value(self, device_value):
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


class MEListChannelMixin(MerossEntity if typing.TYPE_CHECKING else object):
    """
    Implementation for protocol method 'SET' on entities/namespaces backed by a channel
    where the command payload must be enclosed in a list
    Actual examples: Appliance.Control.ToggleX and so on..
    """

    manager: "MerossDeviceBase"

    # interface: MerossEntity
    async def async_request_value(self, device_value):
        """sends the actual request to the device. this is likely to be overloaded"""
        ns = self.ns
        return await self.manager.async_request_ack(
            ns.name,
            mc.METHOD_SET,
            {ns.key: [{ns.key_channel: self.channel, self.key_value: device_value}]},
        )


class MEAutoChannelMixin(MerossEntity if typing.TYPE_CHECKING else object):
    """
    Implementation for protocol method 'SET' on entities/namespaces backed by a channel
    where the command payload could be either a list or a dict. This mixin actually
    tries to learn the correct format at runtime buy 'sensing' it
    Actual examples: Appliance.Control.ToggleX
    """

    manager: "MerossDeviceBase"

    _set_format = None

    # interface: MerossEntity
    async def async_request_value(self, device_value):
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


class MEAlwaysAvailableMixin(MerossEntity if typing.TYPE_CHECKING else object):
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


class MEPartialAvailableMixin(MerossEntity if typing.TYPE_CHECKING else object):
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


class MerossBinaryEntity(MerossEntity):
    """Partially abstract common base class for ToggleEntity and BinarySensor.
    The initializer is skipped."""

    key_value = mc.KEY_ONOFF

    # HA core entity attributes:
    is_on: bool | None

    __slots__ = ("is_on",)

    def __init__(
        self,
        manager: "MerossDeviceBase",
        channel: object,
        entitykey: str | None = None,
        device_class: object | None = None,
        **kwargs: "typing.Unpack[MerossBinaryEntityArgs]",
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


class MerossNumericEntity(MerossEntity):
    """Common base class for (numeric) sensors and numbers."""

    DEVICECLASS_TO_UNIT_MAP: typing.ClassVar[dict[object | None, str | None]]
    """To be init in derived classes with their DeviceClass own types"""
    _attr_device_scale: int | float = 1
    """
    Provides a class initializer default for device_scale
    """
    device_value: int | float | None
    """The 'native' device value carried in protocol messages"""

    # HA core entity attributes:
    native_value: int | float | None
    native_unit_of_measurement: str | None
    # these are core attributes only for Sensor entity but we're
    # trying emulate that kind of same behavior for Number
    _attr_suggested_display_precision: typing.ClassVar[int | None] = None
    suggested_display_precision: int | None

    __slots__ = (
        "device_scale",
        "device_value",
        "native_value",
        "native_unit_of_measurement",
        "suggested_display_precision",
    )

    def __init__(
        self,
        manager: "EntityManager",
        channel: object,
        entitykey: str | None = None,
        device_class: object | None = None,
        **kwargs: "typing.Unpack[MerossNumericEntityArgs]",
    ):
        self.suggested_display_precision = kwargs.pop(
            "suggested_display_precision", self._attr_suggested_display_precision
        )
        self.device_scale = kwargs.pop("device_scale", self._attr_device_scale)
        if "device_value" in kwargs:
            self.device_value = kwargs.pop("device_value")
            if self.suggested_display_precision is None:
                self.native_value = self.device_value / self.device_scale
            else:
                self.native_value = round(
                    self.device_value / self.device_scale,
                    self.suggested_display_precision,
                )
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

    def update_device_value(self, device_value: int | float):
        if self.device_value != device_value:
            self.device_value = device_value
            if self.suggested_display_precision is None:
                self.native_value = device_value / self.device_scale
            else:
                self.native_value = round(
                    device_value / self.device_scale, self.suggested_display_precision
                )
            self.flush_state()
            return True

    def update_native_value(self, native_value: int | float):
        if self.suggested_display_precision is not None:
            native_value = round(native_value, self.suggested_display_precision)
        if self.native_value != native_value:
            self.native_value = native_value
            self.flush_state()
            return True


#
# helper functions to 'commonize' platform setup
#
def platform_setup_entry(
    hass: "HomeAssistant", config_entry: "ConfigEntry", async_add_devices, platform: str
):
    manager = ApiProfile.managers[config_entry.entry_id]
    manager.log(manager.DEBUG, "platform_setup_entry { platform: %s }", platform)
    manager.platforms[platform] = async_add_devices
    async_add_devices(manager.managed_entities(platform))
