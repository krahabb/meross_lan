from __future__ import annotations
from functools import partial

from .helpers import LOGGER

from homeassistant.const import (
    STATE_OFF as OPTION_SPRAY_MODE_OFF,
    STATE_ON as OPTION_SPRAY_MODE_CONTINUOUS,
    STATE_UNKNOWN
)
try:
    from homeassistant.components.humidifier.const import MODE_ECO as OPTION_SPRAY_MODE_INTERMITTENT
except:
    OPTION_SPRAY_MODE_INTERMITTENT = 'intermittent'

try:# to look for select platform in HA core (available since some 2021.xx...)
    from homeassistant.components.select import DOMAIN as PLATFORM_SELECT, SelectEntity
    from .meross_entity import _MerossToggle, platform_setup_entry, platform_unload_entry

    async def async_setup_entry(hass: object, config_entry: object, async_add_devices):
        platform_setup_entry(hass, config_entry, async_add_devices, PLATFORM_SELECT)

    async def async_unload_entry(hass: object, config_entry: object) -> bool:
        return platform_unload_entry(hass, config_entry, PLATFORM_SELECT)

except:# implement a fallback by using a plain old switch
    LOGGER.warning("Missing 'select' entity type. Please update HA to latest version"
        " to fully support meross 'SPRAY' feature. Falling back to basic switch behaviour")
    from homeassistant.components.switch import DOMAIN as PLATFORM_SELECT, SwitchEntity as SelectEntity
    from .meross_entity import _MerossToggle

from .merossclient import const as mc  # mEROSS cONST


OPTION_TO_SPRAY_MODE_MAP = {
    OPTION_SPRAY_MODE_OFF: mc.SPRAY_MODE_OFF,
    OPTION_SPRAY_MODE_CONTINUOUS: mc.SPRAY_MODE_CONTINUOUS,
    OPTION_SPRAY_MODE_INTERMITTENT: mc.SPRAY_MODE_INTERMITTENT
}

"""
    This code is an alternative implementation for SPRAY/humidifier
    since the meross SPRAY doesnt support target humidity and
    the 'semantics' for HA humidifier are a bit odd for this device
    Also, bear in mind that, if select is not supported in HA core
    we're basically implementing a SwitchEntity
"""
class MLSpray(_MerossToggle, SelectEntity):

    PLATFORM = PLATFORM_SELECT

    _attr_options: list[str] = [
        OPTION_SPRAY_MODE_OFF,
        OPTION_SPRAY_MODE_CONTINUOUS,
        OPTION_SPRAY_MODE_INTERMITTENT
    ]

    _attr_current_option: str | None = None


    def __init__(
        self,
        device: 'MerossDevice',
        channel: object,
        namespace: str):
        super().__init__(device, channel, mc.KEY_SPRAY, mc.KEY_SPRAY, namespace)


    async def async_select_option(self, option: str) -> None:

        spray_mode = OPTION_TO_SPRAY_MODE_MAP[option]

        def _ack_callback():
            self._attr_current_option = option
            self.update_state(option)

        self.device.request(
            self.namespace,
            mc.METHOD_SET,
            {mc.KEY_SPRAY: {mc.KEY_CHANNEL: self.channel, mc.KEY_MODE: spray_mode}},
            _ack_callback
        )


    async def async_turn_on(self, **kwargs) -> None:
        """ in case we have to implement as a SwitchEntity when SelectEntity is lacking"""
        await self.async_select_option(OPTION_SPRAY_MODE_CONTINUOUS)


    async def async_turn_off(self, **kwargs) -> None:
        """ in case we have to implement as a SwitchEntity when SelectEntity is lacking"""
        await self.async_select_option(OPTION_SPRAY_MODE_OFF)


    def _parse_spray(self, payload: dict) -> None:
        try:
            spray_mode = payload.get(mc.KEY_MODE)
            self._attr_current_option = self._attr_options[spray_mode]
            # we actually don't care if this is a SwitchEntity
            # this is a bug since state would be wrongly reported
            # when mode != continuous
            self.update_state(self._attr_current_option)
        except:
            self._attr_current_option = None
            self.update_state(STATE_UNKNOWN)



class SprayMixin:


    def _init_spray(self, payload: dict):
        #spray = [{"channel": 0, "mode": 0, "lmTime": 1629035486, "lastMode": 1, "onoffTime": 1629035486}]
        MLSpray(self, payload.get(mc.KEY_CHANNEL, 0), mc.NS_APPLIANCE_CONTROL_SPRAY)


    def _handle_Appliance_Control_Spray(self, namespace: str, method: str, payload: dict, header: dict):
        self._parse_spray(payload.get(mc.KEY_SPRAY))


    def _parse_spray(self, payload: dict):
        self._parse__generic(mc.KEY_SPRAY, payload, mc.KEY_SPRAY)
