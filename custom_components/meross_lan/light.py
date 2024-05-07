import asyncio
import typing

from homeassistant.components import light
from homeassistant.components.light import (
    ATTR_BRIGHTNESS,
    ATTR_COLOR_TEMP,
    ATTR_EFFECT,
    ATTR_RGB_COLOR,
    ColorMode,
    LightEntityFeature,
)
import homeassistant.util.color as color_util

from . import const as mlc, meross_entity as me
from .helpers import schedule_async_callback
from .helpers.namespaces import EntityPollingStrategy, SmartPollingStrategy
from .merossclient import const as mc, get_element_by_key_safe, request_get

if typing.TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

    from .meross_device import DigestParseFunc, MerossDevice

ATTR_TOGGLEX_MODE = "togglex_mode"
#    map light Temperature effective range to HA mired(s):
#    right now we'll use a const approach since it looks like
#    any light bulb out there carries the same specs
#    MIRED <> 1000000/TEMPERATURE[K]
#    (thanks to @nao-pon #87)
MSLANY_MIRED_MIN = 153  # math.floor(1/(6500/1000000))
MSLANY_MIRED_MAX = 371  # math.ceil(1/(2700/1000000))

MSL_LUMINANCE_MIN = 1
MSL_LUMINANCE_MAX = 100
MSL_LUMINANCE_SCALE = (MSL_LUMINANCE_MAX - MSL_LUMINANCE_MIN) / 254


async def async_setup_entry(
    hass: "HomeAssistant", config_entry: "ConfigEntry", async_add_devices
):
    me.platform_setup_entry(hass, config_entry, async_add_devices, light.DOMAIN)


def rgb_to_native(rgb: tuple[int, int, int]) -> int:
    """
    Convert an HA RGB tuple to a device native value (int).
    This function expects a tuple[int, int, int] but can fall-back to
    parsing other types
    """
    try:
        return (rgb[0] << 16) + (rgb[1] << 8) + (rgb[2])
    except Exception:
        # try a bit of euristics:
        if isinstance(rgb, int):
            return rgb
        try:
            if isinstance(rgb, tuple):
                red, green, blue = rgb
            else:  # assume dict
                red = rgb["red"]
                green = rgb["green"]
                blue = rgb["blue"]
            # even if HA states the tuple should be int we have float(s) in the wild (#309)
            return (round(red) << 16) + (round(green) << 8) + round(blue)
        except Exception as exception:
            raise ValueError(
                f"Invalid value for RGB (value: {str(rgb)} - type: {rgb.__class__.__name__} - error: {str(exception)})"
            )


def native_to_rgb(rgb: int):
    return (rgb & 16711680) >> 16, (rgb & 65280) >> 8, (rgb & 255)


def rgbw_to_native(rgb: tuple[int, int, int], brightness: int | None) -> int:
    """
    Convert an HA RGB tuple to a device native value (int).
    When converting, the White channel is scaled to the current luminance
    value since the device (my msl320cp) looks like having a different
    processing hardware for the color and the white channel and the white channel
    is not scaled/amplified by the luminance parameter (so we have to scale it in
    the rgb field).
    The device native value is processed by the device by extracting the white portion from
    the RGB (3-byte int) value and using the remainder RGB to drive the RGB led while
    the white channel is used to drive the CW white leds.
    - rgb: the RGB tuple from HA light.turn_on service call
    - brightness: the HA brightness parameter (0..255)
    """
    r, g, b, w = color_util.color_rgb_to_rgbw(*rgb)
    r, g, b = color_util.color_rgbw_to_rgb(r, g, b, round(w * (brightness or 0) / 255))
    return (r << 16) + (g << 8) + b


