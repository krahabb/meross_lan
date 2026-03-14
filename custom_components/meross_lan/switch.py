from typing import TYPE_CHECKING, override

from homeassistant.components import switch

from .const import hac
from .helpers import entity as mle
from .merossclient import extract_dict_payloads
from .merossclient.protocol import const as mc, namespaces as mn

if TYPE_CHECKING:
    from typing import ClassVar, NotRequired, Unpack

    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

    from .helpers.device import Device
    from .merossclient.protocol.types import JsonDict, JsonList


async def async_setup_entry(
    hass: "HomeAssistant", config_entry: "ConfigEntry", async_add_devices
):
    mle.Entity.platform_setup_entry(
        hass, config_entry, async_add_devices, switch.DOMAIN
    )


class SwitchEntity(mle.BinaryEntity, switch.SwitchEntity):
    """ """

    if TYPE_CHECKING:

        # HA core entity attributes:
        _attr_device_class: ClassVar[switch.SwitchDeviceClass | None]

        class Args(mle.BinaryEntity.Args):
            device_class: NotRequired[switch.SwitchDeviceClass | None]

    PLATFORM = switch.DOMAIN
    DeviceClass = switch.SwitchDeviceClass

    # HA core entity attributes:
    _attr_device_class = switch.SwitchDeviceClass.SWITCH
    _attr_entity_category = mle.BinaryEntity.EntityCategory.CONFIG


class EmulatedSwitch(SwitchEntity):
    """
    Switch entity not related to any device feature but used to configure
    behaviors for meross_lan entities.
    """

    async def async_added_to_hass(self):
        await super().async_added_to_hass()
        with self.exception_warning("restoring previous state"):
            if last_state := await self.get_last_state_available():
                self.is_on = last_state.state == hac.STATE_ON

    @override
    async def async_turn_on(self, **kwargs):
        self.update_boolean_value(True)

    @override
    async def async_turn_off(self, **kwargs):
        self.update_boolean_value(False)


class SwitchParser(mle.BinaryParser, SwitchEntity):
    """Generic switch entity for meross_lan devices.
    This class is 'ready to use' for most of the devices toggleable features.
    It just need to be configured and linked to a proper ns/channel/key_value in order to work.
    """

    pass


class PhysicalLockSwitch(SwitchParser):

    NS_CHANNELS = SwitchParser.NS_CHANNELS_SINGLE
    init_entity_key = mc.KEY_LOCK
    init_ns = mn.Appliance_Control_PhysicalLock


class Toggle(mle.EntityNamespaceMixin, SwitchParser):

    POLLING_CONFIG_DEFAULT = mle.EntityNamespaceMixin.POLLING_CONFIG_STATE_NS
    init_entity_key = "0"  # used to keep unique_id compatibility with legacy versions
    init_ns = mn.Appliance_Control_Toggle
    # HA core entity attributes:
    _attr_device_class = SwitchEntity.DeviceClass.OUTLET
    _attr_entity_category = None

    @classmethod
    @override
    def digest_init(
        cls, device: "Device", digest: "JsonDict", /
    ) -> "Device.DigestInitReturnType":
        """{"onoff": 0, "lmTime": 1645391086}"""
        handler = Toggle.namespace_init(mn.Appliance_Control_Toggle, device)
        return handler._parse, (handler.handler_ns,)


class Togglex(SwitchParser):

    init_ns = mn.Appliance_Control_ToggleX

    # HA core entity attributes:
    _attr_device_class = SwitchEntity.DeviceClass.OUTLET
    _attr_entity_category = None

    @classmethod
    @override
    def digest_init(
        cls, device: "Device", togglex_digest: "JsonList", /
    ) -> "Device.DigestInitReturnType":
        # We don't initialize every switch/ToggleX here since the digest reported channels
        # might be mapped to more specialized entities:
        # this is true for lights, garageDoor and fan though
        # and maybe some more others.
        # In general, it is not very clear how and when these ToggleX entities are really needed
        # so we have some euristics in place to fix 'this and that'.
        # The general rule is to let the togglex namespace/channel be managed by the
        # aforementioned specialized entity, while, if no channel match exists, create a disabled
        # (by default) switch entity. When  switches are really switches (like mssXXX series) instead,
        # we'll setup proper ToggleXSwitch (this is detected by the fact no specialized entity exists in
        # device definition)

        channels = {togglex[mc.KEY_CHANNEL] for togglex in togglex_digest}

        digest = device.descriptor.digest

        for _key in (mc.KEY_FAN, mc.KEY_GARAGEDOOR, mc.KEY_LIGHT):
            if _key in digest:
                for _key_digest in extract_dict_payloads(digest[_key]):
                    try:
                        channels.remove(_key_digest[mc.KEY_CHANNEL])
                    except KeyError:
                        pass

        # the fan controller 'map100' doesn't expose a fan in digest but it has one at channel 0
        if (mn.Appliance_Control_Fan in device.descriptor.ability) and (
            mc.KEY_FAN not in digest
        ):
            try:
                channels.remove(0)
            except KeyError:
                pass

        # don't explicitly create the handler since it might have been created by other digest entities
        handler = device.get_handler(mn.Appliance_Control_ToggleX)
        handler.register_parser_class(Togglex, channels)
        if device.descriptor.is_refoss:
            handler.polling_request = mn.PayloadType.DICT_IDX_65535.build_get(
                handler.id
            )
        return handler.parse_list, (handler,)
