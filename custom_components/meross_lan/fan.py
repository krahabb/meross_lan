from typing import TYPE_CHECKING, override

from homeassistant.components import fan

from .helpers.entity import MLToggleXEntity
from .helpers.namespaces import NamespaceHandler, mn
from .merossclient.protocol import const as mc

if TYPE_CHECKING:
    from typing import Final

    from .helpers.device import Device
    from .merossclient.protocol.types import JsonList


async def async_setup_entry(hass, config_entry, async_add_devices):
    MLToggleXEntity.platform_setup_entry(
        hass, config_entry, async_add_devices, fan.DOMAIN
    )


class MLFan(MLToggleXEntity, fan.FanEntity):
    """
    Fan entity for map100 Air Purifier (or any device implementing Appliance.Control.Fan)
    """

    if TYPE_CHECKING:

        class Args(MLToggleXEntity.Args):
            pass

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
        "_saved_speed",  # used to restore previous speed when turning on/off
    )

    def __init__(self, channel: int, device: "Device", /):
        self.percentage = None
        self.speed_count = 1  # safe default: auto-inc when 'fan' payload updates
        self._saved_speed = 1
        super().__init__(channel, device)
        device.register_parser_entity(self)

    @override
    def update_device_value(self, device_value, /) -> bool | None:
        if self.device_value != device_value:
            self.device_value = device_value
            if device_value:
                self.is_on = True
                if device_value > self.speed_count:
                    self.speed_count = device_value
                self.percentage = round(device_value * 100 / self.speed_count)
                self._saved_speed = device_value
            else:
                self.is_on = False
                self.percentage = 0
            self.flush_state()
            return True

    # interface: fan.FanEntity
    @override
    async def async_set_percentage(self, percentage: int):
        await self.async_request_value(round(percentage * self.speed_count / 100))

    @override
    async def async_turn_on(
        self, percentage: int | None = None, preset_mode: str | None = None, **kwargs
    ):
        if self.handler_togglex and not self.is_on:
            # don't propagate callback confirmation
            await self.handler_togglex.async_set(
                {mc.KEY_CHANNEL: self.channel, mc.KEY_ONOFF: 1}
            )
        await self.async_request_value(
            round(percentage * self.speed_count / 100)
            if percentage
            else self._saved_speed
        )

    @override
    async def async_turn_off(self, **kwargs):
        if self.handler_togglex:
            await self.handler_togglex.async_set({mc.KEY_ONOFF: 0}, self)
        else:
            await self.async_request_value(0)

    # interface: self
    def _parse_fan(self, payload: dict, /):
        """payload = {"channel": 0, "speed": 3, "maxSpeed": 4}"""
        if self.ns_payload != payload:
            self.ns_payload = payload
            self.speed_count = payload.get(mc.KEY_MAXSPEED, self.speed_count)
            self.update_device_value(payload[self.key_value])


def digest_init_fan(
    device: "Device", digest: "JsonList", /
) -> "Device.DigestInitReturnType":
    """[{ "channel": 2, "speed": 3, "maxSpeed": 3 }]"""
    for channel_digest in digest:
        MLFan(channel_digest[mc.KEY_CHANNEL], device)
    handler = device.get_handler(mn.Appliance_Control_Fan)
    return handler.parse_list, (handler,)


def namespace_init_fan(ns: mn.Namespace, device: "Device", /):
    """Special care for NS_FAN since it might have been initialized in digest_init"""
    if mc.KEY_FAN not in device.descriptor.digest:
        # actually only map100 (so far)
        MLFan(0, device)
        # setup a polling strategy since state is not carried in digest
        device.get_handler(ns).polling_strategy = NamespaceHandler.async_poll_default