def native_to_rgbw(rgb: int, brightness: int | None):
    if brightness:
        r = (rgb & 16711680) >> 16
        g = (rgb & 65280) >> 8
        b = rgb & 255
        r, g, b, w = color_util.color_rgb_to_rgbw(r, g, b)
        w = min(round(w * 255 / brightness), 255)
        return color_util.color_rgbw_to_rgb(r, g, b, w)
    return (rgb & 16711680) >> 16, (rgb & 65280) >> 8, (rgb & 255)


def brightness_to_native(brightness: int):
    return MSL_LUMINANCE_MIN + round((brightness - 1) * MSL_LUMINANCE_SCALE)


def native_to_brightness(luminance: int):
    return 1 + round((luminance - MSL_LUMINANCE_MIN) / MSL_LUMINANCE_SCALE)


def _sat_1_100(value):
    if value > 100:
        return 100
    elif value < 1:
        return 1
    else:
        return round(value)


class MLLightBase(me.MerossToggle, light.LightEntity):
    """
    base 'abstract' class for meross light entities handling
    either
    NS_APPLIANCE_CONTROL_LIGHT -> specialized in MLLight
    NS_APPLIANCE_CONTROL_DIFFUSER_LIGHT -> specialized in MLDiffuserLight
    """

    PLATFORM = light.DOMAIN
    manager: "MerossDevice"

    namespace: typing.ClassVar[str]
    key_namespace = mc.KEY_LIGHT

    # define our own EFFECT_OFF semantically compatible to the 'new' HA core
    # symbol in order to mantain a sort of backward compatibility
    EFFECT_OFF = "off"

    # internal copy of the actual meross light state
    _light: dict

    # msl320cp (msl320 pro) has a weird rgb parameter carrying both rgb and white
    # that needs a bit of funny processing
    _rgbw_supported: bool
    # this carries the same as rgb_color when the light has _rgbw_supported and color mode is RGB
    # it just acts as a fast path for condition checks
    _rgbw_color: tuple[int, int, int] | None

    # HA core entity attributes:
    brightness: int | None
    color_mode: ColorMode
    color_temp: int | None
    effect: str | None
    effect_list: list[str] | None
    max_mireds: int = MSLANY_MIRED_MAX
    min_mireds: int = MSLANY_MIRED_MIN
    rgb_color: tuple[int, int, int] | None
    supported_color_modes: set[ColorMode]
    supported_features: LightEntityFeature
    # TODO: add implementation for temp_kelvin and min-max_kelvin

    __slots__ = (
        "_light",
        "_rgbw_supported",
        "_rgbw_color",
        "brightness",
        "color_mode",
        "color_temp",
        "effect",
        "effect_list",
        "rgb_color",
        "supported_features",
    )

    def __init__(
        self,
        manager: "MerossDevice",
        digest: dict,
        effect_list: list[str] | None = None,
    ):
        self._light = {}
        self._rgbw_supported = manager.descriptor.type.startswith(mc.TYPE_MSL320_PRO)
        self._rgbw_color = None
        self.brightness = None
        self.color_mode = ColorMode.UNKNOWN
        self.color_temp = None
        self.effect = None
        self.rgb_color = None
        if effect_list is None:
            self.effect_list = None
            self.supported_features = LightEntityFeature(0)
        else:
            self.effect_list = effect_list
            self.supported_features = LightEntityFeature.EFFECT

        super().__init__(manager, digest.get(mc.KEY_CHANNEL))
        manager.register_parser(self.namespace, self)

    # interface: MerossToggle
    def set_unavailable(self):
        self._light = {}
        self._rgbw_color = None
        self.brightness = None
        self.color_mode = ColorMode.UNKNOWN
        self.color_temp = None
        self.effect = None
        self.rgb_color = None
        super().set_unavailable()

    # interface: self
    def _flush_light(self, _light: dict):
        if mc.KEY_ONOFF in _light:
            self.is_on = _light[mc.KEY_ONOFF]

        # color_mode would need to be set in inherited _flush_light
        if mc.KEY_LUMINANCE in _light:
            luminance = _light[mc.KEY_LUMINANCE]
            # try preserve the original brightness as set in latest async_turn_on
            if (self.brightness is None) or (
                luminance != brightness_to_native(self.brightness)
            ):
                self.brightness = native_to_brightness(luminance)
        else:
            self.brightness = None

        if mc.KEY_TEMPERATURE in _light:
            self.color_temp = ((100 - _light[mc.KEY_TEMPERATURE]) / 99) * (
                self.max_mireds - self.min_mireds
            ) + self.min_mireds
        else:
            self.color_temp = None

        if mc.KEY_RGB in _light:
            if self._rgbw_supported:
                rgb = _light[mc.KEY_RGB]
                if rgb:
                    # try preserve the original rgb as set in latest async_turn_on
                    if (not self.rgb_color) or (
                        rgb != rgbw_to_native(self.rgb_color, self.brightness)
                    ):
                        self.rgb_color = native_to_rgbw(rgb, self.brightness)
                    self._rgbw_color = (
                        self.rgb_color
                        if (self.color_mode is ColorMode.RGB)
                        else None
                    )
                else:
                    # rgb is set to 0 when turning off so
                    # we'd want to preserve whatever self._rgbw_color
                    self.rgb_color = self._rgbw_color
            else:
                self.rgb_color = native_to_rgb(_light[mc.KEY_RGB])
        else:
            self.rgb_color = None

        self._light = _light
        self.flush_state()

    def _parse_light(self, payload: dict):
        if self._light != payload:
            self._flush_light(payload)


