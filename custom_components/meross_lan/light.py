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

    PLATFORM = PLATFORM_LIGHT

    def __init__(self, device: MerossDevice, id: object):
        # suppose we use 'togglex' to switch the light
        super().__init__(
            device, id, None,
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
        self._light = dict()
        self._color_temp = None
        self._hs_color = None
        self._brightness = None

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
        return self._brightness


    @property
    def hs_color(self):
        return self._hs_color


    @property
    def color_temp(self):
        return self._color_temp


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
            self._light[mc.KEY_RGB] = _rgb_to_int(color_hs_to_RGB(h, s))
            self._light.pop(mc.KEY_TEMPERATURE, None)
            capacity |= CAPACITY_RGB
        elif ATTR_COLOR_TEMP in kwargs:
            # map mireds: min_mireds -> 100 - max_mireds -> 1
            mired = kwargs[ATTR_COLOR_TEMP]
            norm_value = (mired - self.min_mireds) / (self.max_mireds - self.min_mireds)
            temperature = 100 - (norm_value * 99)
            self._light[mc.KEY_TEMPERATURE] = _sat_1_100(temperature) # meross wants temp between 1-100
            self._light.pop(mc.KEY_RGB, None)
            capacity |= CAPACITY_TEMPERATURE

        if self._capacity & CAPACITY_LUMINANCE:
            capacity |= CAPACITY_LUMINANCE
            # Brightness must always be set, so take previous luminance if not explicitly set now.
            if ATTR_BRIGHTNESS in kwargs:
                self._light[mc.KEY_LUMINANCE] = _sat_1_100(kwargs[ATTR_BRIGHTNESS] * 100 // 255)

        self._light[mc.KEY_CAPACITY] = capacity

        if self._light.get(mc.KEY_ONOFF) is None:
            # since lights could be repeatedtly 'async_turn_on' when changing attributes
            # we avoid flooding the device with unnecessary messages
            if not self.is_on:
                await super().async_turn_on(**kwargs)
        else:
            self._light[mc.KEY_ONOFF] = 1

        self._device.request(
            namespace=mc.NS_APPLIANCE_CONTROL_LIGHT,
            method=mc.METHOD_SET,
            payload={mc.KEY_LIGHT: self._light})


    async def async_turn_off(self, **kwargs) -> None:

        if self._light.get(mc.KEY_ONOFF) is None:
            # we suppose we have to 'toggle(x)'
            await super().async_turn_off(**kwargs)
        else:
            self._light[mc.KEY_ONOFF] = 0
            self._device.request(
                namespace=mc.NS_APPLIANCE_CONTROL_LIGHT,
                method=mc.METHOD_SET,
                payload={mc.KEY_LIGHT: self._light})


    def _set_light(self, light: dict) -> None:
        if self._light != light:
            self._light = light

            capacity = light.get(mc.KEY_CAPACITY, 0)

            if capacity & CAPACITY_LUMINANCE:
                self._brightness = light.get(mc.KEY_LUMINANCE, 0) * 255 // 100
            else:
                self._brightness = None

            if capacity & CAPACITY_TEMPERATURE:
                self._color_temp = ((100 - light.get(mc.KEY_TEMPERATURE, 0)) / 99) * \
                    (self.max_mireds - self.min_mireds) + self.min_mireds
            else:
                self._color_temp = None

            if capacity & CAPACITY_RGB:
                r, g, b = _int_to_rgb(light.get(mc.KEY_RGB, 0))
                self._hs_color = color_RGB_to_hs(r, g, b)
            else:
                self._hs_color = None

            onoff = light.get(mc.KEY_ONOFF)
            if onoff is not None:
                self._state = STATE_ON if onoff else STATE_OFF

            if self.hass and self.enabled and ((onoff is not None) or (self._state is STATE_ON)):
                # since the light payload could be processed before the relative 'togglex'
                # here we'll flush only when the lamp is 'on' to avoid intra-updates to HA states.
                # when the togglex will arrive, the _light (attributes) will be already set
                # and HA will save a consistent state (hopefully..we'll see)
                self.async_write_ha_state()




