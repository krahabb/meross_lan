import asyncio
from time import monotonic
from typing import TYPE_CHECKING, override

from homeassistant.components import light
from homeassistant.components.light import (
    ATTR_BRIGHTNESS,
    ATTR_COLOR_TEMP_KELVIN,
    ATTR_EFFECT,
    ATTR_RGB_COLOR,
    ATTR_TRANSITION,
    ColorMode,
    LightEntityFeature,
)
import homeassistant.util.color as color_util

from . import const as mlc
from .helpers import clamp, entity as mle
from .merossclient.device.handler import NamespaceHandler
from .merossclient.protocol import const as mc, namespaces as mn
from .merossclient.protocol.message import MerossMessage

if TYPE_CHECKING:
    from typing import ClassVar, Final, NotRequired, Unpack

    from .helpers.device import Device
    from .merossclient.protocol import types as mt


MSL_LUMINANCE_MIN = 1
MSL_LUMINANCE_MAX = 100
MSL_LUMINANCE_SCALE = (MSL_LUMINANCE_MAX - MSL_LUMINANCE_MIN) / 254
BRIGHTNESS_SCALE = (MSL_LUMINANCE_MIN, MSL_LUMINANCE_MAX)


def brightness_to_native(brightness: int):
    return round(color_util.brightness_to_value(BRIGHTNESS_SCALE, brightness))
    return MSL_LUMINANCE_MIN + round((brightness - 1) * MSL_LUMINANCE_SCALE)


def native_to_brightness(luminance: int):
    return color_util.value_to_brightness(BRIGHTNESS_SCALE, luminance)
    return 1 + round((luminance - MSL_LUMINANCE_MIN) / MSL_LUMINANCE_SCALE)


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


def rgbw_patch_to_native(rgb: tuple[int, int, int]) -> int:
    """
    Convert an HA RGB tuple to a device native value (int).
    When converting, the White channel is zeroed since the msl320cp
    is not behaving correctly when the rgb has white in it.
    We're not preserving color saturation so that rgb colors
    close to white (i.e. with high LUMA value) will be sent
    with lower r,g,b values so to dim those damn leds
    """
    r, g, b, w = color_util.color_rgb_to_rgbw(*rgb)
    return (r << 16) + (g << 8) + b


def native_to_rgbw_patch(rgb: int) -> tuple[int, int, int]:
    """
    When converting from device to HA rgb color space we've lost the white channel
    and the HA UI keeps loosing luminance when feeded back with those 'pretty dark' colors.
    In order to keep up the proposed HA UI rgb luminance we'll offset the white channel
    of the amount missing in order to saturate at least 1 of the colors.
    """
    r = (rgb & 16711680) >> 16
    g = (rgb & 65280) >> 8
    b = rgb & 255
    w = 255 - max((r, g, b))
    return color_util.color_rgbw_to_rgb(r + w, g + w, b + w, w)


#    map light Temperature effective range to HA kelvin(s):
#    right now we'll use a const approach since it looks like
#    any light bulb out there carries the same specs
#    (thanks to @nao-pon #87)
MSL_KELVIN_MIN = 2700
MSL_KELVIN_MAX = 6500
MSL_TEMPERATURE_MIN = 1
MSL_TEMPERATURE_MAX = 100
MSL_TEMPERATURE_SCALE = (MSL_TEMPERATURE_MAX - MSL_TEMPERATURE_MIN) / (
    MSL_KELVIN_MAX - MSL_KELVIN_MIN
)


def kelvin_to_native(kelvin: int):
    return clamp(
        round(MSL_TEMPERATURE_MIN + (kelvin - MSL_KELVIN_MIN) * MSL_TEMPERATURE_SCALE),
        MSL_TEMPERATURE_MIN,
        MSL_TEMPERATURE_MAX,
    )


def native_to_kelvin(temperature: int):
    return round(
        MSL_KELVIN_MIN + (temperature - MSL_TEMPERATURE_MIN) / MSL_TEMPERATURE_SCALE
    )