class MLLight(MLLightBase):
    """
    light entity for Meross bulbs and any device supporting light api
    identified from devices carrying 'light' node in SYSTEM_ALL payload and/or
    NS_APPLIANCE_CONTROL_LIGHT in abilities
    """

    manager: "MerossDevice"

    namespace = mc.NS_APPLIANCE_CONTROL_LIGHT

    __slots__ = (
        "_togglex_mode",
        "supported_color_modes",
    )

    def __init__(
        self,
        manager: "MerossDevice",
        digest: dict,
        effect_list: list[str] | None = None,
    ):
        # we'll use the (eventual) togglex payload to
        # see if we have to toggle the light by togglex or so
        # with msl120j (fw 3.1.4) I've discovered that any 'light' payload sent will turn on the light
        # (disregarding any 'onoff' field inside).
        # The msl120j never 'pushes' an 'onoff' field in the light payload while msl120b (fw 2.1.16)
        # does that.
        # we used a 'conservative' approach here where we always toggled by togglex (if presented in digest)
        # and kindly ignore any 'onoff' in the 'light' payload (except digest didn't presented togglex)
        # also (issue #218) the newer mss560-570 dimmer switches are implemented as 'light' devices with ToggleX
        # api and show a glitch when used this way (ToggleX + Light)
        # State-of-the-art is now to auto-detect (when booting the entity) what is the behavior
        descriptor = manager.descriptor
        ability = descriptor.ability

        """
        capacity is set in abilities when using mc.NS_APPLIANCE_CONTROL_LIGHT
        """
        capacity = ability[mc.NS_APPLIANCE_CONTROL_LIGHT].get(
            mc.KEY_CAPACITY, mc.LIGHT_CAPACITY_LUMINANCE
        )
        self.supported_color_modes = supported_color_modes = set()
        if capacity & mc.LIGHT_CAPACITY_RGB:
            supported_color_modes.add(ColorMode.RGB)  # type: ignore
        if capacity & mc.LIGHT_CAPACITY_TEMPERATURE:
            supported_color_modes.add(ColorMode.COLOR_TEMP)  # type: ignore
        if not supported_color_modes:
            if capacity & mc.LIGHT_CAPACITY_LUMINANCE:
                supported_color_modes.add(ColorMode.BRIGHTNESS)  # type: ignore
            else:
                supported_color_modes.add(ColorMode.ONOFF)  # type: ignore

        super().__init__(manager, digest, effect_list)

    async def async_turn_on(self, **kwargs):
        if not kwargs:
            await self.async_request_onoff(1)
            return

        _light = dict(self._light)
        if mc.KEY_ONOFF in _light:
            _light[mc.KEY_ONOFF] = 1
        capacity = _light.get(mc.KEY_CAPACITY, 0) | mc.LIGHT_CAPACITY_LUMINANCE
        # luminance must always be set in payload
        if ATTR_BRIGHTNESS in kwargs:
            self.brightness = kwargs[ATTR_BRIGHTNESS]
            _light[mc.KEY_LUMINANCE] = brightness_to_native(self.brightness)
            if self._rgbw_color:
                # adjust the white component in light.rgb since it is not
                # controlled by light.luminance
                _light[mc.KEY_RGB] = rgbw_to_native(self._rgbw_color, self.brightness)
        elif not _light.get(mc.KEY_LUMINANCE, 0):
            self.brightness = 255
            _light[mc.KEY_LUMINANCE] = MSL_LUMINANCE_MAX

        if ATTR_RGB_COLOR in kwargs:
            self.rgb_color = kwargs[ATTR_RGB_COLOR]
            if self._rgbw_supported:
                self._rgbw_color = self.rgb_color
                _light[mc.KEY_RGB] = rgbw_to_native(self._rgbw_color, self.brightness)
            else:
                _light[mc.KEY_RGB] = rgb_to_native(self.rgb_color)
            capacity |= mc.LIGHT_CAPACITY_RGB
            capacity &= ~mc.LIGHT_CAPACITY_TEMPERATURE
        elif ATTR_COLOR_TEMP in kwargs:
            # map mireds: min_mireds -> 100 - max_mireds -> 1
            norm_value = (kwargs[ATTR_COLOR_TEMP] - self.min_mireds) / (
                self.max_mireds - self.min_mireds
            )
            _light[mc.KEY_TEMPERATURE] = _sat_1_100(100 - (norm_value * 99))
            capacity |= mc.LIGHT_CAPACITY_TEMPERATURE
            capacity &= ~mc.LIGHT_CAPACITY_RGB

        if ATTR_EFFECT in kwargs:
            _light[mc.KEY_EFFECT] = self.effect_list.index(kwargs[ATTR_EFFECT])  # type: ignore
            capacity |= mc.LIGHT_CAPACITY_EFFECT
        else:
            _light.pop(mc.KEY_EFFECT, None)
            capacity &= ~mc.LIGHT_CAPACITY_EFFECT

        _light[mc.KEY_CAPACITY] = capacity

        if await self.async_request_light_ack(_light):
            self._flush_light(_light)
            if not self.is_on:
                await self._async_ensure_on()

        # 87: @nao-pon bulbs need a 'double' send when setting Temp
        if ATTR_COLOR_TEMP in kwargs:
            if self.manager.descriptor.firmwareVersion == "2.1.2":
                await self.async_request_light_ack(_light)

    async def async_turn_off(self, **kwargs):
        if self._rgbw_color:
            # we need to 'patch' the color in _light payload
            # since the device doesn't turn off itself the white channel
            # (at least on my msl320cp 'Pro' strip)
            rgb = rgbw_to_native(self._rgbw_color, None)
            if rgb != self._light[mc.KEY_RGB]:
                _light = dict(self._light)
                _light[mc.KEY_RGB] = 0
                if await self.async_request_light_ack(_light):
                    # directly setting the _light without
                    # updating self.rgb_color will allow
                    # us to restore the white channel on turn_on
                    # (see self.update_on_off)
                    self._light[mc.KEY_RGB] = 0

        await self.async_request_onoff(0)

    def update_onoff(self, onoff):
        if self.is_on != onoff:
            if self._rgbw_color:
                if onoff:
                    # try restore the white channel in rgb
                    rgb = rgbw_to_native(self._rgbw_color, self.brightness)
                    if rgb != self._light[mc.KEY_RGB]:
                        schedule_async_callback(
                            self.hass, 0, self._async_patch_rgbw, rgb
                        )
                else:
                    # we need to 'patch' the color in _light payload
                    # since the device doesn't turn off itself the white channel
                    # (at least on my msl320cp 'Pro' strip)
                    r, g, b, w = color_util.color_rgb_to_rgbw(
                        *native_to_rgb(self._light[mc.KEY_RGB])
                    )
                    if w:

                        async def _async_patch_rgbw_off():
                            await self._async_patch_rgbw(0)
                            await self.async_request_onoff(0)

                        schedule_async_callback(self.hass, 0, _async_patch_rgbw_off)
                        # in order to avoid multiple state toggles we'll try to flush the
                        # state as late as possible in the callback
                        return
            self.is_on = onoff
            self.flush_state()

    async def async_request_light_ack(self, payload: dict):
        return await self.manager.async_request_ack(
            mc.NS_APPLIANCE_CONTROL_LIGHT,
            mc.METHOD_SET,
            {mc.KEY_LIGHT: payload},
        )

    def _flush_light(self, _light: dict):
        try:
            capacity = _light[mc.KEY_CAPACITY]
            if capacity & mc.LIGHT_CAPACITY_EFFECT:
                self._flush_light_effect(_light)
                return
            else:
                self.effect = None
                if capacity & mc.LIGHT_CAPACITY_RGB:
                    self.color_mode = ColorMode.RGB
                elif capacity & mc.LIGHT_CAPACITY_TEMPERATURE:
                    self.color_mode = ColorMode.COLOR_TEMP
                elif capacity & mc.LIGHT_CAPACITY_LUMINANCE:
                    self.color_mode = ColorMode.BRIGHTNESS
                else:
                    self.color_mode = ColorMode.UNKNOWN
        except Exception as exception:
            self.log_exception(
                self.WARNING, exception, "parsing light 'capacity' key", timeout=86400
            )
            self.color_mode = ColorMode.UNKNOWN
        super()._flush_light(_light)

    def _flush_light_effect(self, _light: dict):
        self.color_mode = ColorMode.ONOFF
        try:
            self.effect = self.effect_list[_light[mc.KEY_EFFECT]]  # type: ignore
        except Exception:
            # due to transient conditions this might happen now and then..
            self.effect = None
        super()._flush_light(_light)

    async def _async_ensure_on(self):
        """
        Ensure the light is really on after sending a light/effect payload.
        This is needed when the toggle is controlled by Appliance.Control.ToggleX
        """
        pass  # moved to MLLightToggleXMixin

    async def _async_patch_rgbw(self, rgb: int):
        _light = dict(self._light)
        _light[mc.KEY_RGB] = rgb
        if await self.async_request_light_ack(_light):
            # directly setting the _light without
            # updating self.rgb_color will allow
            # us to restore the white channel on turn_on
            # (see self.update_on_off)
            self._light[mc.KEY_RGB] = rgb


