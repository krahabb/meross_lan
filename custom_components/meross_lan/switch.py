from __future__ import annotations

import typing

from homeassistant.components import switch

from . import meross_entity as me
from .helpers.namespaces import EntityPollingStrategy, digest_parse_empty
from .merossclient import const as mc  # mEROSS cONST

if typing.TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

    from .climate import MtsClimate
    from .meross_device import MerossDevice
    from .merossclient import MerossDeviceDescriptor


async def async_setup_entry(
    hass: HomeAssistant, config_entry: ConfigEntry, async_add_devices
):
    me.platform_setup_entry(hass, config_entry, async_add_devices, switch.DOMAIN)


class MLSwitch(me.MerossToggle, switch.SwitchEntity):
    """
    Generic HA switch: could either be a physical outlet or another 'logical' setting
    (see various config switches)
    Switches are sometimes polymorphic and their message dispatching is not 'set in stone'
    since the status updates are likely managed in higher level implementations or so.
    """

    PLATFORM = switch.DOMAIN
    DeviceClass = switch.SwitchDeviceClass


class MtsConfigSwitch(MLSwitch):
    entity_category = MLSwitch.EntityCategory.CONFIG

    def __init__(
        self,
        climate: MtsClimate,
        entitykey: str,
        *,
        onoff=None,
        namespace: str,
    ):
        super().__init__(
            climate.manager,
            climate.channel,
            entitykey,
            MLSwitch.DeviceClass.SWITCH,
            onoff=onoff,
            namespace=namespace,
        )

    async def async_request_onoff(self, onoff: int):
        if await self.manager.async_request_ack(
            self.namespace,
            mc.METHOD_SET,
            {
                self.key_namespace: [
                    {
                        self.key_channel: self.channel,
                        self.key_value: onoff,
                    }
                ]
            },
        ):
            self.update_onoff(onoff)


class PhysicalLockSwitch(MLSwitch):

    namespace = mc.NS_APPLIANCE_CONTROL_PHYSICALLOCK
    key_namespace = mc.KEY_LOCK

    # HA core entity attributes:
    entity_category = MLSwitch.EntityCategory.CONFIG

    def __init__(self, manager: MerossDevice):
        # right now we expect only 1 entity on channel == 0 (whatever)
        super().__init__(manager, 0, mc.KEY_LOCK, self.DeviceClass.SWITCH)
        manager.register_parser(self.namespace, self)
        EntityPollingStrategy(manager, self.namespace, self, item_count=1)

    # interface: MerossToggle
    async def async_request_onoff(self, onoff: int):
        if await self.manager.async_request_ack(
            self.namespace,
            mc.METHOD_SET,
            {
                self.key_namespace: [
                    {self.key_channel: self.channel, self.key_value: onoff}
                ]
            },
        ):
            self.update_onoff(onoff)


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
                switch = (
                    self.entities[channel]
                    if channel in self.entities
                    else self._build_outlet(channel)
                )
                self.register_parser(mc.NS_APPLIANCE_CONTROL_TOGGLEX, switch)
        elif isinstance(togglex, dict):
            channel = togglex.get(mc.KEY_CHANNEL)
            switch = (
                self.entities[channel]
                if channel in self.entities
                else self._build_outlet(channel)
            )
            self.register_parser(mc.NS_APPLIANCE_CONTROL_TOGGLEX, switch)
        # This is an euristhic for legacy firmwares or
        # so when we cannot init any entity from system.all.digest
        # we then guess we should have at least a switch
        # edit: I guess ToggleX firmwares and on already support
        # system.all.digest status broadcast
        if not self.entities:
            switch = self._build_outlet(0)
            self.register_parser(mc.NS_APPLIANCE_CONTROL_TOGGLEX, switch)

    def _init_togglex(self, digest: list):
        return self.get_handler(mc.NS_APPLIANCE_CONTROL_TOGGLEX)._parse_list

    def _build_outlet(self, channel: object):
        return MLSwitch(
            self,
            channel,
            None,
            MLSwitch.DeviceClass.OUTLET,
            namespace=mc.NS_APPLIANCE_CONTROL_TOGGLEX,
        )


class ToggleMixin(MerossDevice if typing.TYPE_CHECKING else object):
    def __init__(self, descriptor: MerossDeviceDescriptor, entry):
        super().__init__(descriptor, entry)
        # older firmwares (MSS110 with 1.1.28) look like dont really have 'digest'
        # but have 'control' and the toggle payload looks like not carrying 'channel'
        if isinstance(p_control := descriptor.all.get(mc.KEY_CONTROL), dict):
            for _key, _control in p_control.items():
                if _key == mc.KEY_TOGGLE:
                    self._build_outlet(_control.get(mc.KEY_CHANNEL, 0))
                    self.digest_handlers[_key] = self.namespace_handlers[mc.NS_APPLIANCE_CONTROL_TOGGLE]._parse_generic
                else:
                    self.digest_handlers[_key] = digest_parse_empty

        if not self.entities:
            self._build_outlet(0)

    def _build_outlet(self, channel: object):
        switch = MLSwitch(
            self,
            channel,
            None,
            MLSwitch.DeviceClass.OUTLET,
            namespace=mc.NS_APPLIANCE_CONTROL_TOGGLE,
        )
        self.register_parser(mc.NS_APPLIANCE_CONTROL_TOGGLE, switch)
