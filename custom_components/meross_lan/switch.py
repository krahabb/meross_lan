from typing import TYPE_CHECKING, override

from homeassistant.components import switch

from .helpers import entity as me
from .helpers.namespaces import EntityNamespaceMixin, mc, mn
from .merossclient import extract_dict_payloads

if TYPE_CHECKING:
    from typing import ClassVar, NotRequired, Unpack

    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

    from .helpers.device import Device, DigestInitReturnType


async def async_setup_entry(
    hass: "HomeAssistant", config_entry: "ConfigEntry", async_add_devices
):
    me.platform_setup_entry(hass, config_entry, async_add_devices, switch.DOMAIN)


class MLSwitch(me.MLBinaryEntity, switch.SwitchEntity):
    """
    Generic switch entity for meross_lan devices.
    This class is 'ready to use' for most of the devices toggleable features.
    It just need to be configured and linked to a proper ns/channel/key_value in order to work.
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


class PhysicalLockSwitch(MLSwitch):

    ENTITY_KEY = mc.KEY_LOCK
    ns = mn.Appliance_Control_PhysicalLock
    NS_CHANNELS = (0,)

    def __init__(self, manager: "Device", channel, /):
        MLSwitch.__init__(self, manager, channel)
        manager.register_parser_entity(self)


class MLToggle(EntityNamespaceMixin, MLSwitch):

    # 2024-03-13: passing entitykey="0" instead of channel in order
    # to mantain unique_id compatibility with installations but
    # updating to new toggle entity model (where channel is None for this entity type)
    # 2025-12-22: restructiring MLToggle to use EntityNamespaceMixin
    # but we still keep entitykey = "0" for compatibility with installed registry entries
    ENTITY_KEY = "0"
    ns = mn.Appliance_Control_Toggle

    # HA core entity attributes:
    _attr_device_class = MLSwitch.DeviceClass.OUTLET
    entity_category = None


def digest_init_toggle(device: "Device", digest: dict, /) -> "DigestInitReturnType":
    """{"onoff": 0, "lmTime": 1645391086}"""
    toggle = MLToggle.namespace_init(device, mn.Appliance_Control_Toggle)
    return toggle._parse, (device.ns_handlers[mn.Appliance_Control_Toggle],)


class MLToggleX(MLSwitch):

    ns = mn.Appliance_Control_ToggleX

    # HA core entity attributes:
    _attr_device_class = MLSwitch.DeviceClass.OUTLET
    entity_category = None

    def __init__(self, manager: "Device", channel, /):
        MLSwitch.__init__(self, manager, channel, None)
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

    channels = {togglex[mc.KEY_CHANNEL] for togglex in togglex_digest}

    digest = device.descriptor.digest

    for _key in (mc.KEY_FAN, mc.KEY_GARAGEDOOR, mc.KEY_LIGHT):
        if _key in digest:
            for _key_digest in extract_dict_payloads(digest[_key]):
                channel = _key_digest.get(mc.KEY_CHANNEL)
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

    ns = mn.Appliance_Control_ToggleX
    handler = device.get_handler(ns)
    handler.register_entity_class(MLToggleX, channels)
    if device.descriptor.is_refoss:
        handler.polling_request = mn.PayloadType.DICT_C_65535.build_get(ns)
    return handler.parse_list, (handler,)