class MLLightOnOffMixin(MLLight if typing.TYPE_CHECKING else object):

    async def async_request_onoff(self, onoff: int):
        if await self.manager.async_request_ack(
            mc.NS_APPLIANCE_CONTROL_LIGHT,
            mc.METHOD_SET,
            {
                mc.KEY_LIGHT: {
                    mc.KEY_CHANNEL: self.channel,
                    mc.KEY_ONOFF: onoff,
                }
            },
        ):
            self._light[mc.KEY_ONOFF] = onoff
            self.update_onoff(onoff)


class MLLightToggleXMixin(MLLight if typing.TYPE_CHECKING else object):

    _togglex_mode: bool | None
    """
    if False: the device doesn't use TOGGLEX
    elif True: the device needs TOGGLEX to turn ON
    elif None: the component needs to auto-learn the device behavior
    """

    # HA core entity attributes:
    _unrecorded_attributes = frozenset({ATTR_TOGGLEX_MODE})

    def __init__(
        self,
        manager: "MerossDevice",
        digest: dict,
    ):
        self._togglex_mode = None
        self.extra_state_attributes = {ATTR_TOGGLEX_MODE: None}
        super().__init__(manager, digest)
        manager.register_parser(mc.NS_APPLIANCE_CONTROL_TOGGLEX, self)

    async def async_request_onoff(self, onoff: int):
        if await self.manager.async_request_ack(
            mc.NS_APPLIANCE_CONTROL_TOGGLEX,
            mc.METHOD_SET,
            {
                mc.KEY_TOGGLEX: {
                    mc.KEY_CHANNEL: self.channel,
                    mc.KEY_ONOFF: onoff,
                }
            },
        ):
            self.update_onoff(onoff)

    async def _async_ensure_on(self):
        """
        Ensure the light is really on after sending a light/effect payload.
        This is needed when the toggle is controlled by Appliance.Control.ToggleX
        """
        # In general, the LIGHT payload with LUMINANCE set should rightly
        # turn on the light, but this is not true for every model/fw.
        # Since devices exposing TOGGLEX have different behaviors we'll
        # try to learn this at runtime.
        if self._togglex_mode is None:
            # we need to learn the device behavior...
            # wait a bit since this query would report off
            # if the device has not had the time to internally update
            await asyncio.sleep(1)
            if self.is_on:
                # in case MQTT pushed the togglex -> on
                self._togglex_mode = False
                self.extra_state_attributes = {ATTR_TOGGLEX_MODE: False}
                return
            elif await self.manager.async_request_ack(
                mc.NS_APPLIANCE_CONTROL_TOGGLEX,
                mc.METHOD_GET,
                {mc.KEY_TOGGLEX: [{mc.KEY_CHANNEL: self.channel}]},
            ):
                # various kind of lights here might respond with either an array or a
                # simple dict since the "togglex" namespace used to be hybrid and still is.
                # This led to #357 but the resolution is to just bypass parsing since
                # our device message pipe has already processed the response with
                # all its (working) euristics after returning from async_request_ack
                self._togglex_mode = not self.is_on
                self.extra_state_attributes = {ATTR_TOGGLEX_MODE: self._togglex_mode}
            else:
                # no way
                return
        if self._togglex_mode:
            # previous test showed that we need TOGGLEX
            await self.async_request_onoff(1)


