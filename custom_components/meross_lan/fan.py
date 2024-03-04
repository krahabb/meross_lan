import typing

from homeassistant.components import fan

from . import meross_entity as me
from .helpers.namespaces import NamespaceHandler, PollingStrategy
from .merossclient import const as mc  # mEROSS cONST

if typing.TYPE_CHECKING:
    from .helpers.namespaces import DigestParseFunc
    from .meross_device import MerossDevice


async def async_setup_entry(hass, config_entry, async_add_devices):
    me.platform_setup_entry(hass, config_entry, async_add_devices, fan.DOMAIN)


class MLFan(me.MerossToggle, fan.FanEntity):
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
        "sensor_filtermaintenance",
    )

    def __init__(self, manager: "MerossDevice", channel):
        self.percentage = None
        self.speed_count = 0
        self._fan = {}
        self._saved_speed = 1
        super().__init__(manager, channel)
        manager.register_parser(self.namespace, self)
        if mc.NS_APPLIANCE_CONTROL_TOGGLEX in manager.descriptor.ability:
            manager.register_parser(mc.NS_APPLIANCE_CONTROL_TOGGLEX, self)

    # interface: MerossToggle
    def set_unavailable(self):
        self._fan = {}
        self.percentage = None
        super().set_unavailable()

    async def async_turn_off(self):
        await self.async_request_fan(0)

    def _parse_togglex(self, payload: dict):
        pass

    # interface: fan.FanEntity
    async def async_set_percentage(self, percentage: int) -> None:
        await self.async_request_fan(round(percentage * self.speed_count / 100))

    async def async_turn_on(
        self, percentage: int | None = None, preset_mode: str | None = None, **kwargs
    ):
        if percentage:
            await self.async_set_percentage(percentage)
        else:
            await self.async_request_fan(self._saved_speed)

    # interface: self
    async def async_request_fan(self, speed: int):
        payload = {self.key_channel: self.channel, self.key_value: speed}
        if await self.manager.async_request_ack(
            self.namespace,
            mc.METHOD_SET,
            {self.key_namespace: [payload]},
        ):
            self._parse_fan(payload)

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
            self.speed_count = maxspeed = payload[mc.KEY_MAXSPEED]
            self.percentage = round(speed * 100 / maxspeed)
            self.flush_state()


class FanNamespaceHandler(NamespaceHandler):

    def __init__(self, device: "MerossDevice"):
        super().__init__(
            device,
            mc.NS_APPLIANCE_CONTROL_FAN,
            entity_class=MLFan,
        )
        if mc.KEY_FAN not in device.descriptor.digest:
            # actually only map100 (so far)
            MLFan(device, 0)
            # setup a polling strategy since state is not carried in digest
            PollingStrategy(
                device,
                mc.NS_APPLIANCE_CONTROL_FAN,
                payload=[{mc.KEY_CHANNEL: 0}],
                item_count=1,
            )


def digest_init_fan(device: "MerossDevice", digest) -> "DigestParseFunc":
    """[{ "channel": 2, "speed": 3, "maxSpeed": 3 }]"""
    for channel_digest in digest:
        MLFan(device, channel_digest[mc.KEY_CHANNEL])
    # mc.NS_APPLIANCE_CONTROL_FAN should already be there since the namespace
    # handlers dict has been initialized before digest
    return device.namespace_handlers[mc.NS_APPLIANCE_CONTROL_FAN].parse_list
