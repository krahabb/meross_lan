from abc import abstractmethod
import asyncio
from time import monotonic
import typing

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

from . import const as mlc, meross_entity as me
from .helpers import clamp, schedule_async_callback
from .helpers.namespaces import (
    EntityNamespaceHandler,
    EntityNamespaceMixin,
    NamespaceHandler,
)
from .merossclient import const as mc, namespaces as mn

if typing.TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

    from .meross_device import DigestInitReturnType, MerossDevice

ATTR_TOGGLEX_AUTO = "togglex_auto"


async def async_setup_entry(
    hass: "HomeAssistant", config_entry: "ConfigEntry", async_add_devices
):
    me.platform_setup_entry(hass, config_entry, async_add_devices, light.DOMAIN)


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


class MLLightBase(me.MerossBinaryEntity, light.LightEntity):
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
    EFFECT_OFF: typing.Final = "off"

    # internal copy of the actual meross light state
    _light: dict

    T_RESOLUTION_MIN: typing.Final = 0.2
    _t_unsub: asyncio.TimerHandle | None
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
    color_mode: ColorMode
    color_temp_kelvin: int | None
    effect: str | None
    effect_list: list[str] | None
    max_color_temp_kelvin: int = MSL_KELVIN_MAX
    min_color_temp_kelvin: int = MSL_KELVIN_MIN
    rgb_color: tuple[int, int, int] | None
    supported_color_modes: set[ColorMode]
    supported_features: LightEntityFeature

    __slots__ = (
        "_light",
        "_rgb_to_native",
        "_native_to_rgb",
        "_t_unsub",
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
        "color_mode",
        "color_temp_kelvin",
        "effect",
        "effect_list",
        "rgb_color",
        "supported_color_modes",
        "supported_features",
    )

    def __init__(
        self,
        manager: "MerossDevice",
        digest: dict,
        effect_list: list[str] | None = None,
    ):
        self._light = {}
        self._rgb_to_native = rgb_to_native
        self._native_to_rgb = native_to_rgb
        self._t_unsub = None
        self.brightness = None
        self.color_mode = ColorMode.UNKNOWN
        self.color_temp_kelvin = None
        self.effect = None
        self.rgb_color = None
        if effect_list is None:
            self.effect_list = None
            self.supported_features = LightEntityFeature.TRANSITION
        else:
            self.effect_list = effect_list
            self.supported_features = (
                LightEntityFeature.EFFECT | LightEntityFeature.TRANSITION
            )
        super().__init__(manager, digest.get(mc.KEY_CHANNEL))
        manager.register_parser(self.namespace, self)

    # interface: MerossToggle
    async def async_shutdown(self):
        if self._t_unsub:
            self._transition_cancel()
        await super().async_shutdown()

    def set_unavailable(self):
        if self._t_unsub:
            self._transition_cancel()
        self._light = {}
        self.brightness = None
        self.color_mode = ColorMode.UNKNOWN
        self.color_temp_kelvin = None
        self.effect = None
        self.rgb_color = None
        super().set_unavailable()

    @abstractmethod
    async def async_turn_on(self, **kwargs):
        # this is an error since we're not using me.MerossToggle api
        raise NotImplementedError("'async_turn_on' needs to be overriden")

    @abstractmethod
    async def async_turn_off(self, **kwargs):
        # this is an error since we're not using me.MerossToggle api
        raise NotImplementedError("'async_turn_off' needs to be overriden")

    # interface: self
    async def async_request_light_ack(self, payload: dict):
        return await self.manager.async_request_ack(
            self.namespace,
            mc.METHOD_SET,
            {self.key_namespace: payload},
        )

    def _flush_light(self, _light: dict):
        # pretty virtual
        pass

    def _parse_light(self, payload: dict):
        if self._light != payload:
            self._flush_light(payload)

    def _transition_setup(self, _light: dict, kwargs: dict) -> float | None:
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
            self._t_resolution = max(1 / _t_ratio_max, MLLightBase.T_RESOLUTION_MIN)
            return _t_duration
        else:
            return None  # no meaningful transition

    def _transition_cancel(self):
        # assert self._t_unsub
        self._t_unsub.cancel()  # type: ignore
        self._t_unsub = None

    def _transition_schedule(self, t_duration: float):
        """
        Calculates the next scheduled time based off remaining transition duration
        in order to evenly spread the calls. This call also takes care of reducing
        the call frequency in case we're on cloud MQTT
        """
        if self.manager.meross_binded:
            # 'saturate' the resolution of the callback
            _t_resolution = max(10, self._t_resolution)
        else:
            _t_resolution = self._t_resolution
        # now 'spread' the resolution over the remaining duration
        _t_resolution = t_duration / (round(t_duration / _t_resolution) or 1)
        self._t_unsub = schedule_async_callback(
            self.hass, _t_resolution, self._async_transition
        )

    async def _async_transition(self):
        self._t_unsub = None
        if not self.is_on:
            return
        t_now = monotonic()
        _light = dict(self._light)
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

        if _light == self._light:
            # Our time resolution might be too fast to produce
            # visible effects in light payload so we're skipping
            # sending redundant light commands
            return

        if await self.async_request_light_ack(_light):
            self._flush_light(_light)


