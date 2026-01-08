from typing import TYPE_CHECKING, override

from homeassistant.components import fan

from .helpers import entity as me
from .helpers.namespaces import NamespaceHandler, mn
from .merossclient.protocol import const as mc

if TYPE_CHECKING:
    from typing import Final

    from .helpers.device import Device, DigestInitReturnType


async def async_setup_entry(hass, config_entry, async_add_devices):
    me.platform_setup_entry(hass, config_entry, async_add_devices, fan.DOMAIN)


class MLFan(me.MLBinaryEntity, fan.FanEntity):
    """
    Fan entity for map100 Air Purifier (or any device implementing Appliance.Control.Fan)
    """

    if TYPE_CHECKING:

        class Args(me.MLBinaryEntity.Args):
            pass

        manager: "Device"
        handler_togglex: Final[NamespaceHandler | None]

        # HA core entity attributes:
        percentage: int | None
        preset_mode: str | None
        preset_modes: list[str] | None
        speed_count: int
        supported_features: fan.FanEntityFeature

    PLATFORM = fan.DOMAIN

    ns = mn.Appliance_Control_Fan
    key_value = mc.KEY_SPEED

    # HA core entity attributes:
    preset_mode = None
    preset_modes = None
    try:
        # HA core 2024.8.0 new flags
        supported_features = (
            fan.FanEntityFeature.SET_SPEED
            | fan.FanEntityFeature.TURN_OFF
            | fan.FanEntityFeature.TURN_ON
        )
    except:
        supported_features = fan.FanEntityFeature.SET_SPEED

    _enable_turn_on_off_backwards_compatibility = False

    __slots__ = (
        "percentage",
        "speed_count",
        "_fan",
        "_saved_speed",  # used to restore previous speed when turning on/off
        "handler_togglex",
    )

    def __init__(self, manager: "Device", channel, /):
        self.percentage = None
        self.speed_count = 1  # safe default: auto-inc when 'fan' payload updates
        self._fan = None
        self._saved_speed = 1
        super().__init__(manager, channel)
        manager.register_parser_entity(self)
        self.handler_togglex = manager.register_togglex_channel(self, True)

    @override
    def set_unavailable(self):
        self._fan = None
        self.percentage = None
        super().set_unavailable()

    @override
    def update_native_value(self, onoff, /):
        if self.is_on != onoff:
            self.is_on = onoff
            # self.percentage = (
            #    round(self._saved_speed * 100 / self.speed_count) if onoff else 0
            # )
            self.flush_state()
            return True

    # interface: fan.FanEntity
    @override
    async def async_set_percentage(self, percentage: int) -> None:
        await self.handler_ns.async_set(
            {mc.KEY_SPEED: round(percentage * self.speed_count / 100)}, self, self._fan
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
        await self.handler_ns.async_set(
            {
                mc.KEY_SPEED: (
                    round(percentage * self.speed_count / 100)
                    if percentage
                    else self._saved_speed
                )
            },
            self,
            self._fan,
        )

    @override
    async def async_turn_off(self, **kwargs):
        if self.handler_togglex:
            await self.handler_togglex.async_set({mc.KEY_ONOFF: 0}, self)
        else:
            await self.handler_ns.async_set({mc.KEY_SPEED: 0}, self, self._fan)

    # interface: self
    def _parse_fan(self, payload: dict, /):
        """payload = {"channel": 0, "speed": 3, "maxSpeed": 4}"""
        if self._fan != payload:
            self._fan = payload
            speed = payload[mc.KEY_SPEED]
            if speed:
                self.is_on = True
                self._saved_speed = speed
            else:
                self.is_on = False
            self.speed_count = max(
                payload.get(mc.KEY_MAXSPEED, self.speed_count), speed
            )
            self.percentage = round(speed * 100 / self.speed_count)
            self.flush_state()


def digest_init_fan(device: "Device", digest, /) -> "DigestInitReturnType":
    """[{ "channel": 2, "speed": 3, "maxSpeed": 3 }]"""
    for channel_digest in digest:
        MLFan(device, channel_digest[mc.KEY_CHANNEL])
    handler = device.get_handler(mn.Appliance_Control_Fan)
    return handler.parse_list, (handler,)


def namespace_init_fan(device: "Device", ns=mn.Appliance_Control_Fan, /):
    """Special care for NS_FAN since it might have been initialized in digest_init"""
    if mc.KEY_FAN not in device.descriptor.digest:
        # actually only map100 (so far)
        MLFan(device, 0)
        # setup a polling strategy since state is not carried in digest
        device.get_handler(ns).polling_strategy = NamespaceHandler.async_poll_default
