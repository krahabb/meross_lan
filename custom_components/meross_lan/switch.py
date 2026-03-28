from typing import TYPE_CHECKING, override

from homeassistant.components import switch

from .const import hac
from .helpers import entity as mle
from .merossclient import extract_dict_payloads
from .merossclient.protocol import const as mc, namespaces as mn

if TYPE_CHECKING:
    from typing import ClassVar, NotRequired, Unpack

    from .helpers.device import Device


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
        with self.exception_warning("restoring previous state"):
            if last_state := await self.get_last_state_available():
                self.is_on = last_state.state == hac.STATE_ON
        await super().async_added_to_hass()

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

    __slots__ = mle.BinaryParser._calc_slots()


class PhysicalLockSwitch(SwitchParser):

    init_entity_key = mc.KEY_LOCK


class Toggle(mle.EntityNamespaceMixin, SwitchParser):

    init_entity_key = "0"  # used to keep unique_id compatibility with legacy versions
    # HA core entity attributes:
    _attr_device_class = SwitchEntity.DeviceClass.OUTLET
    _attr_entity_category = None


class ToggleX(SwitchParser):

    # HA core entity attributes:
    _attr_device_class = SwitchEntity.DeviceClass.OUTLET
    _attr_entity_category = None

    @classmethod
    @override
    def namespace_init(cls, ns: mn.Namespace, device: "Device", /):
        # We don't initialize every channel here since the digest reported channels
        # might be mapped to more specialized entities:
        # this is true for lights, garageDoor and fan though.
        # When this happens we link the ToggleX ns to that more specialized parser entity
        # and we shouldnt build a ToggleX.
        # BEWARE: this ns registration must be done before those others in order to properly
        # link the channels and/or setup the correct entities.
        digest = device.descriptor.digest
        ns_digest = ns.get_digest(digest)
        channels = {togglex[mc.KEY_CHANNEL] for togglex in ns_digest}
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

        handler = device._create_handler(ns, parser_class=ToggleX, channels=channels)
        if device.descriptor.is_refoss:
            handler.polling_request = mn.PayloadType.DICT_IDX_65535.build_get(
                handler.id
            )


async_setup_entry = SwitchEntity.platform_setup_entry