class MLLightEffect(MLLight):
    """
    Specialized light entity for devices supporting Appliance.Control.Light.Effect
    like msl320
    """

    # HA core entity attributes:
    effect_list: list[str]

    __slots__ = ("_light_effect_list",)

    def __init__(self, manager: "MerossDevice", digest: dict):
        self._light_effect_list: list[dict] = []
        super().__init__(manager, digest, [])
        SmartPollingStrategy(
            manager,
            mc.NS_APPLIANCE_CONTROL_LIGHT_EFFECT,
            handler=self._handle_Appliance_Control_Light_Effect,
        )

    async def async_turn_on(self, **kwargs):
        # intercept light command if it is related to effects (on/off/change of luminance)
        if ATTR_EFFECT in kwargs:
            effect_index = self.effect_list.index(kwargs[ATTR_EFFECT])  # type: ignore
            if effect_index == len(self._light_effect_list):  # EFFECT_OFF
                _light = dict(self._light)
                _light.pop(mc.KEY_EFFECT, None)
                _light[mc.KEY_CAPACITY] &= ~mc.LIGHT_CAPACITY_EFFECT
                if not await self.async_request_light_ack(_light):
                    return  # TODO: report an HomeAssistantError maybe
            else:
                _light_effect = self._light_effect_list[effect_index]
                _light_effect[mc.KEY_ENABLE] = 1
                if not await self.manager.async_request_ack(
                    mc.NS_APPLIANCE_CONTROL_LIGHT_EFFECT,
                    mc.METHOD_SET,
                    {mc.KEY_EFFECT: [_light_effect]},
                ):
                    _light_effect[mc.KEY_ENABLE] = 0
                    return  # TODO: report an HomeAssistantError maybe
                _light = self._light
                _light[mc.KEY_EFFECT] = effect_index
                _light[mc.KEY_CAPACITY] |= mc.LIGHT_CAPACITY_EFFECT
            self._flush_light(_light)
            if not self.is_on:
                await self._async_ensure_on()
            return

        if ATTR_BRIGHTNESS in kwargs:
            _light = self._light
            if _light[mc.KEY_CAPACITY] & mc.LIGHT_CAPACITY_EFFECT:
                # we're trying to control the luminance of the effect though...
                _light_effect = None
                try:
                    effect_index = _light[mc.KEY_EFFECT]
                    _light_effect = self._light_effect_list[effect_index]
                    member = _light_effect[mc.KEY_MEMBER]
                    brightness = kwargs[ATTR_BRIGHTNESS]
                    luminance = brightness_to_native(brightness)
                    for m in member:
                        m[mc.KEY_LUMINANCE] = luminance
                    if await self.manager.async_request_ack(
                        mc.NS_APPLIANCE_CONTROL_LIGHT_EFFECT,
                        mc.METHOD_SET,
                        {mc.KEY_EFFECT: [_light_effect]},
                    ):
                        self.brightness = brightness
                        self.flush_state()
                        if not self.is_on:
                            await self._async_ensure_on()
                        return
                    else:
                        # the _light_effect is now dirty..it'll get reset at
                        # the next effect list query
                        return  # TODO: report an HomeAssistantError maybe
                except Exception as exception:
                    self.log_exception(
                        self.WARNING,
                        exception,
                        "setting effect parameters (light:%s light_effect:%s)",
                        str(_light),
                        str(_light_effect),
                    )
                    return  # TODO: report an HomeAssistantError maybe

        # nothing related to effects in this service call so
        # we'll proceed to 'standard' light commands
        await super().async_turn_on(**kwargs)

    def _flush_light_effect(self, _light: dict):
        effect_index = _light[mc.KEY_EFFECT]
        try:
            _light_effect = self._light_effect_list[effect_index]
        except IndexError:
            # our _light_effect_list might be stale
            self.manager.polling_strategies[
                mc.NS_APPLIANCE_CONTROL_LIGHT_EFFECT
            ].lastrequest = 0
            return

        self.effect = _light_effect[mc.KEY_EFFECTNAME]
        try:
            member = _light_effect[mc.KEY_MEMBER]
            # extract only the first item luminance since
            # it looks they're all the same in the app default effects list
            self.brightness = native_to_brightness(member[0][mc.KEY_LUMINANCE])
            self.color_mode = ColorMode.BRIGHTNESS
        except Exception:
            self.brightness = None
            self.color_mode = ColorMode.ONOFF
        self._light = _light
        self.flush_state()
        return

    def _handle_Appliance_Control_Light_Effect(self, header: dict, payload: dict):
        """
        {
            "effect": [
                {
                    "Id": "0000000000000000",
                    "effectName": "Night",
                    "iconName": "light_effect_icon_night",
                    "enable": 0,
                    "mode": 0,
                    "speed": 10,
                    "member": [{"temperature": 1, "luminance": 30}, {"rgb": 1, "luminance": 30}],
                },
            ]
        }
        """
        _light_effect_list = payload[mc.KEY_EFFECT]
        if self._light_effect_list != _light_effect_list:
            self._light_effect_list = _light_effect_list
            self.effect_list = effect_list = [
                _light_effect[mc.KEY_EFFECTNAME] for _light_effect in _light_effect_list
            ]
            effect_list.append(MLLightBase.EFFECT_OFF)
            # add a 'fake' key so the next update will force-flush
            self._light["_"] = None
            self.manager.request(request_get(mc.NS_APPLIANCE_CONTROL_LIGHT))


