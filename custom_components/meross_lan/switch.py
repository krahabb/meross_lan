from __future__ import annotations

from homeassistant.components.switch import (
    DOMAIN as PLATFORM_SWITCH,
    SwitchEntity,
    DEVICE_CLASS_OUTLET
)

from .merossclient import const as mc  # mEROSS cONST
from .meross_entity import (
    _MerossToggle,
    platform_setup_entry, platform_unload_entry,
    STATE_OFF, STATE_ON,
    ENTITY_CATEGORY_CONFIG,
)
from .const import DND_ID


async def async_setup_entry(hass: object, config_entry: object, async_add_devices):
    platform_setup_entry(hass, config_entry, async_add_devices, PLATFORM_SWITCH)

async def async_unload_entry(hass: object, config_entry: object) -> bool:
    return platform_unload_entry(hass, config_entry, PLATFORM_SWITCH)



class MLSwitch(_MerossToggle, SwitchEntity):
    """
    generic plugs (single/multi outlet and so)
    """
    PLATFORM = PLATFORM_SWITCH


    def __init__(self, device: 'MerossDevice', channel: object, namespace: str):
        super().__init__(device, channel, None, DEVICE_CLASS_OUTLET, namespace)


class ToggleXMixin:

    def __init__(self, api, descriptor, entry) -> None:
        super().__init__(api, descriptor, entry)
        # we build switches here after everything else have been
        # setup since the togglex verb might refer to a more specialized
        # entity than switches
        togglex = descriptor.digest.get(mc.KEY_TOGGLEX)
        if isinstance(togglex, list):
            for t in togglex:
                channel = t.get(mc.KEY_CHANNEL)
                if channel not in self.entities:
                    MLSwitch(
                        self,
                        channel,
                        mc.NS_APPLIANCE_CONTROL_TOGGLEX)
        elif isinstance(togglex, dict):
            channel = togglex.get(mc.KEY_CHANNEL)
            if channel not in self.entities:
                MLSwitch(
                    self,
                    channel,
                    mc.NS_APPLIANCE_CONTROL_TOGGLEX)
        # This is an euristhic for legacy firmwares or
        # so when we cannot init any entity from system.all.digest
        # we then guess we should have at least a switch
        # edit: I guess ToggleX firmwares and on already support
        # system.all.digest status broadcast
        if not self.entities:
            MLSwitch(self, 0, mc.NS_APPLIANCE_CONTROL_TOGGLEX)


    """

    def _init_togglex(self, togglex: dict):
        channel = togglex.get(mc.KEY_CHANNEL)
        if channel not in self.entities:
            MLSwitch(self, channel, mc.NS_APPLIANCE_CONTROL_TOGGLEX)
    """

    def _handle_Appliance_Control_ToggleX(self,
    namespace: str, method: str, payload: dict, header: dict):
        self._parse__generic(mc.KEY_TOGGLEX, payload.get(mc.KEY_TOGGLEX))


    def _parse_togglex(self, payload: dict):
        self._parse__generic(mc.KEY_TOGGLEX, payload)


class ToggleMixin:

    def __init__(self, api, descriptor, entry) -> None:
        super().__init__(api, descriptor, entry)
        # older firmwares (MSS110 with 1.1.28) look like dont really have 'digest'
        # but have 'control' and the toggle payload looks like not carrying 'channel'
        p_control = descriptor.all.get(mc.KEY_CONTROL)
        if p_control:
            p_toggle = p_control.get(mc.KEY_TOGGLE)
            if isinstance(p_toggle, dict):
                MLSwitch(
                    self,
                    p_toggle.get(mc.KEY_CHANNEL, 0),
                    mc.NS_APPLIANCE_CONTROL_TOGGLE)

        if not self.entities:
            MLSwitch(self, 0, mc.NS_APPLIANCE_CONTROL_TOGGLE)


    def _handle_Appliance_Control_Toggle(self,
    namespace: str, method: str, payload: dict, header: dict):
        self._parse_toggle(payload.get(mc.KEY_TOGGLE))


    def _parse_toggle(self, payload: dict):
        """
        toggle doesn't have channel (#172)
        """
        if isinstance(payload, dict):
            entity: MLSwitch = self.entities[payload.get(mc.KEY_CHANNEL, 0)]
            entity._parse_toggle(payload)



class MLConfigSwitch(_MerossToggle, SwitchEntity):
    """
    configuration switch
    """
    PLATFORM = PLATFORM_SWITCH

    @property
    def entity_category(self):
        return ENTITY_CATEGORY_CONFIG


"""
class MLDNDSwitch(_MerossToggle, SwitchEntity):
    "
    Do Not Disturb mode for devices supporting it (i.e. comfort lights on switches)
    "
    PLATFORM = PLATFORM_SWITCH


    def __init__(self, device: 'MerossDevice'):
        super().__init__(device, None, DND_ID, mc.KEY_DNDMODE, None)


    @property
    def entity_category(self) -> str | None:
        return ENTITY_CATEGORY_CONFIG


    async def async_turn_on(self, **kwargs) -> None:

        def _ack_callback():
            self.update_state(STATE_ON)

        # WARNING: on MQTT we'll loose the ack callback since
        # it's not (yet) implemented and the option to correctly
        # update the state will be loosed since the ack payload is empty
        # right now 'force' http proto even tho that could be disabled in config
        await self.device.async_http_request(
            mc.NS_APPLIANCE_SYSTEM_DNDMODE,
            mc.METHOD_SET,
            {mc.KEY_DNDMODE: {mc.KEY_MODE: 1}},
            _ack_callback
        )


    async def async_turn_off(self, **kwargs) -> None:

        def _ack_callback():
            self.update_state(STATE_OFF)

        await self.device.async_http_request(
            mc.NS_APPLIANCE_SYSTEM_DNDMODE,
            mc.METHOD_SET,
            {mc.KEY_DNDMODE: {mc.KEY_MODE: 0}},
            _ack_callback
        )
"""