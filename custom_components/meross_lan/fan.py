from typing import TYPE_CHECKING, override

from homeassistant.components import fan

from .helpers.entity import ToggleXParser
from .merossclient.protocol import const as mc

if TYPE_CHECKING:
    from typing import Final

    from .merossclient.protocol import types as mt


class Fan(ToggleXParser, fan.FanEntity):
    """
    Fan entity for any device implementing Appliance.Control.Fan
    Currently:
    - map100
    - mfc100
    """

    if TYPE_CHECKING:
        REFRESH_DELAY: Final
        ns_value: mt.control.Fan
        # HA core entity attributes:
        percentage: int | None
        speed_count: int
        _attr_supported_features: fan.FanEntityFeature

    PLATFORM = fan.DOMAIN

    # After issuing any request idle a bit and then perform a state poll
    # since, overlapped requests, even tho ack'd, are silently discarded while
    # the device is performing its 'long' transaction(s). This in turn would lead
    # to HA believing in a state which was not performed (thus desync).
    # Give (at least) 5 seconds since that's the time (worst case) the device
    # needs to actually complete its own 'correct' transaction
    REFRESH_DELAY = 5

    # HA core entity attributes:
    try:
        # HA core 2024.8.0 new flags
        _attr_supported_features = (
            fan.FanEntityFeature.SET_SPEED
            | fan.FanEntityFeature.TURN_OFF
            | fan.FanEntityFeature.TURN_ON
        )
    except:
        _attr_supported_features = fan.FanEntityFeature.SET_SPEED

    _enable_turn_on_off_backwards_compatibility = False

    _INIT_speed_count = 1
    _INIT__saved_speed = 1
    _AUTO_INIT = ("percentage", "speed_count", "_saved_speed")
    __slots__ = ToggleXParser._calc_slots()

    @override
    def set_unavailable(self):
        self.cancel_callback(self.__async_refresh)
        super().set_unavailable()

    # interface: fan.FanEntity
    @override
    async def async_set_percentage(self, percentage: int):
        await self.async_request_parse_ex(
            {mc.KEY_SPEED: round(percentage * self.speed_count / 100)}
        )
        self.schedule_callback(self.REFRESH_DELAY, self.__async_refresh)

    @override
    async def async_turn_on(
        self, percentage: int | None = None, preset_mode: str | None = None, **kwargs
    ):
        if self.handler_togglex and not self.is_on:
            # don't propagate callback confirmation so that we'll flush once after set_percentage
            await self.handler_togglex.async_set(self.index | {mc.KEY_ONOFF: 1})
            self.is_on = True
            self.ns_value[mc.KEY_SPEED] = 0  # force flushing on next state callback
        await self.async_set_percentage(
            percentage or round(self._saved_speed * 100 / self.speed_count)
        )

    @override
    async def async_turn_off(self, **kwargs):
        if self.handler_togglex:
            # don't propagate callback confirmation so that we'll flush once after set_percentage
            await self.handler_togglex.async_set(self.index | {mc.KEY_ONOFF: 0})
            self.is_on = False
        await self.async_set_percentage(0)

    @override
    def __call__(self, payload: "mt.control.Fan", /):
        """payload = {"channel": 0, "speed": 3, "maxSpeed": 4}"""
        if self.ns_value != payload:
            self.ns_value = payload
            self.speed_count = payload.get(mc.KEY_MAXSPEED, self.speed_count)
            speed = payload[mc.KEY_SPEED]
            if speed:
                if speed > self.speed_count:
                    self.speed_count = speed
                self.percentage = round(speed * 100 / self.speed_count)
                self._saved_speed = speed
                if not self.handler_togglex:
                    self.is_on = True
            else:
                self.percentage = 0
                if not self.handler_togglex:
                    self.is_on = False
            self.flush_state()

    async def __async_refresh(self, /):
        await self.handler_ns.async_get(self.index)
        if self.handler_togglex:
            await self.handler_togglex.async_get(self.index)


async_setup_entry = Fan.platform_setup_entry
