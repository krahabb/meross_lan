import datetime as dt
from typing import TYPE_CHECKING

from homeassistant.components import time

from .helpers.entity import Entity

if TYPE_CHECKING:
    from typing import Any, NotRequired, Unpack

    from .helpers.manager import ConfigEntryManager


async def async_setup_entry(hass, config_entry, async_add_devices):
    Entity.platform_setup_entry(hass, config_entry, async_add_devices, time.DOMAIN)


class TimeEntity(Entity, time.TimeEntity):
    """
    This first implementation was mostly tailored to suit mts300 'fan hold time' feature
    We'll maybe generalize this platform when the need comes.
    After testing I found this entity a little useless since it just work for 'time of day'
    and not very well for time durations. The code is left for reference in the future.
    """

    if TYPE_CHECKING:

        class Args(Entity.Args):
            native_value: NotRequired[dt.time]
            device_scale: NotRequired[int | float]
            device_value_disabled: NotRequired[int]
            device_value: NotRequired[int]

        device_scale: int | float
        device_value_disabled: int
        device_value: int | None

        # HA core entity attributes:
        native_value: dt.time | None

    PLATFORM = time.DOMAIN

    # HA core entity attributes:
    _attr_entity_category = Entity.EntityCategory.CONFIG

    __slots__ = (
        "device_scale",
        "device_value_disabled",
        # HA core
        "native_value",
    )

    def __init__(
        self,
        channel: "Any | None",
        manager: "ConfigEntryManager",
        **kwargs: "Unpack[Args]",
    ):
        self.native_value = kwargs.pop("native_value", None)
        self.device_scale = kwargs.pop("device_scale", 1)
        self.device_value_disabled = kwargs.pop("device_value_disabled", 0)
        super().__init__(channel, manager, **kwargs)

    def set_unavailable(self):
        self.native_value = None
        super().set_unavailable()

    async def async_set_value(self, value: dt.time):
        # just a basic 'emulated' behavior
        self.update_native_value(value)

    # interface: self
    def update_device_value(self, device_value: int | None):
        if self.device_value != device_value:
            if (device_value == self.device_value_disabled) or (device_value is None):
                self.native_value = None
            else:
                second = round(device_value / self.device_scale)
                if 0 <= second < 86400:
                    hour = second // 3600
                    second -= hour * 3600
                    minute = second // 60
                    second -= minute * 60
                    self.native_value = dt.time(hour, minute, second)
                else:
                    self.native_value = None
            self.device_value = device_value
            self.flush_state()
            return True

    def update_native_value(self, native_value: dt.time | None):
        if self.native_value != native_value:
            self.native_value = native_value
            self.flush_state()
            return True
