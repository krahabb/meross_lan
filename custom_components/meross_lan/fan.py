import typing

from homeassistant.components import fan

from . import meross_entity as me
from .helpers.namespaces import NamespaceHandler
from .merossclient import const as mc

if typing.TYPE_CHECKING:
    from .meross_device import DigestInitReturnType, MerossDevice


async def async_setup_entry(hass, config_entry, async_add_devices):
    me.platform_setup_entry(hass, config_entry, async_add_devices, fan.DOMAIN)


class MLFan(me.MerossBinaryEntity, fan.FanEntity):
    """
    Fan entity for map100 Air Purifier (or any device implementing Appliance.Control.Fan)
    """

    PLATFORM = fan.DOMAIN
    manager: "MerossDevice"

    namespace = mc.NS_APPLIANCE_CONTROL_FAN
    key_namespace = mc.KEY_FAN
    key_value = mc.KEY_SPEED

    # HA core entity attributes:
    percentage: int | None
    preset_mode: str | None = None
    preset_modes: list[str] | None = None
    speed_count: int
    supported_features: fan.FanEntityFeature = fan.FanEntityFeature.SET_SPEED

    __slots__ = (
        "percentage",
        "speed_count",
        "_fan",
        "_saved_speed",  # used to restore previous speed when turning on/off
        "_togglex",
    )

    def __init__(self, manager: "MerossDevice", channel):
        self.percentage = None
        self.speed_count = 1  # safe default: auto-inc when 'fan' payload updates
        self._fan = {}
        self._saved_speed = 1
        super().__init__(manager, channel)
        manager.register_parser(self.namespace, self)
        self._togglex = manager.register_togglex_channel(self)

    # interface: MerossToggle
    def set_unavailable(self):
        self._fan = {}
        self.percentage = None
        super().set_unavailable()

    def update_onoff(self, onoff):
        if self.is_on != onoff:
            self.is_on = onoff
            if onoff:
                self.percentage = round(self._saved_speed * 100 / self.speed_count)
            else:
                self.percentage = 0
            self.flush_state()

    # interface: fan.FanEntity
    async def async_set_percentage(self, percentage: int) -> None:
        await self.async_request_fan(round(percentage * self.speed_count / 100))

    async def async_turn_on(
        self, percentage: int | None = None, preset_mode: str | None = None, **kwargs
    ):
        if self._togglex and not self.is_on:
            await self.async_request_togglex(1)
        if percentage:
            await self.async_request_fan(round(percentage * self.speed_count / 100))
        else:
            await self.async_request_fan(self._saved_speed)

    async def async_turn_off(self, **kwargs):
        if self._togglex:
            await self.async_request_togglex(0)
        else:
            await self.async_request_fan(0)

    # interface: self
    async def async_request_fan(self, speed: int):
        payload = {mc.KEY_CHANNEL: self.channel, mc.KEY_SPEED: speed}
        if await self.manager.async_request_ack(
            mc.NS_APPLIANCE_CONTROL_FAN,
            mc.METHOD_SET,
            {mc.KEY_FAN: [payload]},
        ):
            self._parse_fan(payload)

    async def async_request_togglex(self, onoff: int):
        if await self.manager.async_request_ack(
            mc.NS_APPLIANCE_CONTROL_TOGGLEX,
            mc.METHOD_SET,
            {
                mc.KEY_TOGGLEX: {
                    mc.KEY_CHANNEL: self.channel,
                    mc.KEY_ONOFF: onoff,
                }
            },
        ):
            self.update_onoff(onoff)

    def _parse_fan(self, payload: dict):
        """payload = {"channel": 0, "speed": 3, "maxSpeed": 4}"""
        if self._fan != payload:
            self._fan.update(payload)
            payload = self._fan
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

    def _parse_togglex(self, payload: dict):
        self.update_onoff(payload[mc.KEY_ONOFF])


def digest_init_fan(device: "MerossDevice", digest) -> "DigestInitReturnType":
    """[{ "channel": 2, "speed": 3, "maxSpeed": 3 }]"""
    for channel_digest in digest:
        MLFan(device, channel_digest[mc.KEY_CHANNEL])
    handler = device.get_handler(mc.NS_APPLIANCE_CONTROL_FAN)
    return handler.parse_list, (handler,)


def namespace_init_fan(device: "MerossDevice"):
    """Special care for NS_FAN since it might have been initialized in digest_init"""
    handler = device.get_handler(mc.NS_APPLIANCE_CONTROL_FAN)
    handler.register_entity_class(MLFan)
    if mc.KEY_FAN not in device.descriptor.digest:
        # actually only map100 (so far)
        MLFan(device, 0)
        # setup a polling strategy since state is not carried in digest
        handler.polling_strategy = NamespaceHandler.async_poll_default
