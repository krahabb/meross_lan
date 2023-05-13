from __future__ import annotations

import typing

from homeassistant.components import switch

from . import meross_entity as me
from .merossclient import const as mc  # mEROSS cONST

if typing.TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

    from .meross_device import MerossDevice
    from .merossclient import MerossDeviceDescriptor

try:
    SwitchDeviceClass = switch.SwitchDeviceClass  # type: ignore
except Exception:
    from .helpers import StrEnum

    class SwitchDeviceClass(StrEnum):
        OUTLET = "outlet"
        SWITCH = "switch"


async def async_setup_entry(
    hass: HomeAssistant, config_entry: ConfigEntry, async_add_devices
):
    me.platform_setup_entry(hass, config_entry, async_add_devices, switch.DOMAIN)


class MLSwitch(me.MerossToggle, switch.SwitchEntity):
    """
    generic plugs (single/multi outlet and so)
    """

    PLATFORM = switch.DOMAIN
    DeviceClass = SwitchDeviceClass

    @staticmethod
    def build_for_device(device: MerossDevice, channel: object, namespace: str):
        return MLSwitch(
            device, channel, None, SwitchDeviceClass.OUTLET, namespace
        )


class ToggleXMixin(MerossDevice if typing.TYPE_CHECKING else object):
    def __init__(self, descriptor: MerossDeviceDescriptor, entry):
        super().__init__(descriptor, entry)
        # we build switches here after everything else have been
        # setup since the togglex verb might refer to a more specialized
        # entity than switches
        togglex = descriptor.digest.get(mc.KEY_TOGGLEX)
        if isinstance(togglex, list):
            for t in togglex:
                channel = t.get(mc.KEY_CHANNEL)
                if channel not in self.entities:
                    MLSwitch.build_for_device(self, channel, mc.NS_APPLIANCE_CONTROL_TOGGLEX)
        elif isinstance(togglex, dict):
            channel = togglex.get(mc.KEY_CHANNEL)
            if channel not in self.entities:
                MLSwitch.build_for_device(self, channel, mc.NS_APPLIANCE_CONTROL_TOGGLEX)
        # This is an euristhic for legacy firmwares or
        # so when we cannot init any entity from system.all.digest
        # we then guess we should have at least a switch
        # edit: I guess ToggleX firmwares and on already support
        # system.all.digest status broadcast
        if not self.entities:
            MLSwitch.build_for_device(self, 0, mc.NS_APPLIANCE_CONTROL_TOGGLEX)

    def _handle_Appliance_Control_ToggleX(self, header: dict, payload: dict):
        self._parse__generic(mc.KEY_TOGGLEX, payload.get(mc.KEY_TOGGLEX))

    def _parse_togglex(self, payload: dict):
        self._parse__generic(mc.KEY_TOGGLEX, payload)


class ToggleMixin(MerossDevice if typing.TYPE_CHECKING else object):
    def __init__(self, descriptor: MerossDeviceDescriptor, entry):
        super().__init__(descriptor, entry)
        # older firmwares (MSS110 with 1.1.28) look like dont really have 'digest'
        # but have 'control' and the toggle payload looks like not carrying 'channel'
        p_control = descriptor.all.get(mc.KEY_CONTROL)
        if p_control:
            p_toggle = p_control.get(mc.KEY_TOGGLE)
            if isinstance(p_toggle, dict):
                MLSwitch.build_for_device(
                    self,
                    p_toggle.get(mc.KEY_CHANNEL, 0),
                    mc.NS_APPLIANCE_CONTROL_TOGGLE,
                )

        if not self.entities:
            MLSwitch.build_for_device(self, 0, mc.NS_APPLIANCE_CONTROL_TOGGLE)

    def _handle_Appliance_Control_Toggle(self, header: dict, payload: dict):
        self._parse_toggle(payload[mc.KEY_TOGGLE])

    def _parse_toggle(self, payload):
        """
        toggle doesn't have channel (#172)
        """
        if isinstance(payload, dict):
            entity: MLSwitch = self.entities[payload.get(mc.KEY_CHANNEL, 0)]  # type: ignore
            entity._parse_toggle(payload)
