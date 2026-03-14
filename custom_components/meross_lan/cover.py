from typing import TYPE_CHECKING

from homeassistant.components import cover

from .helpers.entity import ParserEntity

if TYPE_CHECKING:
    from typing import ClassVar, Final, NotRequired

    from .helpers.device import Device


async def async_setup_entry(hass, config_entry, async_add_devices):
    ParserEntity.platform_setup_entry(
        hass, config_entry, async_add_devices, cover.DOMAIN
    )


class Cover(ParserEntity, cover.CoverEntity):

    if TYPE_CHECKING:

        parent: Final[Device]  # type: ignore[override]
        channel: Final[int]  # type: ignore[override]
        # HA core entity attributes:
        _attr_device_class: ClassVar[cover.CoverDeviceClass | None]
        is_closed: bool | None
        is_closing: bool | None
        is_opening: bool | None

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

    SLOTS_AUTO_INIT = (
        "is_closed",
        "is_closing",
        "is_opening",
    )
    __slots__ = ()

    async def async_will_remove_from_hass(self):
        self._transition_cancel()
        await super().async_will_remove_from_hass()

    def set_unavailable(self):
        self._transition_cancel()
        self.is_closed = None
        self.is_closing = None
        self.is_opening = None
        super().set_unavailable()

    # interface: self
    def _transition_cancel(self):
        self.cancel_callback(self._transition_callback)
        self.cancel_callback(self._async_transition_end_callback)

    def _transition_callback(self):
        raise NotImplementedError

    async def _async_transition_end_callback(self, /):
        raise NotImplementedError