class MLLight(MLLightBase):
    """
    light entity for Meross bulbs and any device supporting light api
    identified from devices carrying 'light' node in SYSTEM_ALL payload and/or
    NS_APPLIANCE_CONTROL_LIGHT in abilities
    """

    manager: "MerossDevice"

    namespace = mc.NS_APPLIANCE_CONTROL_LIGHT

    _togglex: bool
    _togglex_auto: bool | None
    """
    - False: the device needs to use TOGGLEX
    - True: the device automatically turns on when setting 'Appliance.Control.Light' (very fragile though)
    - None: the component needs to auto-learn the device behavior
    """

    # HA core entity attributes:
    _unrecorded_attributes = frozenset({ATTR_TOGGLEX_AUTO})

    __slots__ = (
        "_togglex",
        "_togglex_auto",
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

        capacity = ability[mc.NS_APPLIANCE_CONTROL_LIGHT].get(
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

        super().__init__(manager, digest, effect_list)

        self._togglex = manager.register_togglex_channel(self)
        self._togglex_auto = None if self._togglex else False

    # interface: MLLightBase
    def _flush_light(self, _light: dict):
        try:
            if mc.KEY_ONOFF in _light:
                self.is_on = _light[mc.KEY_ONOFF]

            capacity = _light[mc.KEY_CAPACITY]
            if capacity & mc.LIGHT_CAPACITY_EFFECT:
                self._flush_light_effect(_light)
                return

            else:
                self.effect = None

                if mc.KEY_LUMINANCE in _light:
                    self.brightness = native_to_brightness(_light[mc.KEY_LUMINANCE])

                if capacity & mc.LIGHT_CAPACITY_RGB:
                    self.rgb_color = self._native_to_rgb(_light[mc.KEY_RGB])
                    self.color_mode = ColorMode.RGB
                    return

                if capacity & mc.LIGHT_CAPACITY_TEMPERATURE:
                    self.color_temp_kelvin = native_to_kelvin(
                        _light[mc.KEY_TEMPERATURE]
                    )
                    self.color_mode = ColorMode.COLOR_TEMP
                    return

                if capacity & mc.LIGHT_CAPACITY_LUMINANCE:
                    self.color_mode = ColorMode.BRIGHTNESS
                    return

                self.color_mode = ColorMode.UNKNOWN

        except Exception as exception:
            self.log_exception(
                self.WARNING,
                exception,
                "parsing light (%s)",
                str(_light),
                timeout=86400,
            )
        finally:
            self._light = _light
            self.flush_state()

    def _flush_light_effect(self, _light: dict):
        self.color_mode = ColorMode.ONOFF
        self.effect = self.effect_list[_light[mc.KEY_EFFECT]]  # type: ignore

    # interface: LightEntity
    async def async_turn_on(self, **kwargs):
        if self._t_unsub:
            self._transition_cancel()

        if not kwargs:
            await self.async_request_onoff(1)
            return

        _light = dict(self._light)

        if ATTR_TRANSITION in kwargs:
            _t_duration = self._transition_setup(_light, kwargs)
            if self._t_rgb_end:
                _light[mc.KEY_CAPACITY] = mc.LIGHT_CAPACITY_RGB_LUMINANCE
            elif self._t_temp_end:
                _light[mc.KEY_CAPACITY] = mc.LIGHT_CAPACITY_TEMPERATURE_LUMINANCE
            else:
                _light[mc.KEY_CAPACITY] |= mc.LIGHT_CAPACITY_LUMINANCE
        else:
            _t_duration = None
            if ATTR_BRIGHTNESS in kwargs:
                _light[mc.KEY_LUMINANCE] = brightness_to_native(kwargs[ATTR_BRIGHTNESS])
            elif not _light.get(mc.KEY_LUMINANCE, 0):
                _light[mc.KEY_LUMINANCE] = MSL_LUMINANCE_MAX
            if ATTR_EFFECT in kwargs:
                _light[mc.KEY_EFFECT] = self.effect_list.index(kwargs[ATTR_EFFECT])  # type: ignore
                _light[mc.KEY_CAPACITY] |= mc.LIGHT_CAPACITY_EFFECT
            elif ATTR_RGB_COLOR in kwargs:
                _light[mc.KEY_RGB] = self._rgb_to_native(kwargs[ATTR_RGB_COLOR])
                _light[mc.KEY_CAPACITY] = mc.LIGHT_CAPACITY_RGB_LUMINANCE
            elif ATTR_COLOR_TEMP_KELVIN in kwargs:
                _light[mc.KEY_TEMPERATURE] = kelvin_to_native(
                    kwargs[ATTR_COLOR_TEMP_KELVIN]
                )
                _light[mc.KEY_CAPACITY] = mc.LIGHT_CAPACITY_TEMPERATURE_LUMINANCE
            else:
                _light[mc.KEY_CAPACITY] |= mc.LIGHT_CAPACITY_LUMINANCE

        if await self.async_request_light_on_flush(_light):
            if _t_duration:
                self._transition_schedule(_t_duration)

        # 87: @nao-pon bulbs need a 'double' send when setting Temp
        if ATTR_COLOR_TEMP_KELVIN in kwargs:
            if self.manager.descriptor.firmwareVersion == "2.1.2":
                await self.async_request_light_ack(_light)

    async def async_turn_off(self, **kwargs):
        await self.async_request_onoff(0)

    # interface: self
    async def async_request_onoff(self, onoff: int):
        if self._togglex:
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
        else:
            if await self.async_request_light_ack(
                {
                    mc.KEY_CHANNEL: self.channel,
                    mc.KEY_ONOFF: onoff,
                }
            ):
                self._light[mc.KEY_ONOFF] = onoff
                self.update_onoff(onoff)

    async def async_request_light_on_flush(self, _light: dict):
        if mc.KEY_ONOFF in _light:
            _light[mc.KEY_ONOFF] = 1

        if await self.manager.async_request_ack(
            mc.NS_APPLIANCE_CONTROL_LIGHT,
            mc.METHOD_SET,
            {mc.KEY_LIGHT: _light},
        ):
            self.is_on = self.is_on or self._togglex_auto
            self._flush_light(_light)
            if not self.is_on:
                # In general, the LIGHT payload with LUMINANCE set should rightly
                # turn on the light, but this is not true for every model/fw.
                # Since devices exposing TOGGLEX have different behaviors we'll
                # try to learn this at runtime.
                if self._togglex_auto is None:
                    # we need to learn the device behavior...
                    # wait a bit since this query would report off
                    # if the device has not had the time to internally update
                    await asyncio.sleep(1)
                    if self.is_on:
                        # in case MQTT pushed the togglex -> on
                        self._togglex_auto = True
                        self.extra_state_attributes = {ATTR_TOGGLEX_AUTO: True}
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
                        self._togglex_auto = self.is_on
                        self.extra_state_attributes = {
                            ATTR_TOGGLEX_AUTO: self._togglex_auto
                        }
                        if self.is_on:
                            return
                    else:
                        # no way
                        return
                # previous test showed that we need TOGGLEX
                await self.async_request_onoff(1)
            return True

        return False


class MLLightEffect(MLLight):
    """
    Specialized light entity for devices supporting Appliance.Control.Light.Effect
    like msl320
    """

    # HA core entity attributes:
    effect_list: list[str]

    __slots__ = (
        "_light_effect_list",
        "_light_effect_handler",
    )

    def __init__(self, manager: "MerossDevice", digest: dict):
        self._light_effect_list: list[dict] = []
        super().__init__(manager, digest, [])
        self._light_effect_handler = NamespaceHandler(
            manager,
            mc.NS_APPLIANCE_CONTROL_LIGHT_EFFECT,
            handler=self._handle_Appliance_Control_Light_Effect,
        )
        if manager.descriptor.type.startswith(mc.TYPE_MSL320_PRO):
            # special rgb channels mgmt here
            self._rgb_to_native = rgbw_patch_to_native
            self._native_to_rgb = native_to_rgbw_patch

    # interface: MerossBinaryEntity
    def update_onoff(self, onoff):
        if self.is_on != onoff:
            self.is_on = onoff
            if onoff and (mc.KEY_EFFECT in self._light):
                self._light_effect_handler.polling_period = 0
            self.flush_state()

    # interface: MLLight
    def _flush_light_effect(self, _light: dict):
        effect_index = _light[mc.KEY_EFFECT]
        self._light_effect_handler.polling_period = 0
        try:
            _light_effect = self._light_effect_list[effect_index]
        except IndexError:
            # our _light_effect_list might be stale
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

    # interface: LightEntity
    async def async_turn_on(self, **kwargs):
        if self._t_unsub:
            self._transition_cancel()
        # intercept light command if it is related to effects (on/off/change of luminance)
        if ATTR_EFFECT in kwargs:
            effect_index = self.effect_list.index(kwargs[ATTR_EFFECT])  # type: ignore
            if effect_index == len(self._light_effect_list):  # EFFECT_OFF
                _light = dict(self._light)
                _light.pop(mc.KEY_EFFECT, None)
                _light[mc.KEY_CAPACITY] &= ~mc.LIGHT_CAPACITY_EFFECT
                if await self.async_request_light_on_flush(_light):
                    return
            else:
                _light_effect = self._light_effect_list[effect_index]
                _light_effect[mc.KEY_ENABLE] = 1
                if await self.manager.async_request_ack(
                    mc.NS_APPLIANCE_CONTROL_LIGHT_EFFECT,
                    mc.METHOD_SET,
                    {mc.KEY_EFFECT: [_light_effect]},
                ):
                    _light = self._light
                    _light[mc.KEY_EFFECT] = effect_index
                    _light[mc.KEY_CAPACITY] |= mc.LIGHT_CAPACITY_EFFECT
                    self._flush_light(_light)
                    if not self.is_on:
                        await self.async_request_onoff(1)
                    return
                else:
                    _light_effect[mc.KEY_ENABLE] = 0
            return  # TODO: report an HomeAssistantError maybe

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
                            await self.async_request_onoff(1)
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

    # interface: self
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
            self.manager.request(mn.Appliance_Control_Light.request_default)

        if not (self.is_on and (mc.KEY_EFFECT in self._light)):
            self._light_effect_handler.polling_period = mlc.PARAM_INFINITE_TIMEOUT


class MLLightMp3(MLLight):
    """
    Specialized light entity for devices supporting Appliance.Control.Mp3
    Actually this should be an HP110.
    """

    def __init__(self, manager: "MerossDevice", payload: dict):
        super().__init__(manager, payload, mc.HP110A_LIGHT_EFFECT_LIST)


class MLDNDLightEntity(EntityNamespaceMixin, me.MerossBinaryEntity, light.LightEntity):
    """
    light entity representing the device DND feature usually implemented
    through a light feature (presence light or so)
    """

    PLATFORM = light.DOMAIN
    manager: "MerossDevice"

    namespace = mc.NS_APPLIANCE_SYSTEM_DNDMODE

    # HA core entity attributes:
    color_mode: ColorMode = ColorMode.ONOFF
    entity_category = me.EntityCategory.CONFIG
    supported_color_modes: set[ColorMode] = {ColorMode.ONOFF}

    def __init__(self, manager: "MerossDevice"):
        super().__init__(manager, None, mlc.DND_ID, mc.KEY_DNDMODE)
        EntityNamespaceHandler(self)

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

    def _handle(self, header: dict, payload: dict):
        self.update_onoff(not payload[mc.KEY_DNDMODE][mc.KEY_MODE])


def digest_init_light(device: "MerossDevice", digest: dict) -> "DigestInitReturnType":
    """{ "channel": 0, "capacity": 4 }"""

    ability = device.descriptor.ability

    if mc.NS_APPLIANCE_CONTROL_LIGHT_EFFECT in ability:
        MLLightEffect(device, digest)
    elif mc.NS_APPLIANCE_CONTROL_MP3 in ability:
        MLLightMp3(device, digest)
    else:
        MLLight(device, digest)

    handler = device.namespace_handlers[mc.NS_APPLIANCE_CONTROL_LIGHT]
    return handler.parse_generic, (handler,)
