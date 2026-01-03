from typing import TYPE_CHECKING, override

from homeassistant.components import switch

from .helpers import entity as me
from .helpers.namespaces import EntityNamespaceMixin, mc, mn
from .merossclient import extract_dict_payloads

if TYPE_CHECKING:
    from typing import ClassVar, NotRequired, Unpack

    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

    from .helpers.device import BaseDevice, Device, DigestInitReturnType


async def async_setup_entry(
    hass: "HomeAssistant", config_entry: "ConfigEntry", async_add_devices
):
    me.platform_setup_entry(hass, config_entry, async_add_devices, switch.DOMAIN)


class MLSwitch(me.MLBinaryEntity, switch.SwitchEntity):
    """
    Base (almost abstract) entity for switches. This has 2 main implementations:
    - MLDeviceSwitch: switch representing some device feature (an actual output or a config option)
    - MLEmulatedSwitch: switch used to configure a meross_lan feature/option
    """

    if TYPE_CHECKING:

        class Args(me.MLBinaryEntity.Args):
            device_class: NotRequired[switch.SwitchDeviceClass | None]

        # HA core entity attributes:
        _attr_device_class: ClassVar[switch.SwitchDeviceClass | None]

    PLATFORM = switch.DOMAIN
    DeviceClass = switch.SwitchDeviceClass

    # HA core entity attributes:
    _attr_device_class = switch.SwitchDeviceClass.SWITCH
    entity_category = me.MLBinaryEntity.EntityCategory.CONFIG


class MLEmulatedSwitch(me.MEPartialAvailableMixin, MLSwitch):
    """
    Switch entity not related to any device feature but used to configure
    behaviors for meross_lan entities.
    """

    def __init__(
        self,
        manager: "BaseDevice",
        channel: object,
        entitykey: str | None = None,
        /,
        **kwargs: "Unpack[MLEmulatedSwitch.Args]",
    ):
        super().__init__(manager, channel, entitykey, **kwargs)

    async def async_added_to_hass(self):
        await super().async_added_to_hass()
        with self.exception_warning("restoring previous state"):
            if last_state := await self.get_last_state_available():
                self.is_on = last_state.state == self.hac.STATE_ON

    @override
    async def async_turn_on(self, **kwargs):
        self.update_native_value(True)

    @override
    async def async_turn_off(self, **kwargs):
        self.update_native_value(False)


class MLDeviceSwitch(MLSwitch):
    """
    Generic HA switch: could either be a physical outlet or another binary setting
    of the device (see various config switches)
    Switches are sometimes hybrid and their message dispatching is not 'set in stone'
    since the status updates are likely managed in higher level implementations or so.
    This class needs to be mixed in with any of the me.MENoChannelMixin,
    me.MEDictChannelMixin, MEListChannelMixin in order to actually define the
    implementation of the protocol message payload for 'SET' commands
    """

    @override
    async def async_turn_on(self, **kwargs):
        if await self.async_request_value(self.native_on):
            self.update_native_value(True)

    @override
    async def async_turn_off(self, **kwargs):
        if await self.async_request_value(self.native_off):
            self.update_native_value(False)


class PhysicalLockSwitch(MLDeviceSwitch):

    ns = mn.Appliance_Control_PhysicalLock

    def __init__(self, manager: "Device", ns, /):
        # right now we expect only 1 entity on channel == 0 (whatever)
        MLDeviceSwitch.__init__(self, manager, 0, mc.KEY_LOCK)
        manager.register_parser_entity(self)


class MLToggle(EntityNamespaceMixin, MLDeviceSwitch):

    # 2024-03-13: passing entitykey="0" instead of channel in order
    # to mantain unique_id compatibility with installations but
    # updating to new toggle entity model (where channel is None for this entity type)
    # 2025-12-22: restructiring MLToggle to use EntityNamespaceMixin
    # but we still keep entitykey = "0" for compatibility with installed registry entries
    ENTITY_KEY = "0"
    ns = mn.Appliance_Control_Toggle

    # HA core entity attributes:
    _attr_device_class = MLDeviceSwitch.DeviceClass.OUTLET
    entity_category = None


def digest_init_toggle(device: "Device", digest: dict, /) -> "DigestInitReturnType":
    """{"onoff": 0, "lmTime": 1645391086}"""
    toggle = MLToggle(device, mn.Appliance_Control_Toggle)
    return toggle._parse, (device.ns_handlers[mn.Appliance_Control_Toggle],)


class MLToggleX(MLDeviceSwitch):

    ns = mn.Appliance_Control_ToggleX

    # HA core entity attributes:
    _attr_device_class = MLDeviceSwitch.DeviceClass.OUTLET
    entity_category = None

    def __init__(self, manager: "Device", channel, /):
        MLDeviceSwitch.__init__(self, manager, channel, None)
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
    if (mn.Appliance_Control_Fan in device.descriptor.ability) and (
        mc.KEY_FAN not in digest
    ):
        try:
            channels.remove(0)
        except KeyError:
            pass

    for channel in channels:
        MLToggleX(device, channel)

    ns = mn.Appliance_Control_ToggleX
    handler = device.get_handler(ns)
    handler.register_entity_class(MLToggleX)
    if device.descriptor.is_refoss:
        handler.polling_request = (
            ns,
            mc.METHOD_GET,
            {ns.key: mn.PayloadType.DICT_C_65535.value},
        )
    return handler.parse_list, (handler,)