class LightBase(mle.ToggleXParser, light.LightEntity):
    """
    base 'abstract' class for meross light entities handling
    either
    NS_APPLIANCE_CONTROL_LIGHT -> specialized in Light
    NS_APPLIANCE_CONTROL_DIFFUSER_LIGHT -> specialized in DiffuserLight
    """

    if TYPE_CHECKING:
        ns_value: mt.control.Light | mt.diffuser.Light

        T_RESOLUTION_MIN: Final[float]

        _t_begin: float
        _t_end: float
        _t_duration: float
        _t_resolution: float
        _t_luminance_begin: int
        _t_luminance_end: int
        _t_luminance_r: float
        _t_temp_begin: int
        _t_temp_end: int | None
        _t_temp_r: float
        _t_rgb_begin: tuple[int, int, int]
        _t_rgb_end: tuple[int, int, int] | None
        _t_rgb_r: tuple[float, float, float]

        # HA core entity attributes:
        brightness: int | None
        rgb_color: tuple[int, int, int] | None
        color_temp_kelvin: int | None
        color_mode: ColorMode
        supported_color_modes: set[ColorMode]
        effect: str | None
        effect_list: list[str] | None
        supported_features: LightEntityFeature

        parent: Final[Device]  # type: ignore[override]
        init_effect_list: ClassVar[list[str] | None]

        class Args(mle.ToggleXParser.Args):
            effect_list: NotRequired[list[str]]
            pass

    PLATFORM = light.DOMAIN

    T_RESOLUTION_MIN = 0.2
    init_effect_list = None

    # HA core entity attributes:
    _attr_max_color_temp_kelvin = MSL_KELVIN_MAX
    _attr_min_color_temp_kelvin = MSL_KELVIN_MIN

    __slots__ = (
        "_rgb_to_native",
        "_native_to_rgb",
        "_t_begin",
        "_t_end",
        "_t_duration",
        "_t_resolution",
        "_t_luminance_begin",
        "_t_luminance_end",
        "_t_luminance_r",
        "_t_temp_begin",
        "_t_temp_end",
        "_t_temp_r",
        "_t_rgb_begin",
        "_t_rgb_end",
        "_t_rgb_r",
        "brightness",
        "rgb_color",
        "color_temp_kelvin",
        "color_mode",
        "effect",
        "effect_list",
        "supported_features",
    )

    def __init__(self, id, device: "Device", /, **kwargs: "Unpack[Args]"):
        self._rgb_to_native = rgb_to_native
        self._native_to_rgb = native_to_rgb
        self.brightness = None
        self.rgb_color = None
        self.color_mode = ColorMode.UNKNOWN
        self.color_temp_kelvin = None
        self.effect = None
        self.effect_list = kwargs.pop("effect_list", self.init_effect_list)
        self.supported_features = (
            (LightEntityFeature.EFFECT | LightEntityFeature.TRANSITION)
            if self.effect_list
            else LightEntityFeature.TRANSITION
        )
        mle.ToggleXParser.__init__(self, id, device, **kwargs)

    @override
    def set_unavailable(self):
        self.cancel_callback(self._transition_callback)
        self.brightness = None
        self.color_mode = ColorMode.UNKNOWN
        self.color_temp_kelvin = None
        self.effect = None
        self.rgb_color = None
        mle.ToggleXParser.set_unavailable(self)

    # interface: self
    def _transition_setup(self, _light: "mt.JsonDict", kwargs: dict, /) -> float | None:
        self._t_duration = _t_duration = kwargs[ATTR_TRANSITION]
        self._t_begin = monotonic()
        self._t_end = self._t_begin + _t_duration

        if self.is_on:
            self._t_luminance_begin = _light[mc.KEY_LUMINANCE]
        else:
            self._t_luminance_begin = MSL_LUMINANCE_MIN
        if ATTR_BRIGHTNESS in kwargs:
            self._t_luminance_end = brightness_to_native(kwargs[ATTR_BRIGHTNESS])
        else:
            self._t_luminance_end = _light[mc.KEY_LUMINANCE]
        self._t_luminance_r = (
            self._t_luminance_end - self._t_luminance_begin
        ) / _t_duration
        _light[mc.KEY_LUMINANCE] = self._t_luminance_begin
        _t_ratio_max = abs(self._t_luminance_r)

        if ATTR_RGB_COLOR in kwargs:
            self._t_rgb_end = _t_rgb_end = kwargs[ATTR_RGB_COLOR]
            self._t_rgb_begin = _t_rgb_begin = self.rgb_color or (1, 1, 1)
            self._t_rgb_r = (
                (_t_rgb_end[0] - _t_rgb_begin[0]) / _t_duration,
                (_t_rgb_end[1] - _t_rgb_begin[1]) / _t_duration,
                (_t_rgb_end[2] - _t_rgb_begin[2]) / _t_duration,
            )
            _light[mc.KEY_RGB] = self._rgb_to_native(_t_rgb_begin)
            _t_ratio_max = max(_t_ratio_max, max((abs(c) for c in self._t_rgb_r)))
        else:
            self._t_rgb_end = None

        if ATTR_COLOR_TEMP_KELVIN in kwargs:
            self._t_temp_end = kelvin_to_native(kwargs[ATTR_COLOR_TEMP_KELVIN])
            self._t_temp_begin = _light.get(
                mc.KEY_TEMPERATURE, (MSL_TEMPERATURE_MAX + MSL_TEMPERATURE_MIN) // 2
            )
            self._t_temp_r = (self._t_temp_end - self._t_temp_begin) / _t_duration
            _light[mc.KEY_TEMPERATURE] = self._t_temp_begin
            _t_ratio_max = max(_t_ratio_max, abs(self._t_temp_r))
        else:
            self._t_temp_end = None

        if _t_ratio_max:
            # we now setup an update period (resolution)
            # for the transition according to its highest dynamic
            self._t_resolution = max(1 / _t_ratio_max, LightBase.T_RESOLUTION_MIN)
            return _t_duration
        else:
            return None  # no meaningful transition

    def _transition_schedule(self, t_duration: float, /):
        """
        Calculates the next scheduled time based off remaining transition duration
        in order to evenly spread the calls. This call also takes care of reducing
        the call frequency in case we're on cloud MQTT
        """
        if self.parent.meross_binded:
            # Saturate the resolution of the callback
            # to avoid excessive MQTT traffic when on cloud MQTT
            # This is applied even if we're using HTTP to send commands
            # since they'll likely create MQTT PUSH messages from the device.
            _t_resolution = max(10, self._t_resolution)
        else:
            _t_resolution = self._t_resolution
        # now 'spread' the resolution over the remaining duration
        _t_resolution = t_duration / (round(t_duration / _t_resolution) or 1)
        self.schedule_callback(_t_resolution, self._transition_callback)

    def _transition_callback(self, /):
        if not self.is_on:
            return
        t_now = monotonic()
        _light = dict(self.ns_value)
        if t_now >= (self._t_end - self._t_resolution):
            _light[mc.KEY_LUMINANCE] = self._t_luminance_end
            if self._t_rgb_end:
                _light[mc.KEY_RGB] = self._rgb_to_native(self._t_rgb_end)
            elif self._t_temp_end:
                _light[mc.KEY_TEMPERATURE] = self._t_temp_end
        else:
            t_time = t_now - self._t_begin
            _light[mc.KEY_LUMINANCE] = round(
                self._t_luminance_begin + self._t_luminance_r * t_time
            )
            if self._t_rgb_end:
                _t_rgb_begin = self._t_rgb_begin
                _t_rgb_r = self._t_rgb_r
                _light[mc.KEY_RGB] = self._rgb_to_native(
                    (
                        round(_t_rgb_begin[0] + _t_rgb_r[0] * t_time),
                        round(_t_rgb_begin[1] + _t_rgb_r[1] * t_time),
                        round(_t_rgb_begin[2] + _t_rgb_r[2] * t_time),
                    )
                )
            elif self._t_temp_end:
                _light[mc.KEY_TEMPERATURE] = round(
                    self._t_temp_begin + self._t_temp_r * t_time
                )
            self._transition_schedule(self._t_end - t_now)

        if _light == self.ns_value:
            # Our time resolution might be too fast to produce
            # visible effects in light payload so we're skipping
            # sending redundant light commands
            return

        self.create_task(
            self.async_request_parse(_light), "._transition_callback", True
        )


