from typing import TYPE_CHECKING, override

from homeassistant.components import fan

from .helpers.entity import ToggleXParser
from .merossclient.protocol import const as mc


class Fan(ToggleXParser, fan.FanEntity):
    """
    Fan entity for map100 Air Purifier (or any device implementing Appliance.Control.Fan)
    """

    if TYPE_CHECKING:

        # HA core entity attributes:
        percentage: int | None
        speed_count: int
        _attr_supported_features: fan.FanEntityFeature

    PLATFORM = fan.DOMAIN

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

    init_speed_count = 1
    init__saved_speed = 1
    SLOTS_AUTO_INIT = ("percentage", "speed_count", "_saved_speed")
    __slots__ = ()

    # interface: fan.FanEntity
    @override
    async def async_set_percentage(self, percentage: int):
        await self.async_request_parse_ex(
            {mc.KEY_SPEED: round(percentage * self.speed_count / 100)}
        )

    @override
    async def async_turn_on(
        self, percentage: int | None = None, preset_mode: str | None = None, **kwargs
    ):
        if self.handler_togglex and not self.is_on:
            # don't propagate callback confirmation
            await self.handler_togglex.async_set(
                {mc.KEY_CHANNEL: self.channel, mc.KEY_ONOFF: 1}
            )
        await self.async_request_parse_ex(
            {
                mc.KEY_SPEED: (
                    round(percentage * self.speed_count / 100)
                    if percentage
                    else self._saved_speed
                )
            }
        )

    @override
    async def async_turn_off(self, **kwargs):
        if self.handler_togglex:
            await self.handler_togglex.async_set({mc.KEY_ONOFF: 0}, self)
        else:
            await self.async_request_parse_ex({mc.KEY_SPEED: 0})

    # interface: self
    def _parse(self, payload: dict, /):
        """payload = {"channel": 0, "speed": 3, "maxSpeed": 4}"""
        if self.ns_payload != payload:
            self.ns_payload = payload
            self.speed_count = payload.get(mc.KEY_MAXSPEED, self.speed_count)
            speed = payload[mc.KEY_SPEED]
            if speed:
                self.is_on = True
                if speed > self.speed_count:
                    self.speed_count = speed
                self.percentage = round(speed * 100 / self.speed_count)
                self._saved_speed = speed
            else:
                self.is_on = False
                self.percentage = 0
            self.flush_state()


async_setup_entry = Fan.platform_setup_entry
