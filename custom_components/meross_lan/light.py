from __future__ import annotations
from typing import Union, Tuple

from homeassistant.components.light import (
    DOMAIN as PLATFORM_LIGHT,
    LightEntity,
    ATTR_BRIGHTNESS, ATTR_HS_COLOR, ATTR_COLOR_TEMP,
    ATTR_RGB_COLOR, ATTR_EFFECT,
)

# back-forward compatibility hell
try:
    from homeassistant.components.light import (
        SUPPORT_BRIGHTNESS,
        SUPPORT_COLOR,
        SUPPORT_COLOR_TEMP,
        SUPPORT_EFFECT
    )
except:
    SUPPORT_BRIGHTNESS = 0
    SUPPORT_COLOR = 0
    SUPPORT_COLOR_TEMP = 0
    SUPPORT_EFFECT = 0

try:
    from homeassistant.components.light import (
        COLOR_MODE_UNKNOWN, COLOR_MODE_ONOFF, COLOR_MODE_BRIGHTNESS,
        COLOR_MODE_HS, COLOR_MODE_RGB, COLOR_MODE_COLOR_TEMP,
    )
except:
    COLOR_MODE_UNKNOWN = ''
    COLOR_MODE_ONOFF = COLOR_MODE_UNKNOWN
    COLOR_MODE_BRIGHTNESS = COLOR_MODE_UNKNOWN
    COLOR_MODE_HS = COLOR_MODE_UNKNOWN
    COLOR_MODE_RGB = COLOR_MODE_UNKNOWN
    COLOR_MODE_COLOR_TEMP = COLOR_MODE_UNKNOWN


import homeassistant.util.color as color_util

from .merossclient import const as mc
from .meross_device import MerossDevice
from .meross_entity import (
    _MerossToggle,
    platform_setup_entry, platform_unload_entry,
    STATE_ON, STATE_OFF,
    ENTITY_CATEGORY_CONFIG,
)
from .const import DND_ID

"""
    map light Temperature effective range to HA mired(s):
    right now we'll use a const approach since it looks like
    any light bulb out there carries the same specs
    MIRED <> 1000000/TEMPERATURE[K]
    (thanks to @nao-pon #87)
"""
MSLANY_MIRED_MIN = 153 # math.floor(1/(6500/1000000))
MSLANY_MIRED_MAX = 371 # math.ceil(1/(2700/1000000))


async def async_setup_entry(hass: object, config_entry: object, async_add_devices):
    platform_setup_entry(hass, config_entry, async_add_devices, PLATFORM_LIGHT)

async def async_unload_entry(hass: object, config_entry: object) -> bool:
    return platform_unload_entry(hass, config_entry, PLATFORM_LIGHT)


def _rgb_to_int(rgb: Union[tuple, dict, int]) -> int:  # pylint: disable=unsubscriptable-object
    if isinstance(rgb, int):
        return rgb
    elif isinstance(rgb, tuple):
        red, green, blue = rgb
    elif isinstance(rgb, dict):
        red = rgb['red']
        green = rgb['green']
        blue = rgb['blue']
    else:
        raise ValueError("Invalid value for RGB!")
    return (red << 16) + (green << 8) + blue

def _int_to_rgb(rgb: int) -> Tuple[int, int, int]:
    return (rgb & 16711680) >> 16, (rgb & 65280) >> 8, (rgb & 255)

def _sat_1_100(value) -> int:
    if value > 100:
        return 100
    elif value < 1:
        return 1
    else:
        return int(value)


