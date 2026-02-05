from typing import TYPE_CHECKING

from homeassistant.components import cover

from .helpers import entity as me

if TYPE_CHECKING:
    import asyncio
    from typing import ClassVar, NotRequired

    from .helpers.device import Device


async def async_setup_entry(hass, config_entry, async_add_devices):
    me.platform_setup_entry(hass, config_entry, async_add_devices, cover.DOMAIN)


class MLCover(me.MLEntity, cover.CoverEntity):

    if TYPE_CHECKING:

        class Args(me.MLEntity.Args):
            device_class: NotRequired[cover.CoverDeviceClass | None]

        manager: "Device"

        # HA core entity attributes:
        _attr_device_class: ClassVar[cover.CoverDeviceClass | None]
        is_closed: bool | None
        is_closing: bool
        is_opening: bool

        _transition_unsub: asyncio.TimerHandle | None
        _transition_end_unsub: asyncio.TimerHandle | None

    PLATFORM = cover.DOMAIN

    try:
        CoverState = cover.CoverState  # type: ignore[attr-defined]
    except AttributeError:
        import enum

        class CoverState(enum.StrEnum):
            """State of Cover entities."""

            CLOSED = "closed"
            CLOSING = "closing"
            OPEN = "open"
            OPENING = "opening"

    DeviceClass = cover.CoverDeviceClass
    EntityFeature = cover.CoverEntityFeature

    __slots__ = (
        "is_closed",
        "is_closing",
        "is_opening",
        "_transition_unsub",
        "_transition_end_unsub",
    )

    def __init__(self, channel: int, manager: "Device", /):
        self.is_closed = None
        self.is_closing = False
        self.is_opening = False
        self._transition_unsub = None
        self._transition_end_unsub = None
        super().__init__(channel, manager)

    # interface: MLEntity
    async def async_shutdown(self):
        self._transition_cancel()
        await super().async_shutdown()

    async def async_will_remove_from_hass(self):
        self._transition_cancel()
        await super().async_will_remove_from_hass()

    def set_unavailable(self):
        self._transition_cancel()
        self.is_closed = None
        self.is_closing = False
        self.is_opening = False
        super().set_unavailable()

    # interface: self
    def _transition_cancel(self):
        if self._transition_end_unsub:
            self._transition_end_unsub.cancel()
            self._transition_end_unsub = None
        if self._transition_unsub:
            self._transition_unsub.cancel()
            self._transition_unsub = None