class MLLightMp3(MLLight):
    """
    Specialized light entity for devices supporting Appliance.Control.Mp3
    Actually this should be an HP110.
    """

    def __init__(self, manager: "MerossDevice", payload: dict):
        super().__init__(manager, payload, mc.HP110A_LIGHT_EFFECT_LIST)


class MLDNDLightEntity(me.MerossToggle, light.LightEntity):
    """
    light entity representing the device DND feature usually implemented
    through a light feature (presence light or so)
    """

    PLATFORM = light.DOMAIN
    manager: "MerossDevice"

    namespace = mc.NS_APPLIANCE_SYSTEM_DNDMODE
    key_namespace = mc.KEY_DNDMODE
    key_value = mc.KEY_MODE

    # HA core entity attributes:
    color_mode: ColorMode = ColorMode.ONOFF
    entity_category = me.EntityCategory.CONFIG
    supported_color_modes: set[ColorMode] = {ColorMode.ONOFF}

    def __init__(self, manager: "MerossDevice"):
        super().__init__(manager, None, mlc.DND_ID, mc.KEY_DNDMODE)
        EntityPollingStrategy(
            manager,
            self.namespace,
            self,
            handler=self._handle_Appliance_System_DNDMode,
        )

    async def async_turn_on(self, **kwargs):
        if await self.manager.async_request_ack(
            mc.NS_APPLIANCE_SYSTEM_DNDMODE,
            mc.METHOD_SET,
            {mc.KEY_DNDMODE: {mc.KEY_MODE: 0}},
        ):
            self.update_onoff(1)

    async def async_turn_off(self, **kwargs):
        if await self.manager.async_request_ack(
            mc.NS_APPLIANCE_SYSTEM_DNDMODE,
            mc.METHOD_SET,
            {mc.KEY_DNDMODE: {mc.KEY_MODE: 1}},
        ):
            self.update_onoff(0)

    def _handle_Appliance_System_DNDMode(self, header: dict, payload: dict):
        self.update_onoff(not payload[mc.KEY_DNDMODE][mc.KEY_MODE])


