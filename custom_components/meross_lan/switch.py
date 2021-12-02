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



class MerossLanSwitch(_MerossToggle, SwitchEntity):
    """
    generic plugs (single/multi outlet and so)
    """
    PLATFORM = PLATFORM_SWITCH


    def __init__(self, device: 'MerossDevice', _id: object, toggle_ns: str, toggle_key: str):
        super().__init__(device, _id, DEVICE_CLASS_OUTLET, toggle_ns, toggle_key)



class MerossLanSpray(_MerossToggle, SwitchEntity):
    """
    Meross humidifier (spray device) is implemented as 'select' entity on later HA cores
    this is a fallback implementation for older cores
    """
    PLATFORM = PLATFORM_SWITCH


    def __init__(self, device: 'MerossDevice', _id: object):
        super().__init__(device, _id, mc.KEY_SPRAY, None, None)


    async def async_turn_on(self, **kwargs) -> None:

        def _ack_callback():
            self.update_state(STATE_ON)

        # WARNING: on MQTT we'll loose the ack callback since
        # it's not (yet) implemented and the option to correctly
        # update the state will be loosed since the ack payload is empty
        # right now 'force' http proto even tho that could be disabled in config
        await self.device.async_http_request(
            mc.NS_APPLIANCE_CONTROL_SPRAY,
            mc.METHOD_SET,
            {mc.KEY_SPRAY: {mc.KEY_CHANNEL: self.id, mc.KEY_MODE: mc.SPRAY_MODE_CONTINUOUS}},
            _ack_callback
        )


    async def async_turn_off(self, **kwargs) -> None:

        def _ack_callback():
            self.update_state(STATE_OFF)

        await self.device.async_http_request(
            mc.NS_APPLIANCE_CONTROL_SPRAY,
            mc.METHOD_SET,
            {mc.KEY_SPRAY: {mc.KEY_CHANNEL: self.id, mc.KEY_MODE: mc.SPRAY_MODE_OFF}},
            _ack_callback
        )


    def update_mode(self, spray_mode: int) -> None:
        self.update_state(STATE_OFF if spray_mode == mc.SPRAY_MODE_OFF else STATE_ON)



class MerossLanDND(_MerossToggle, SwitchEntity):
    """
    Do Not Disturb mode for devices supporting it (i.e. comfort lights on switches)
    """
    PLATFORM = PLATFORM_SWITCH


    def __init__(self, device: 'MerossDevice'):
        super().__init__(device, DND_ID, mc.KEY_DNDMODE, None, None)


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