class Light(LightBase):
    """
    light entity for Meross bulbs and any device supporting light api
    identified from devices carrying 'light' node in SYSTEM_ALL payload and/or
    NS_APPLIANCE_CONTROL_LIGHT in abilities
    """

    if TYPE_CHECKING:
        ns_value: mt.control.Light

        ATTR_TOGGLEX_AUTO: Final[str]
        _togglex_auto: bool | None
        """
        - False: the device needs to use TOGGLEX
        - True: the device automatically turns on when setting 'Appliance.Control.Light' (very fragile though)
        - None: the component needs to auto-learn the device behavior
        """

        class Args(LightBase.Args):
            pass

    ATTR_TOGGLEX_AUTO = "togglex_auto"

    # HA core entity attributes:
    _unrecorded_attributes = frozenset(
        {
            ATTR_TOGGLEX_AUTO,
            *LightBase._unrecorded_attributes,
        }
    )

    __slots__ = (
        "_togglex_auto",
        "supported_color_modes",
    )

    def __init__(self, id, device: "Device", /, **kwargs: "Unpack[Args]"):
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
        capacity = device.descriptor.ability[mn.Appliance_Control_Light].get(
            mc.KEY_CAPACITY, mc.LIGHT_CAPACITY_LUMINANCE
        )
        self.supported_color_modes = supported_color_modes = set()
        if capacity & mc.LIGHT_CAPACITY_RGB:
            supported_color_modes.add(ColorMode.RGB)
        if capacity & mc.LIGHT_CAPACITY_TEMPERATURE:
            supported_color_modes.add(ColorMode.COLOR_TEMP)
        if not supported_color_modes:
            if capacity & mc.LIGHT_CAPACITY_LUMINANCE:
                supported_color_modes.add(ColorMode.BRIGHTNESS)
            else:
                supported_color_modes.add(ColorMode.ONOFF)
        LightBase.__init__(self, id, device, **kwargs)
        self._togglex_auto = None if self.handler_togglex else False

    @override
    def __call__(self, payload: "mt.control.Light", /):
        if self.ns_value != payload:
            self.ns_value = payload
            if mc.KEY_ONOFF in payload:
                self.is_on = payload[mc.KEY_ONOFF]
            capacity = payload[mc.KEY_CAPACITY]
            if capacity & mc.LIGHT_CAPACITY_EFFECT:
                self._flush_light_effect(payload[mc.KEY_EFFECT])
            else:
                self.effect = None
                if mc.KEY_LUMINANCE in payload:
                    self.brightness = native_to_brightness(payload[mc.KEY_LUMINANCE])
                if capacity & mc.LIGHT_CAPACITY_RGB:
                    self.rgb_color = self._native_to_rgb(payload[mc.KEY_RGB])
                    self.color_mode = ColorMode.RGB
                elif capacity & mc.LIGHT_CAPACITY_TEMPERATURE:
                    self.color_temp_kelvin = native_to_kelvin(
                        payload[mc.KEY_TEMPERATURE]
                    )
                    self.color_mode = ColorMode.COLOR_TEMP
                elif ColorMode.BRIGHTNESS in self.supported_color_modes:
                    self.color_mode = ColorMode.BRIGHTNESS
                # Here we should set ColorMode.UNKNOWN since capacity is inconsistent
                # with HA ColorMode(s). This shouldnt happen though in real life
                # since devices supporting either rgb or color_temp should never
                # report only luminance capacity. For better behavior (also in our testing)
                # we'll leave the color_mode unchanged
                # self.color_mode = ColorMode.UNKNOWN
            self.flush_state()

    def _flush_light_effect(self, effect: int, /):
        self.color_mode = ColorMode.ONOFF
        self.effect = self.effect_list[effect]  # type: ignore

    # interface: LightEntity
    async def async_turn_on(self, **kwargs):
        self.cancel_callback(self._transition_callback)

        if not kwargs:
            await self.async_request_onoff(1)
            return

        _light = dict(self.ns_value)

        if ATTR_TRANSITION in kwargs and kwargs[ATTR_TRANSITION] != 0:
            _t_duration = self._transition_setup(_light, kwargs)
            if self._t_rgb_end:
                _light[mc.KEY_CAPACITY] = mc.LIGHT_CAPACITY_RGB_LUMINANCE
            elif self._t_temp_end:
                _light[mc.KEY_CAPACITY] = mc.LIGHT_CAPACITY_TEMPERATURE_LUMINANCE
            else:
                _light[mc.KEY_CAPACITY] = mc.LIGHT_CAPACITY_LUMINANCE
        else:
            _t_duration = None
            if ATTR_BRIGHTNESS in kwargs:
                _light[mc.KEY_LUMINANCE] = brightness_to_native(kwargs[ATTR_BRIGHTNESS])
            elif not _light.get(mc.KEY_LUMINANCE, 0):
                _light[mc.KEY_LUMINANCE] = MSL_LUMINANCE_MAX
            if ATTR_EFFECT in kwargs:
                _light[mc.KEY_EFFECT] = self.effect_list.index(kwargs[ATTR_EFFECT])  # type: ignore
                _light[mc.KEY_CAPACITY] = mc.LIGHT_CAPACITY_EFFECT
            elif ATTR_RGB_COLOR in kwargs:
                _light[mc.KEY_RGB] = self._rgb_to_native(kwargs[ATTR_RGB_COLOR])
                _light[mc.KEY_CAPACITY] = mc.LIGHT_CAPACITY_RGB_LUMINANCE
            elif ATTR_COLOR_TEMP_KELVIN in kwargs:
                _light[mc.KEY_TEMPERATURE] = kelvin_to_native(
                    kwargs[ATTR_COLOR_TEMP_KELVIN]
                )
                _light[mc.KEY_CAPACITY] = mc.LIGHT_CAPACITY_TEMPERATURE_LUMINANCE
            else:
                _light[mc.KEY_CAPACITY] = mc.LIGHT_CAPACITY_LUMINANCE

        await self.async_request_light_on_flush(_light)
        # 87: @nao-pon bulbs need a 'double' send when setting Temp
        if ATTR_COLOR_TEMP_KELVIN in kwargs:
            if self.parent.descriptor.fw_version == "2.1.2":
                with self.exception_warning("async_turn_on fw 2.1.2 patch"):
                    await self.async_request_parse(_light)
        if _t_duration:
            self._transition_schedule(_t_duration)

    async def async_turn_off(self, **kwargs):
        await self.async_request_onoff(0)

    # interface: self
    async def async_request_onoff(self, onoff: int):
        if self.handler_togglex:
            await self.handler_togglex.async_set_parse({mc.KEY_ONOFF: onoff}, self)
        else:
            await self.async_request_parse_ex({mc.KEY_ONOFF: onoff})

    async def async_request_light_on_flush(self, _light: "mt.JsonDict", /):
        if mc.KEY_ONOFF in _light:
            _light[mc.KEY_ONOFF] = 1
        else:
            self.is_on = self.is_on or self._togglex_auto

        await self.async_request_parse(_light)
        if self.is_on:
            return
        # In general, the LIGHT payload with LUMINANCE set should rightly
        # turn on the light, but this is not true for every model/fw.
        # Since devices exposing TOGGLEX have different behaviors we'll
        # try to learn this at runtime.
        if self._togglex_auto is None:
            # we need to learn the device behavior...
            # wait a bit since this query would report off
            # if the device has not had the time to internally update
            await asyncio.sleep(1)
            if self.is_on or not self.handler_togglex:
                # in case MQTT pushed the togglex -> on
                self._togglex_auto = True
                self.extra_state_attributes = {Light.ATTR_TOGGLEX_AUTO: True}
                return

            try:
                await self.handler_togglex.async_get(self.index)
                # various kind of lights here might respond with either an array or a
                # simple dict since the "togglex" namespace used to be hybrid and still is.
                # This led to #357 but the resolution is to just bypass parsing since
                # our device message pipe has already processed the response with
                # all its (working) euristics after returning from async_request
                self._togglex_auto = self.is_on
                self.extra_state_attributes = {
                    Light.ATTR_TOGGLEX_AUTO: self._togglex_auto
                }
                if self.is_on:
                    return
            except Exception:
                # no way
                return
        # previous test showed that we need TOGGLEX
        await self.async_request_onoff(1)

    @classmethod
    @override
    def namespace_init(cls, ns: mn.Namespace, device: "Device", /):
        handler = NamespaceHandler(ns, device)
        descriptor = device.descriptor
        channel = ns.get_digest(descriptor.digest)[mc.KEY_CHANNEL]
        kwargs = {
            "ns": ns,
            "index": mn.IndexType.channel.get(channel),
        }
        if mn.Appliance_Control_Light_Effect in descriptor.ability:
            light_class = EffectLight
        elif mn.Appliance_Control_Mp3 in descriptor.ability:
            light_class = Light
            kwargs["effect_list"] = mc.HP110A_LIGHT_EFFECT_LIST
        else:
            light_class = Light
        handler.register_parser(light_class(channel, device, **kwargs))


