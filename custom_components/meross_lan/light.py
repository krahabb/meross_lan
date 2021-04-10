from typing import Any, Callable, Dict, List, Optional, Union, Tuple

from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.typing import HomeAssistantType
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

from .meross_entity import _MerossEntity
from .const import (
    DOMAIN,
    CONF_DEVICE_ID,
    NS_APPLIANCE_CONTROL_LIGHT, NS_APPLIANCE_CONTROL_TOGGLEX,
    METHOD_SET, METHOD_GET,
)
from .logger import LOGGER

CAPACITY_RGB = 1
CAPACITY_TEMPERATURE = 2
CAPACITY_LUMINANCE = 4
CAPACITY_RGB_LUMINANCE = 5
CAPACITY_TEMPERATURE_LUMINANCE = 6


async def async_setup_entry(hass: HomeAssistantType, config_entry: ConfigEntry, async_add_devices):
    device_id = config_entry.data[CONF_DEVICE_ID]
    device = hass.data[DOMAIN].devices[device_id]
    async_add_devices([entity for entity in device.entities.values() if isinstance(entity, MerossLanLight)])
    LOGGER.debug("async_setup_entry device_id = %s - platform = light", device_id)
    return

async def async_unload_entry(hass: object, config_entry: object) -> bool:
    LOGGER.debug("async_unload_entry device_id = %s - platform = light", config_entry.data[CONF_DEVICE_ID])
    return True



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


class MerossLanLight(_MerossEntity, LightEntity):
    def __init__(self, meross_device: object, channel: int):
        super().__init__(meross_device, channel, None)
        self._light = {
			"onoff": 0,
			"capacity": CAPACITY_LUMINANCE,
			"channel": channel,
			#"rgb": 16753920,
			#"temperature": 100,
			"luminance": 100,
			"transform": 0,
            "gradual": 0
		}
        self._payload = {"light": self._light}

        self._capacity = meross_device.ability.get(NS_APPLIANCE_CONTROL_LIGHT, {}).get("capacity", CAPACITY_LUMINANCE)

        self._supported_features = (SUPPORT_COLOR if self._capacity & CAPACITY_RGB else 0)\
            | (SUPPORT_COLOR_TEMP if self._capacity & CAPACITY_TEMPERATURE else 0)\
            | (SUPPORT_BRIGHTNESS if self._capacity & CAPACITY_LUMINANCE else 0)

        meross_device.has_lights = True


    @property
    def supported_features(self):
        return self._supported_features


    @property
    def brightness(self):
        """Return the brightness of this light between 0..255."""
        luminance = self._light.get("luminance")
        return None if luminance is None else luminance * 255 / 100


    @property
    def hs_color(self):
        """Return the hue and saturation color value [float, float]."""
        rgb = self._light.get("rgb")
        if rgb is not None:
            r, g, b = int_to_rgb(rgb)
            return color_RGB_to_hs(r, g, b)
        return None


    @property
    def color_temp(self):
        """Return the CT color value in mireds."""
        temp = self._light.get("temperature")
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


    @property
    def is_on(self) -> bool:
        return self._state == STATE_ON


    async def async_turn_on(self, **kwargs) -> None:

        capacity = 0
        # Color is taken from either of these 2 values, but not both.
        if ATTR_HS_COLOR in kwargs:
            h, s = kwargs[ATTR_HS_COLOR]
            self._light["rgb"] = rgb_to_int(color_hs_to_RGB(h, s))
            self._light.pop("temperature", None)
            capacity |= CAPACITY_RGB
        elif ATTR_COLOR_TEMP in kwargs:
            mired = kwargs[ATTR_COLOR_TEMP]
            norm_value = (mired - self.min_mireds) / (self.max_mireds - self.min_mireds)
            temperature = 100 - (norm_value * 100)
            self._light["temperature"] = temperature
            self._light.pop("rgb", None)
            capacity |= CAPACITY_TEMPERATURE

        if self._capacity & CAPACITY_LUMINANCE:
            capacity |= CAPACITY_LUMINANCE
            # Brightness must always be set, so take previous luminance if not explicitly set now.
            if ATTR_BRIGHTNESS in kwargs:
                self._light["luminance"] = kwargs[ATTR_BRIGHTNESS] * 100 / 255

        self._light["capacity"] = capacity

        self._internal_send(onoff=1)
        return


    async def async_turn_off(self, **kwargs) -> None:
        self._internal_send(onoff = 0)
        return


    def _set_onoff(self, onoff) -> None:
        newstate = STATE_ON if onoff else STATE_OFF
        if self._state != newstate:
            self._state = newstate
            self._light["onoff"] = 1 if onoff else 0
            if self.enabled:
                self.async_write_ha_state()
        return


    def _set_light(self, light: dict) -> None:
        self._light = light
        self._payload["light"] = light
        self._state = STATE_ON if light.get("onoff") else STATE_OFF
        if self.enabled:
            self.async_write_ha_state()
        return


    def _internal_send(self, onoff: int):
        if NS_APPLIANCE_CONTROL_TOGGLEX in self._meross_device.ability:
            # since lights could be repeatedtly 'async_turn_on' when changing attributes
            # we avoid flooding the device with unnecessary messages
            if (onoff == 0) or (not self.is_on):
                self._meross_device.togglex_set(channel = self._channel, ison = onoff)

        self._light["onoff"] = onoff
        self._meross_device.mqtt_publish(
            namespace=NS_APPLIANCE_CONTROL_LIGHT,
            method=METHOD_SET,
            payload=self._payload)


