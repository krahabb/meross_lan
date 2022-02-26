from __future__ import annotations

from .helpers import LOGGER

from homeassistant.const import (
    STATE_OFF as OPTION_SPRAY_MODE_OFF,
    STATE_ON as OPTION_SPRAY_MODE_CONTINUOUS,
)
try:
    from homeassistant.components.humidifier.const import MODE_ECO as OPTION_SPRAY_MODE_ECO
except:
    OPTION_SPRAY_MODE_ECO = 'eco'

try:# to look for select platform in HA core (available since some 2021.xx...)
    from homeassistant.components.select import DOMAIN as PLATFORM_SELECT, SelectEntity
    from .meross_entity import _MerossEntity, platform_setup_entry, platform_unload_entry

    async def async_setup_entry(hass: object, config_entry: object, async_add_devices):
        platform_setup_entry(hass, config_entry, async_add_devices, PLATFORM_SELECT)

    async def async_unload_entry(hass: object, config_entry: object) -> bool:
        return platform_unload_entry(hass, config_entry, PLATFORM_SELECT)

except:# implement a fallback by using a plain old switch
    LOGGER.warning("Missing 'select' entity type. Please update HA to latest version"
        " to fully support meross 'SPRAY' feature. Falling back to basic switch behaviour")
    from homeassistant.components.switch import DOMAIN as PLATFORM_SELECT, SwitchEntity as SelectEntity
    from .meross_entity import _MerossEntity

from .merossclient import const as mc  # mEROSS cONST


"""
    This code is an alternative implementation for SPRAY/humidifier
    since the meross SPRAY doesnt support target humidity and
    the 'semantics' for HA humidifier are a bit odd for this device
    Also, bear in mind that, if select is not supported in HA core
    we're basically implementing a SwitchEntity
"""
class MLSpray(_MerossEntity, SelectEntity):

    PLATFORM = PLATFORM_SELECT

    """
    a dict containing mapping between meross modes <-> HA select options
    like { mc.SPRAY_MODE_OFF: OPTION_SPRAY_MODE_OFF }
    """
    _spray_mode_map: dict[object, str]


    def __init__(
        self,
        device: 'MerossDevice',
        channel: object,
        spraymode_map: dict):
        super().__init__(device, channel, mc.KEY_SPRAY, mc.KEY_SPRAY)
        # we could use the shared instance but different device firmwares
        # could bring in unwanted global options...
        self._spray_mode_map = dict(spraymode_map)
        self._attr_options = list(self._spray_mode_map.values())


    @property
    def current_option(self) -> str | None:
        """Return the selected entity option to represent the entity state."""
        return self._attr_state


    async def async_select_option(self, option: str) -> None:
        # reverse lookup the dict
        for mode, _option in self._spray_mode_map.items():
            if _option == option:
                break
        else:
            raise NotImplementedError()

        def _ack_callback():
            self.update_state(option)

        self.device.request_spray(
            { mc.KEY_CHANNEL: self.channel, mc.KEY_MODE: mode },
            _ack_callback)


    async def async_turn_on(self, **kwargs) -> None:
        """ in case we have to implement as a SwitchEntity when SelectEntity is lacking"""
        await self.async_select_option(OPTION_SPRAY_MODE_CONTINUOUS)


    async def async_turn_off(self, **kwargs) -> None:
        """ in case we have to implement as a SwitchEntity when SelectEntity is lacking"""
        await self.async_select_option(OPTION_SPRAY_MODE_OFF)


    def _parse_spray(self, payload: dict) -> None:
        """
        We'll map the mode key to a well-known option for this entity
        but, since there could be some additions from newer spray devices
        we'll also eventually add the unknown mode value as a supported mode
        Keep in mind we're updating a class instance dict so it should affect
        all of the same-class-entities
        """
        mode = payload[mc.KEY_MODE]
        option = self._spray_mode_map.get(mode)
        if option is None:
            # unknown mode value -> auto-learning
            option = 'mode_' + str(mode)
            self._spray_mode_map[mode] = option
            self._attr_options = list(self._spray_mode_map.values())
        # we actually don't care if this is a SwitchEntity
        # this is a bug since state would be wrongly reported
        # when mode != on/off
        self.update_state(option)



class SprayMixin:

    SPRAY_MODE_MAP = {
        mc.SPRAY_MODE_OFF: OPTION_SPRAY_MODE_OFF,
        mc.SPRAY_MODE_INTERMITTENT: OPTION_SPRAY_MODE_ECO,
        mc.SPRAY_MODE_CONTINUOUS: OPTION_SPRAY_MODE_CONTINUOUS,
    }


    def _init_spray(self, payload: dict):
        #spray = [{"channel": 0, "mode": 0, "lmTime": 1629035486, "lastMode": 1, "onoffTime": 1629035486}]
        MLSpray(
            self,
            payload.get(mc.KEY_CHANNEL, 0),
            self.SPRAY_MODE_MAP)


    def _handle_Appliance_Control_Spray(self, namespace: str, method: str, payload: dict, header: dict):
        self._parse_spray(payload.get(mc.KEY_SPRAY))


    def _parse_spray(self, payload: dict):
        self._parse__generic(mc.KEY_SPRAY, payload, mc.KEY_SPRAY)


    def request_spray(self, payload, callback):
        self.request(
            mc.NS_APPLIANCE_CONTROL_SPRAY,
            mc.METHOD_SET,
            { mc.KEY_SPRAY: payload },
            callback)