class EffectLight(Light):
    """
    Specialized light entity for devices supporting Appliance.Control.Light.Effect
    like msl320
    """

    if TYPE_CHECKING:
        _light_effects: list[mt.control.Light_Effect]
        # HA core entity attributes:
        init_effect_list: Final[list[str]]
        effect_list: list[str]

    init_effect_list = [light.EFFECT_OFF]

    __slots__ = (
        "_light_effects",
        "handler_light_effect",
    )

    def __init__(self, id, device: "Device", /, **kwargs: "Unpack[Light.Args]"):
        self.handler_light_effect = handler_light_effect = NamespaceHandler(
            mn.Appliance_Control_Light_Effect,
            device,
            handler=self._handle_Appliance_Control_Light_Effect,
            config=mlc.POLLING_CONFIG_CONFIGURATION,
        )
        # This is a 'new' (2025-06-17) key appearing in msl320cpr digest.
        # The key itself is 'light.entity' and carries the effect list
        # (same as Appliance.Control.Light.Effect)
        self._light_effects = handler_light_effect.digest  # type: ignore
        if self._light_effects:
            kwargs["effect_list"] = [
                _light_effect[mc.KEY_EFFECTNAME]
                for _light_effect in self._light_effects
            ] + EffectLight.init_effect_list
            handler_light_effect.parse_digest = self._update_effects  # type: ignore

            def _shutdown():
                del handler_light_effect.parse_digest

            handler_light_effect.shutdown_broadcast.add(_shutdown)
        else:
            self._light_effects = []
        Light.__init__(self, id, device, **kwargs)

        if device.descriptor.type.startswith(mc.TYPE_MSL320_PRO):
            # special rgb channels mgmt here
            self._rgb_to_native = rgbw_patch_to_native
            self._native_to_rgb = native_to_rgbw_patch

    @override
    def flush_state(self):
        self.handler_light_effect.polling_strategy = (
            self.handler_light_effect.__class__.async_poll_smart
            if self.is_on and self.effect
            else None
        )
        return Light.flush_state(self)

    # interface: Light
    @override
    def _flush_light_effect(self, effect: int, /):
        try:
            _light_effect = self._light_effects[effect]
        except IndexError:
            # our _light_effects might be stale
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

    @override
    async def async_turn_on(self, **kwargs):
        self.cancel_callback(self._transition_callback)

        # intercept light command if it is related to effects (on/off/change of luminance)
        if ATTR_EFFECT in kwargs:
            _light = self.ns_value.copy()
            effect_index = self.effect_list.index(kwargs[ATTR_EFFECT])
            if effect_index == len(self._light_effects):  # EFFECT_OFF
                _light.pop(mc.KEY_EFFECT, None)
                _light[mc.KEY_CAPACITY] = (
                    _light[mc.KEY_CAPACITY] & ~mc.LIGHT_CAPACITY_EFFECT
                )
                await self.async_request_light_on_flush(_light)  # type: ignore
            else:
                _light_effect = self._light_effects[effect_index]
                _light_effect[mc.KEY_ENABLE] = 1
                await self.handler_light_effect.async_set(_light_effect)
                _light[mc.KEY_EFFECT] = effect_index
                _light[mc.KEY_CAPACITY] = (
                    _light[mc.KEY_CAPACITY] | mc.LIGHT_CAPACITY_EFFECT
                )
                self(_light)
                if not self.is_on:
                    await self.async_request_onoff(1)
            return

        if ATTR_BRIGHTNESS in kwargs:
            _light = self.ns_value
            if _light[mc.KEY_CAPACITY] & mc.LIGHT_CAPACITY_EFFECT:
                # we're trying to control the luminance of the effect though...
                _light_effect = None
                try:
                    effect_index = _light[mc.KEY_EFFECT]
                    _light_effect = self._light_effects[effect_index]
                    member = _light_effect[mc.KEY_MEMBER]
                    brightness = kwargs[ATTR_BRIGHTNESS]
                    luminance = brightness_to_native(brightness)
                    for m in member:
                        m[mc.KEY_LUMINANCE] = luminance
                    await self.handler_light_effect.async_set(_light_effect)
                    self.brightness = brightness
                    self.flush_state()
                    if not self.is_on:
                        await self.async_request_onoff(1)
                except Exception as exception:
                    self.log_exception(
                        self.WARNING,
                        exception,
                        "setting effect parameters (light:%s light_effect:%s)",
                        str(_light),
                        str(_light_effect),
                    )
                return

        # nothing related to effects in this service call so
        # we'll proceed to 'standard' light commands
        await Light.async_turn_on(self, **kwargs)

    # interface: self
    def _handle_Appliance_Control_Light_Effect(self, message: MerossMessage, /):
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
        # We use an optimistic approach on updating since the effect list is not expected
        # to change a lot. Moreover, any meaningful change to the light will
        # already flush the updated 'effect_list' anyway.
        self._update_effects(message.payload[mc.KEY_EFFECT])

    def _update_effects(self, light_effects: list["mt.control.Light_Effect"], /):
        self._light_effects = light_effects
        self.effect_list = [
            _light_effect[mc.KEY_EFFECTNAME] for _light_effect in light_effects
        ] + EffectLight.init_effect_list


class DNDLight(mle.BinaryParser, mle.EntityNamespaceMixin, light.LightEntity):
    """
    light entity representing the device DND feature usually implemented
    through a light feature (presence light or so)
    """

    POLLING_CONFIG_DEFAULT = mlc.POLLING_CONFIG_CONFIGURATION
    PLATFORM = light.DOMAIN
    init_entity_key = "dnd"
    init_key_value = mle.BinaryParser.KeyValue(mc.KEY_MODE)
    init_value_on = 0
    init_value_off = 1
    # HA core entity attributes:
    _attr_color_mode = ColorMode.ONOFF
    _attr_entity_category = mle.BinaryParser.EntityCategory.CONFIG
    _attr_supported_color_modes = {ColorMode.ONOFF}


async_setup_entry = LightBase.platform_setup_entry
