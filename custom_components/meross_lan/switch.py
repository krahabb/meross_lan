from abc import abstractmethod
import typing

from homeassistant.components import switch

from .helpers import entity as me
from .merossclient import extract_dict_payloads
from .merossclient.protocol import const as mc, namespaces as mn

if typing.TYPE_CHECKING:
    from typing import Unpack

    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

    from .helpers.device import BaseDevice, Device, DigestInitReturnType


async def async_setup_entry(
    hass: "HomeAssistant", config_entry: "ConfigEntry", async_add_devices
):
    me.platform_setup_entry(hass, config_entry, async_add_devices, switch.DOMAIN)


class MLSwitchBase(me.MLBinaryEntity, switch.SwitchEntity):
    """
    Base (almost abstract) entity for switches. This has 2 main implementations:
    - MLSwitch: switch representing some device feature (an actual output or a config option)
    - MLConfigSwitch: switch used to configure a meross_lan feature/option
    """

    PLATFORM = switch.DOMAIN
    DeviceClass = switch.SwitchDeviceClass


class MLEmulatedSwitch(me.MEPartialAvailableMixin, MLSwitchBase):
    """
    Switch entity not related to any device feature but used to configure
    behaviors for meross_lan entities.
    """

    # HA core entity attributes:
    entity_category = MLSwitchBase.EntityCategory.CONFIG

    def __init__(
        self,
        manager: "BaseDevice",
        channel: object,
        entitykey: str | None = None,
        /,
        **kwargs: "Unpack[MLSwitchBase.Args]",
    ):
        super().__init__(
            manager,
            channel,
            entitykey,
            MLSwitchBase.DeviceClass.SWITCH,
            **kwargs,
        )

    async def async_added_to_hass(self):
        await super().async_added_to_hass()
        with self.exception_warning("restoring previous state"):
            if last_state := await self.get_last_state_available():
                self.is_on = last_state.state == self.hac.STATE_ON

    async def async_turn_on(self, **kwargs):
        self.update_onoff(1)

    async def async_turn_off(self, **kwargs):
        self.update_onoff(0)


class MLSwitch(MLSwitchBase):
    """
    Generic HA switch: could either be a physical outlet or another binary setting
    of the device (see various config switches)
    Switches are sometimes hybrid and their message dispatching is not 'set in stone'
    since the status updates are likely managed in higher level implementations or so.
    This class needs to be mixed in with any of the me.MENoChannelMixin,
    me.MEDictChannelMixin, MEListChannelMixin in order to actually define the
    implementation of the protocol message payload for 'SET' commands
    """

    @abstractmethod
    async def async_request_value(self, device_value, /):
        raise NotImplementedError("'async_request_value' needs to be overriden")

    async def async_turn_on(self, **kwargs):
        if await self.async_request_value(1):
            self.update_onoff(1)

    async def async_turn_off(self, **kwargs):
        if await self.async_request_value(0):
            self.update_onoff(0)


class PhysicalLockSwitch(me.MEListChannelMixin, MLSwitch):

    ns = mn.Appliance_Control_PhysicalLock

    # HA core entity attributes:
    entity_category = MLSwitch.EntityCategory.CONFIG

    def __init__(self, manager: "Device"):
        # right now we expect only 1 entity on channel == 0 (whatever)
        super().__init__(manager, 0, mc.KEY_LOCK, self.DeviceClass.SWITCH)
        manager.register_parser_entity(self)


class MLToggle(me.MENoChannelMixin, MLSwitch):

    ns = mn.Appliance_Control_Toggle

    def __init__(self, manager: "Device", /):
        # 2024-03-13: passing entitykey="0" instead of channel in order
        # to mantain unique_id compatibility with installations but
        # updating to new toggle entity model (where channel is None for this entity type)
        super().__init__(manager, None, "0", MLSwitch.DeviceClass.OUTLET)
        manager.register_parser_entity(self)


def digest_init_toggle(device: "Device", digest: dict, /) -> "DigestInitReturnType":
    """{"onoff": 0, "lmTime": 1645391086}"""
    MLToggle(device)
    handler = device.namespace_handlers[mn.Appliance_Control_Toggle.name]
    return handler.parse_generic, (handler,)


class MLToggleX(me.MEDictChannelMixin, MLSwitch):

    ns = mn.Appliance_Control_ToggleX

    def __init__(self, manager: "Device", channel: object, /):
        super().__init__(manager, channel, None, MLSwitch.DeviceClass.OUTLET)
        manager.register_parser_entity(self)


def digest_init_togglex(
    device: "Device", togglex_digest: list, /
) -> "DigestInitReturnType":
    # We don't initialize every switch/ToggleX here since the digest reported channels
    # might be mapped to more specialized entities:
    # this is true for lights (MLLight), garageDoor (MLGarage) and fan (MLFan) though
    # and maybe some more others.
    # In general, it is not very clear how and when these ToggleX entities are really needed
    # so we have some euristics in place to fix 'this and that'.
    # The general rule is to let the togglex namespace/channel be managed by the
    # aforementioned specialized entity, while, if no channel match exists, create a disabled
    # (by default) switch entity. When  switches are really switches (like mssXXX series) instead,
    # we'll setup proper MLToggleX (this is detected by the fact no specialized entity exists in
    # device definition)

    channels = {digest[mc.KEY_CHANNEL] for digest in togglex_digest}

    digest = device.descriptor.digest

    for key_digest in (mc.KEY_FAN, mc.KEY_GARAGEDOOR, mc.KEY_LIGHT):
        if key_digest in digest:
            for digest_channel in extract_dict_payloads(digest[key_digest]):
                channel = digest_channel.get(mc.KEY_CHANNEL)
                if channel in channels:
                    channels.remove(channel)

    # the fan controller 'map100' doesn't expose a fan in digest but it has one at channel 0
    if (mn.Appliance_Control_Fan.name in device.descriptor.ability) and (
        mc.KEY_FAN not in digest
    ):
        if 0 in channels:
            channels.remove(0)

    for channel in channels:
        MLToggleX(device, channel)

    ns = mn.Appliance_Control_ToggleX
    handler = device.get_handler(ns)
    handler.register_entity_class(MLToggleX)
    if device.descriptor.is_refoss:
        handler.polling_request = (
            ns.name,
            mc.METHOD_GET,
            {ns.key: {mc.KEY_CHANNEL: 65535}},
        )
    return handler.parse_list, (handler,)