_LIGHT_ENTITY_CLASSES: dict[str, type] = {}


def digest_init_light(device: "MerossDevice", digest: dict) -> "DigestParseFunc":
    """{ "channel": 0, "capacity": 4 }"""

    descriptor = device.descriptor
    ability = descriptor.ability

    if get_element_by_key_safe(
        descriptor.digest.get(mc.KEY_TOGGLEX),
        mc.KEY_CHANNEL,
        digest.get(mc.KEY_CHANNEL),
    ):
        toggle_mixin = MLLightToggleXMixin
    elif mc.KEY_ONOFF in digest:
        toggle_mixin = MLLightOnOffMixin
    else:
        raise Exception(
            "Missing both 'ToggleX' namespace and 'onoff' key. Unknown light entity model"
        )

    if mc.NS_APPLIANCE_CONTROL_LIGHT_EFFECT in ability:
        light_mixin = MLLightEffect
    elif mc.NS_APPLIANCE_CONTROL_MP3 in ability:
        light_mixin = MLLightMp3
    else:
        light_mixin = MLLight

    # build a label to cache the set
    class_name = toggle_mixin.__name__ + light_mixin.__name__
    try:
        class_type = _LIGHT_ENTITY_CLASSES[class_name]
    except KeyError:
        class_type = type(class_name, (toggle_mixin, light_mixin), {})
        _LIGHT_ENTITY_CLASSES[class_name] = class_type

    class_type(device, digest)

    return device.namespace_handlers[mc.NS_APPLIANCE_CONTROL_LIGHT].parse_generic