class MerossLanLight(_MerossToggle, LightEntity):
    """
    light entity for Meross bulbs and any device supporting light api
    (identified from devices carrying 'light' node in SYSTEM_ALL payload)
    """
    PLATFORM = PLATFORM_LIGHT

    _attr_max_mireds = MSLANY_MIRED_MAX
    _attr_min_mireds = MSLANY_MIRED_MIN
    _attr_supported_features = 0
    _attr_supported_color_modes = {}
    _attr_color_mode = None
    _attr_rgb_color: tuple[int, int, int] | None = None
    _attr_hs_color: tuple[float, float] | None = None
    _attr_color_temp = None
    _attr_brightness = None
    _attr_effect: str | None = None


    def __init__(self, device: MerossDevice, id: object, p_togglex):
        # we'll use the (eventual) togglex payload to
        # see if we have to toggle the light by togglex or so
        # with msl120j (fw 3.1.4) I've discovered that any 'light' payload sent will turn on the light
        # (disregarding any 'onoff' field inside).
        # The msl120j never 'pushes' an 'onoff' field in the light payload while msl120b (fw 2.1.16)
        # does that.
        # we'll use a 'conservative' approach here where we always toggle by togglex (if presented in digest)
        # and kindly ignore any 'onoff' in the 'light' payload (except digest didn't presented togglex)
        self._usetogglex = False
        if isinstance(p_togglex, list):
            for t in p_togglex:
                if t.get(mc.KEY_CHANNEL) == id:
                    self._usetogglex = True
                    break
        elif isinstance(p_togglex, dict):
            self._usetogglex = (p_togglex.get(mc.KEY_CHANNEL) == id)

        # in case we're not using togglex fallback to toggle but..the light could
        # be switchable by 'onoff' field in light payload itself..(to be investigated)
        super().__init__(
            device, id, None,
            mc.NS_APPLIANCE_CONTROL_TOGGLEX if self._usetogglex else mc.NS_APPLIANCE_CONTROL_TOGGLE,
            mc.KEY_TOGGLEX if self._usetogglex else mc.KEY_TOGGLE)
        """
        self._light = {
			#"onoff": 0,
			"capacity": CAPACITY_LUMINANCE,
			"channel": channel,
			#"rgb": 16753920,
			#"temperature": 100,
			"luminance": 100,
			"transform": 0,
            "gradual": 0
		}
        """
        self._light = dict()

        self._capacity = device.descriptor.ability.get(
            mc.NS_APPLIANCE_CONTROL_LIGHT, {}).get(
                mc.KEY_CAPACITY, mc.LIGHT_CAPACITY_LUMINANCE)

        if SUPPORT_BRIGHTNESS:
            # these will be removed in 2021.10
            self._attr_supported_features = (SUPPORT_BRIGHTNESS if self._capacity & mc.LIGHT_CAPACITY_LUMINANCE else 0)\
                | (SUPPORT_COLOR if self._capacity & mc.LIGHT_CAPACITY_RGB else 0)\
                | (SUPPORT_COLOR_TEMP if self._capacity & mc.LIGHT_CAPACITY_TEMPERATURE else 0)

        if COLOR_MODE_BRIGHTNESS:
            # new color_mode support from 2021.4.0
            self._attr_supported_color_modes = set()
            if self._capacity & mc.LIGHT_CAPACITY_RGB:
                self._attr_supported_color_modes.add(COLOR_MODE_RGB)
                self._attr_supported_color_modes.add(COLOR_MODE_HS)
            if self._capacity & mc.LIGHT_CAPACITY_TEMPERATURE:
                self._attr_supported_color_modes.add(COLOR_MODE_COLOR_TEMP)
            if not self._attr_supported_color_modes:
                if self._capacity & mc.LIGHT_CAPACITY_LUMINANCE:
                    self._attr_supported_color_modes.add(COLOR_MODE_BRIGHTNESS)
                else:
                    self._attr_supported_color_modes.add(COLOR_MODE_ONOFF)


    @property
    def supported_features(self):
        return self._attr_supported_features


    @property
    def supported_color_modes(self):
        return self._attr_supported_color_modes


    @property
    def color_mode(self):
        return self._attr_color_mode


    @property
    def brightness(self):
        return self._attr_brightness


    @property
    def rgb_color(self):
        return self._attr_rgb_color


    @property
    def hs_color(self):
        return self._attr_hs_color


    @property
    def color_temp(self):
        return self._attr_color_temp


    @property
    def effect_list(self) -> list[str] | None:
        return self.device.effect_list


    @property
    def effect(self) -> str | None:
        return self._attr_effect


    async def async_turn_on(self, **kwargs) -> None:

        light = dict(self._light)
        capacity = 0
        # Color is taken from either of these 2 values, but not both.
        if ATTR_HS_COLOR in kwargs:
            h, s = kwargs[ATTR_HS_COLOR]
            light[mc.KEY_RGB] = _rgb_to_int(color_util.color_hs_to_RGB(h, s))
            light.pop(mc.KEY_TEMPERATURE, None)
            capacity |= mc.LIGHT_CAPACITY_RGB
        elif ATTR_RGB_COLOR in kwargs:
            rgb = kwargs[ATTR_RGB_COLOR]
            light[mc.KEY_RGB] = _rgb_to_int(rgb)
            light.pop(mc.KEY_TEMPERATURE, None)
            capacity |= mc.LIGHT_CAPACITY_RGB
        elif ATTR_COLOR_TEMP in kwargs:
            # map mireds: min_mireds -> 100 - max_mireds -> 1
            mired = kwargs[ATTR_COLOR_TEMP]
            norm_value = (mired - self.min_mireds) / (self.max_mireds - self.min_mireds)
            temperature = 100 - (norm_value * 99)
            light[mc.KEY_TEMPERATURE] = _sat_1_100(temperature) # meross wants temp between 1-100
            light.pop(mc.KEY_RGB, None)
            capacity |= mc.LIGHT_CAPACITY_TEMPERATURE

        if self._capacity & mc.LIGHT_CAPACITY_LUMINANCE:
            # Brightness must always be set, so take previous luminance if not explicitly set now.
            capacity |= mc.LIGHT_CAPACITY_LUMINANCE
            if ATTR_BRIGHTNESS in kwargs:
                light[mc.KEY_LUMINANCE] = _sat_1_100(kwargs[ATTR_BRIGHTNESS] * 100 // 255)

        if ATTR_EFFECT in kwargs:
            effect_id = self.device.effect_dict_names.get(kwargs[ATTR_EFFECT], None)
            if effect_id is not None:
                light[mc.KEY_EFFECT] = effect_id
            else:
                light.pop(mc.KEY_EFFECT, None)
        else:
            light.pop(mc.KEY_EFFECT, None)

        light[mc.KEY_CAPACITY] = capacity

        if self._usetogglex:
            # since lights could be repeatedtly 'async_turn_on' when changing attributes
            # we avoid flooding the device with unnecessary messages
            # this is probably unneeded since any light payload sent seems to turn on the light
            if not self.is_on:
                await super().async_turn_on(**kwargs)
        else:
            light[mc.KEY_ONOFF] = 1

        self.device.request(
            namespace=mc.NS_APPLIANCE_CONTROL_LIGHT,
            method=mc.METHOD_SET,
            payload={mc.KEY_LIGHT: light})
        #87: @nao-pon bulbs need a 'double' send when setting Temp
        if ATTR_COLOR_TEMP in kwargs:
            if self.device.descriptor.firmware.get(mc.KEY_VERSION) == '2.1.2':
                self.device.request(
                    namespace=mc.NS_APPLIANCE_CONTROL_LIGHT,
                    method=mc.METHOD_SET,
                    payload={mc.KEY_LIGHT: light})


    async def async_turn_off(self, **kwargs) -> None:

        if self._usetogglex:
            # we suppose we have to 'toggle(x)'
            await super().async_turn_off(**kwargs)
        else:
            self.device.request(
                namespace=mc.NS_APPLIANCE_CONTROL_LIGHT,
                method=mc.METHOD_SET,
                payload={mc.KEY_LIGHT: { mc.KEY_CHANNEL: self.id, mc.KEY_ONOFF: 0}})


    def update_light(self, light: dict) -> None:
        """
        update light entity state with fresh data from the device
        """
        if self._light != light:
            self._light = light

            capacity = light.get(mc.KEY_CAPACITY, 0)
            self._attr_color_mode = COLOR_MODE_UNKNOWN

            if mc.KEY_EFFECT in light:
                self._attr_effect = self.device.effect_dict_ids.get(light[mc.KEY_EFFECT], None)
            else:
                self._attr_effect = None

            if capacity & mc.LIGHT_CAPACITY_LUMINANCE:
                self._attr_color_mode = COLOR_MODE_BRIGHTNESS
                self._attr_brightness = light.get(mc.KEY_LUMINANCE, 0) * 255 // 100
            else:
                self._attr_brightness = None

            if capacity & mc.LIGHT_CAPACITY_TEMPERATURE:
                self._attr_color_mode = COLOR_MODE_COLOR_TEMP
                self._attr_color_temp = ((100 - light.get(mc.KEY_TEMPERATURE, 0)) / 99) * \
                    (self.max_mireds - self.min_mireds) + self.min_mireds
            else:
                self._attr_color_temp = None

            if capacity & mc.LIGHT_CAPACITY_RGB:
                self._attr_color_mode = COLOR_MODE_RGB
                self._attr_rgb_color = _int_to_rgb(light.get(mc.KEY_RGB, 0))
                self._attr_hs_color = color_util.color_RGB_to_hs(*self._attr_rgb_color)
            else:
                self._attr_rgb_color = None
                self._attr_hs_color = None

            onoff = light.get(mc.KEY_ONOFF)
            if onoff is not None:
                self._attr_state = STATE_ON if onoff else STATE_OFF

            if self.hass and self.enabled and ((onoff is not None) or (self._attr_state is STATE_ON)):
                # since the light payload could be processed before the relative 'togglex'
                # here we'll flush only when the lamp is 'on' to avoid intra-updates to HA states.
                # when the togglex will arrive, the _light (attributes) will be already set
                # and HA will save a consistent state (hopefully..we'll see)
                self.async_write_ha_state()


    def update_effect_list(self):
        """
        the list of available effects was changed (context at device level)
        so we'll just tell HA to update the state
        """
        if self.device.effect_list:
            self._attr_supported_features = self._attr_supported_features | SUPPORT_EFFECT
        else:
            self._attr_supported_features = self._attr_supported_features & ~SUPPORT_EFFECT
        if self.hass and self.enabled:
            self.async_write_ha_state()



class MerossLanDNDLight(_MerossToggle, LightEntity):
    """
    light entity representing the device DND feature usually implemented
    through a light feature (presence light or so)
    This is a different representation (user can choose from configuration UI)
    of the same DND feature implemented through MerossLanDND(switch)
    """
    PLATFORM = PLATFORM_LIGHT

    _attr_supported_color_modes = { COLOR_MODE_ONOFF }


    def __init__(self, device: MerossDevice):
        super().__init__(device, DND_ID, mc.KEY_DNDMODE, None, None)


    @property
    def supported_color_modes(self):
        return self._attr_supported_color_modes


    @property
    def color_mode(self):
        return COLOR_MODE_ONOFF


    @property
    def entity_category(self) -> str | None:
        return ENTITY_CATEGORY_CONFIG


    async def async_turn_on(self, **kwargs) -> None:
        def _ack_callback():
            self.update_state(STATE_ON)

        await self.device.async_http_request(
            mc.NS_APPLIANCE_SYSTEM_DNDMODE,
            mc.METHOD_SET,
            {mc.KEY_DNDMODE: {mc.KEY_MODE: 0}},
            _ack_callback
        )


    async def async_turn_off(self, **kwargs) -> None:
        def _ack_callback():
            self.update_state(STATE_OFF)

        await self.device.async_http_request(
            mc.NS_APPLIANCE_SYSTEM_DNDMODE,
            mc.METHOD_SET,
            {mc.KEY_DNDMODE: {mc.KEY_MODE: 1}},
            _ack_callback
        )


    def update_onoff(self, onoff) -> None:
        """
        intercept base update_onoff to invert the dndmode flag/state
        """
        self.update_state(STATE_OFF if onoff else STATE_ON)