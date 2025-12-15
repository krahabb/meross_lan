from typing import TYPE_CHECKING

from homeassistant.components import select

from .helpers import entity as me, reverse_lookup

if TYPE_CHECKING:
    from typing import Any, ClassVar, Final, Unpack

    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

    from .helpers.device import BaseDevice


async def async_setup_entry(
    hass: "HomeAssistant", config_entry: "ConfigEntry", async_add_devices
):
    me.platform_setup_entry(hass, config_entry, async_add_devices, select.DOMAIN)


class MLSelect(me.MLEntity, select.SelectEntity):
    """Base 'abstract' class for both select entities representing a
    device config/option value (through MLConfigSelect) and
    emulated entities used to configure meross_lan (i.e. MtsTrackedSensor).
    Be sure to correctly init current_option and options in any derived class."""

    PLATFORM = select.DOMAIN

    if TYPE_CHECKING:
        # HA core entity attributes:
        current_option: str | None
        options: list[str]

    entity_category = me.MLEntity.EntityCategory.CONFIG

    __slots__ = (
        "current_option",
        "options",
    )

    def set_unavailable(self):
        self.current_option = None
        super().set_unavailable()

    def update_option(self, option: str):
        if self.current_option != option:
            self.current_option = option
            self.flush_state()


class MLConfigSelect(MLSelect):
    """
    Base class for any configurable 'list-like' parameter in the device.
    This works much-like MLConfigNumber but does not provide a default
    async_request_value so this needs to be defined in actual implementations.
    The mapping between HA entity select.options (string representation) and
    the native (likely int) device value is carried in a dedicated map
    (which also auto-updates should the device provide an unmapped value).
    """

    if TYPE_CHECKING:
        OPTIONS_MAP: ClassVar[dict[Any, str]]
        options_map: dict[Any, str]

        device_value: Any

    # configure initial options(map) through a class default
    OPTIONS_MAP = {}

    __slots__ = (
        "options_map",
        "device_value",
    )

    def __init__(
        self,
        manager: "BaseDevice",
        channel: object | None,
        entitykey: str | None = None,
        **kwargs: "Unpack[MLSelect.Args]",
    ):
        self.current_option = None
        self.options_map = self.OPTIONS_MAP
        self.options = list(self.options_map.values())
        self.device_value = None
        super().__init__(manager, channel, entitykey, None, **kwargs)

    def set_unavailable(self):
        self.device_value = None
        return super().set_unavailable()

    def update_device_value(self, device_value, /):
        if self.device_value != device_value:
            try:
                self.update_option(self.options_map[device_value])
            except KeyError:
                if self.options_map is self.OPTIONS_MAP:
                    # first time we see a new value - create an instance map
                    self.options_map = dict(self.OPTIONS_MAP)
                self.options_map[device_value] = option = str(device_value)
                self.options.append(option)
                self.update_option(option)

            self.device_value = device_value
            return True

    # interface: select.SelectEntity
    async def async_select_option(self, option: str):
        device_value = reverse_lookup(self.options_map, option)
        if await self.async_request_value(device_value):
            self.update_device_value(device_value)
