from typing import TYPE_CHECKING, override

from homeassistant.components import select

from .helpers import entity as mle, reverse_lookup

if TYPE_CHECKING:
    from typing import Any, ClassVar, Final, Never, NotRequired, Unpack

    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

    from .helpers.device import BaseDevice
    from .helpers.entity import ChannelType
    from .helpers.manager import EntityManager


async def async_setup_entry(
    hass: "HomeAssistant", config_entry: "ConfigEntry", async_add_devices
):
    mle.Entity.platform_setup_entry(
        hass, config_entry, async_add_devices, select.DOMAIN
    )


class SelectEntity(mle.Entity, select.SelectEntity):
    """Base 'abstract' class for both select entities representing a
    device config/option value (through ParserSelect) and
    emulated entities used to configure meross_lan (i.e. MtsTrackedSensor).
    Be sure to correctly init current_option and options in any derived class."""

    PLATFORM = select.DOMAIN

    if TYPE_CHECKING:
        # HA core entity attributes:
        current_option: str | None
        options: list[str]

        class Args(mle.Entity.Args):
            current_option: NotRequired[str | None]
            options: NotRequired[list[str]]

        def __init__(
            self,
            channel: ChannelType | None,
            device: EntityManager,
            /,
            **kwargs: Unpack[Args],
        ): ...

    _attr_entity_category = mle.Entity.EntityCategory.CONFIG

    init_options = []
    SLOTS_AUTO_INIT = ("current_option", "options")
    __slots__ = ()

    def set_unavailable(self):
        self.current_option = None
        super().set_unavailable()

    def update_option(self, option: str):
        if self.current_option != option:
            self.current_option = option
            self.flush_state()


class SelectParser(mle.ValueParser, SelectEntity):
    """
    Base class for any configurable 'list-like' parameter in the device.
    The mapping between HA entity select.options (string representation) and
    the native (likely int) device value is carried in a dedicated map
    (which also auto-updates should the device provide an unmapped value).
    """

    if TYPE_CHECKING:

        init_options_map: ClassVar[dict[Any, str]]
        options_map: dict[Any, str]

        class Args(mle.ValueParser.Args, SelectEntity.Args):
            options_map: NotRequired[dict[Any, str]]
            # options: NotRequired[Never]

    # configure initial options(map) through a class default
    init_options_map = {}
    __slots__ = ("options_map",)

    def __init__(
        self,
        channel: "ChannelType | None",
        device: "BaseDevice",
        /,
        **kwargs: "Unpack[Args]",
    ):
        self.options_map = kwargs.pop("options_map", self.init_options_map)
        kwargs["options"] = list(self.options_map.values())
        super().__init__(channel, device, **kwargs)

    @override
    def update_device_value(self, device_value, /):
        if self.device_value != device_value:
            try:
                self.current_option = self.options_map[device_value]
            except KeyError:
                if self.options_map is self.init_options_map:
                    # first time we see a new value - create an instance map
                    self.options_map = dict(self.init_options_map)
                self.options_map[device_value] = option = str(device_value)
                self.options.append(option)
                self.current_option = option
            self.device_value = device_value
            self.flush_state()
            return True

    # interface: select.SelectEntity
    @override
    async def async_select_option(self, option: str):
        await self.async_request_value(reverse_lookup(self.options_map, option))
