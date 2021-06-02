from typing import Union, Tuple

from homeassistant.components.light import (
    LightEntity,
    SUPPORT_BRIGHTNESS, SUPPORT_COLOR, SUPPORT_COLOR_TEMP, SUPPORT_WHITE_VALUE,
    SUPPORT_EFFECT, SUPPORT_FLASH, SUPPORT_TRANSITION,
    ATTR_HS_COLOR, ATTR_COLOR_TEMP, ATTR_RGB_COLOR,
    ATTR_BRIGHTNESS, ATTR_TRANSITION,
    ATTR_MIN_MIREDS, ATTR_MAX_MIREDS,
)
from homeassistant.util.color import (
    color_hs_to_RGB, color_RGB_to_hs
)

from homeassistant.const import STATE_UNKNOWN, STATE_ON, STATE_OFF

from .merossclient import const as mc
from .meross_device import MerossDevice
from .meross_entity import _MerossToggle, platform_setup_entry, platform_unload_entry
from .const import (
    PLATFORM_LIGHT,
)

CAPACITY_RGB = 1
CAPACITY_TEMPERATURE = 2
CAPACITY_LUMINANCE = 4
CAPACITY_RGB_LUMINANCE = 5
CAPACITY_TEMPERATURE_LUMINANCE = 6


async def async_setup_entry(hass: object, config_entry: object, async_add_devices):
    platform_setup_entry(hass, config_entry, async_add_devices, PLATFORM_LIGHT)

async def async_unload_entry(hass: object, config_entry: object) -> bool:
    return platform_unload_entry(hass, config_entry, PLATFORM_LIGHT)


def rgb_to_int(rgb: Union[tuple, dict, int]) -> int:  # pylint: disable=unsubscriptable-object
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

def int_to_rgb(rgb: int) -> Tuple[int, int, int]:
    return (rgb & 16711680) >> 16, (rgb & 65280) >> 8, (rgb & 255)


class MerossLanLight(_MerossToggle, LightEntity):

    PLATFORM = PLATFORM_LIGHT

    def __init__(self, device: MerossDevice, p_light: dict):
        # suppose we use 'togglex' to switch the light
        super().__init__(
            device, p_light.get(mc.KEY_CHANNEL, 0), None,
            mc.NS_APPLIANCE_CONTROL_TOGGLEX, mc.KEY_TOGGLEX)
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
        self._light = p_light

        self._capacity = device.descriptor.ability.get(
            mc.NS_APPLIANCE_CONTROL_LIGHT, {}).get(
                mc.KEY_CAPACITY, CAPACITY_LUMINANCE)

        self._supported_features = (SUPPORT_COLOR if self._capacity & CAPACITY_RGB else 0)\
            | (SUPPORT_COLOR_TEMP if self._capacity & CAPACITY_TEMPERATURE else 0)\
            | (SUPPORT_BRIGHTNESS if self._capacity & CAPACITY_LUMINANCE else 0)


    @property
    def supported_features(self):
        return self._supported_features


    @property
    def brightness(self):
        """Return the brightness of this light between 0..255."""
        luminance = self._light.get(mc.KEY_LUMINANCE)
        return None if luminance is None else luminance * 255 // 100


    @property
    def hs_color(self):
        """Return the hue and saturation color value [float, float]."""
        rgb = self._light.get(mc.KEY_RGB)
        if rgb is not None:
            r, g, b = int_to_rgb(rgb)
            return color_RGB_to_hs(r, g, b)
        return None


    @property
    def color_temp(self):
        """Return the CT color value in mireds."""
        temp = self._light.get(mc.KEY_TEMPERATURE)
        return None if temp is None else ((100 - temp) / 100) * (self.max_mireds - self.min_mireds) + self.min_mireds


    @property
    def white_value(self):
        """Return the white value of this light between 0..255."""
        return None


    @property
    def effect_list(self):
        """Return the list of supported effects."""
        return None


    @property
    def effect(self):
        """Return the current effect."""
        return None


    async def async_turn_on(self, **kwargs) -> None:

        capacity = 0
        # Color is taken from either of these 2 values, but not both.
        if ATTR_HS_COLOR in kwargs:
            h, s = kwargs[ATTR_HS_COLOR]
            self._light[mc.KEY_RGB] = rgb_to_int(color_hs_to_RGB(h, s))
            self._light.pop(mc.KEY_TEMPERATURE, None)
            capacity |= CAPACITY_RGB
        elif ATTR_COLOR_TEMP in kwargs:
            mired = kwargs[ATTR_COLOR_TEMP]
            norm_value = (mired - self.min_mireds) / (self.max_mireds - self.min_mireds)
            temperature = 100 - (norm_value * 100)
            self._light[mc.KEY_TEMPERATURE] = int(temperature)
            self._light.pop(mc.KEY_RGB, None)
            capacity |= CAPACITY_TEMPERATURE

        if self._capacity & CAPACITY_LUMINANCE:
            capacity |= CAPACITY_LUMINANCE
            # Brightness must always be set, so take previous luminance if not explicitly set now.
            if ATTR_BRIGHTNESS in kwargs:
                self._light[mc.KEY_LUMINANCE] = kwargs[ATTR_BRIGHTNESS] * 100 // 255

        self._light[mc.KEY_CAPACITY] = capacity

        if self._light.get(mc.KEY_ONOFF) is None:
            # since lights could be repeatedtly 'async_turn_on' when changing attributes
            # we avoid flooding the device with unnecessary messages
            if not self.is_on:
                super().async_turn_on(**kwargs)
        else:
            self._light[mc.KEY_ONOFF] = 1

        self._device.request(
            namespace=mc.NS_APPLIANCE_CONTROL_LIGHT,
            method=mc.METHOD_SET,
            payload={mc.KEY_LIGHT: self._light})


    async def async_turn_off(self, **kwargs) -> None:

        if self._light.get(mc.KEY_ONOFF) is None:
            # we suppose we have to 'toggle(x)'
            super().async_turn_off(**kwargs)
        else:
            self._light[mc.KEY_ONOFF] = 0
            self._device.request(
                namespace=mc.NS_APPLIANCE_CONTROL_LIGHT,
                method=mc.METHOD_SET,
                payload={mc.KEY_LIGHT: self._light})


    def _set_light(self, light: dict) -> None:
        self._light = light
        onoff = light.get(mc.KEY_ONOFF)
        if onoff is not None:
            self._state = STATE_ON if onoff else STATE_OFF
        if self.hass and self.enabled:
            self.async_write_ha_state()




