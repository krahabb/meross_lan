from __future__ import annotations

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

from . import const as mlc, meross_entity as me
from .helpers import reverse_lookup
from .helpers.namespaces import EntityPollingStrategy, SmartPollingStrategy
from .merossclient import const as mc, get_element_by_key_safe

if typing.TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

    from .meross_device import MerossDevice
    from .merossclient import MerossDeviceDescriptor

ATTR_TOGGLEX_MODE = "togglex_mode"
#    map light Temperature effective range to HA mired(s):
#    right now we'll use a const approach since it looks like
#    any light bulb out there carries the same specs
#    MIRED <> 1000000/TEMPERATURE[K]
#    (thanks to @nao-pon #87)
MSLANY_MIRED_MIN = 153  # math.floor(1/(6500/1000000))
MSLANY_MIRED_MAX = 371  # math.ceil(1/(2700/1000000))


async def async_setup_entry(
    hass: HomeAssistant, config_entry: ConfigEntry, async_add_devices
):
    me.platform_setup_entry(hass, config_entry, async_add_devices, light.DOMAIN)


def _rgb_to_int(rgb) -> int:
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


def _int_to_rgb(rgb: int):
    return (rgb & 16711680) >> 16, (rgb & 65280) >> 8, (rgb & 255)


def _sat_1_100(value):
    if value > 100:
        return 100
    elif value < 1:
        return 1
    else:
        return int(value)


class MLLightBase(me.MerossToggle, light.LightEntity):
    """
    base 'abstract' class for meross light entities
    """

    PLATFORM = light.DOMAIN
    manager: MerossDevice

    """
    internal copy of the actual meross light state
    """
    _light: dict
    """
    if the device supports effects, we'll map these to effect names
    to interact with HA api. This dict contains the effect key value
    used in the 'light' payload to the effect name
    """
    _light_effect_map = {}

    # HA core entity attributes:
    brightness: int | None
    color_mode: ColorMode
    color_temp: int | None
    effect: str | None = None
    effect_list: list[str] | None = None
    max_mireds: int = MSLANY_MIRED_MAX
    min_mireds: int = MSLANY_MIRED_MIN
    rgb_color: tuple[int, int, int] | None
    supported_color_modes: set[ColorMode]
    supported_features: LightEntityFeature = LightEntityFeature(0)
    # TODO: add implementation for temp_kelvin and min-max_kelvin

    __slots__ = (
        "_light",
        "brightness",
        "color_mode",
        "color_temp",
        "rgb_color",
    )

    def __init__(self, manager: MerossDevice, payload: dict):
        self._light = {}
        self.brightness = None
        self.color_mode = ColorMode.UNKNOWN
        self.color_temp = None
        self.rgb_color = None
        super().__init__(manager, payload.get(mc.KEY_CHANNEL, 0))

    def set_unavailable(self):
        self._light = {}
        super().set_unavailable()

    def update_onoff(self, onoff):
        if mc.KEY_ONOFF in self._light:
            self._light[mc.KEY_ONOFF] = onoff
        super().update_onoff(onoff)

    def _inherited_parse_light(self, payload: dict):
        """
        allow inherited implementations to refine light payload parsing
        """
        pass

    def _parse_light(self, payload: dict):
        if self._light != payload:
            self._light = payload

            if mc.KEY_ONOFF in payload:
                self.is_on = payload[mc.KEY_ONOFF]

            self.color_mode = ColorMode.UNKNOWN

            if mc.KEY_LUMINANCE in payload:
                self.color_mode = ColorMode.BRIGHTNESS
                self.brightness = payload[mc.KEY_LUMINANCE] * 255 // 100
            else:
                self.brightness = None

            if mc.KEY_TEMPERATURE in payload:
                self.color_mode = ColorMode.COLOR_TEMP
                self.color_temp = ((100 - payload[mc.KEY_TEMPERATURE]) / 99) * (
                    self.max_mireds - self.min_mireds
                ) + self.min_mireds
            else:
                self.color_temp = None

            if mc.KEY_RGB in payload:
                self.color_mode = ColorMode.RGB
                self.rgb_color = _int_to_rgb(payload[mc.KEY_RGB])
            else:
                self.rgb_color = None

            self._inherited_parse_light(payload)
            self.flush_state()


class MLLight(MLLightBase):
    """
    light entity for Meross bulbs and any device supporting light api
    (identified from devices carrying 'light' node in SYSTEM_ALL payload)
    """

    manager: LightMixin

    _unrecorded_attributes = frozenset({ATTR_TOGGLEX_MODE})

    _capacity: int
    _togglex_switch: bool
    """
    if True the device supports/needs TOGGLEX namespace to toggle
    """
    _togglex_mode: bool | None
    """
    if False: the device doesn't use TOGGLEX
    elif True: the device needs TOGGLEX to turn ON
    elif None: the component needs to auto-learn the device behavior
    """

    __slots__ = (
        "_capacity",
        "_togglex_switch",
        "_togglex_mode",
        "supported_color_modes",
    )

    def __init__(self, manager: LightMixin, payload: dict):
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
        descr = manager.descriptor
        if get_element_by_key_safe(
            descr.digest.get(mc.KEY_TOGGLEX),
            mc.KEY_CHANNEL,
            payload.get(mc.KEY_CHANNEL, 0),
        ):
            self._togglex_switch = True
            self._togglex_mode = None
            self.extra_state_attributes = {ATTR_TOGGLEX_MODE: None}
            self.namespace = mc.NS_APPLIANCE_CONTROL_TOGGLEX
            self.key_namespace = mc.KEY_TOGGLEX
        else:
            self._togglex_switch = False
            self._togglex_mode = False

        """
        capacity is set in abilities when using mc.NS_APPLIANCE_CONTROL_LIGHT
        """
        self._capacity = descr.ability[mc.NS_APPLIANCE_CONTROL_LIGHT].get(
            mc.KEY_CAPACITY, mc.LIGHT_CAPACITY_LUMINANCE
        )

        self.supported_color_modes = supported_color_modes = set()
        if self._capacity & mc.LIGHT_CAPACITY_RGB:
            supported_color_modes.add(ColorMode.RGB)  # type: ignore
        if self._capacity & mc.LIGHT_CAPACITY_TEMPERATURE:
            supported_color_modes.add(ColorMode.COLOR_TEMP)  # type: ignore
        if not supported_color_modes:
            if self._capacity & mc.LIGHT_CAPACITY_LUMINANCE:
                supported_color_modes.add(ColorMode.BRIGHTNESS)  # type: ignore
            else:
                supported_color_modes.add(ColorMode.ONOFF)  # type: ignore

        super().__init__(manager, payload)
        manager.register_parser(mc.NS_APPLIANCE_CONTROL_LIGHT, self)

    async def async_turn_on(self, **kwargs):
        if not kwargs:
            await self.async_request_onoff(1)
            return

        light = dict(self._light)
        capacity = light.get(mc.KEY_CAPACITY, 0) | mc.LIGHT_CAPACITY_LUMINANCE
        # Brightness must always be set in payload
        if ATTR_BRIGHTNESS in kwargs:
            light[mc.KEY_LUMINANCE] = _sat_1_100(kwargs[ATTR_BRIGHTNESS] * 100 // 255)
        elif not light.get(mc.KEY_LUMINANCE, 0):
            light[mc.KEY_LUMINANCE] = 100

        # Color is taken from either of these 2 values, but not both.
        if ATTR_RGB_COLOR in kwargs:
            light[mc.KEY_RGB] = _rgb_to_int(kwargs[ATTR_RGB_COLOR])
            light.pop(mc.KEY_TEMPERATURE, None)
            capacity |= mc.LIGHT_CAPACITY_RGB
            capacity &= ~mc.LIGHT_CAPACITY_TEMPERATURE
        elif ATTR_COLOR_TEMP in kwargs:
            # map mireds: min_mireds -> 100 - max_mireds -> 1
            norm_value = (kwargs[ATTR_COLOR_TEMP] - self.min_mireds) / (
                self.max_mireds - self.min_mireds
            )
            light[mc.KEY_TEMPERATURE] = _sat_1_100(100 - (norm_value * 99))
            light.pop(mc.KEY_RGB, None)
            capacity |= mc.LIGHT_CAPACITY_TEMPERATURE
            capacity &= ~mc.LIGHT_CAPACITY_RGB

        if ATTR_EFFECT in kwargs:
            effect = reverse_lookup(self._light_effect_map, kwargs[ATTR_EFFECT])
            if effect:
                if isinstance(effect, str) and effect.isdigit():
                    effect = int(effect)
                light[mc.KEY_EFFECT] = effect
                capacity |= mc.LIGHT_CAPACITY_EFFECT
            else:
                light.pop(mc.KEY_EFFECT, None)
                capacity &= ~mc.LIGHT_CAPACITY_EFFECT
        else:
            light.pop(mc.KEY_EFFECT, None)
            capacity &= ~mc.LIGHT_CAPACITY_EFFECT

        light[mc.KEY_CAPACITY] = capacity

        if not self._togglex_switch:
            light[mc.KEY_ONOFF] = 1

        if await self.manager.async_request_light_ack(light):
            self._light = {}  # invalidate so _parse_light will force-flush
            self._parse_light(light)
            if not self.is_on:
                # In general, the LIGHT payload with LUMINANCE set should rightly
                # turn on the light, but this is not true for every model/fw.
                # Since devices exposing TOGGLEX have different behaviors we'll
                # try to learn this at runtime.
                if self._togglex_mode is None:
                    # we need to learn the device behavior...
                    if await self.manager.async_request_ack(
                        mc.NS_APPLIANCE_CONTROL_TOGGLEX,
                        mc.METHOD_GET,
                        {mc.KEY_TOGGLEX: [{mc.KEY_CHANNEL: self.channel}]},
                    ):
                        # various kind of lights here might respond with either an array or a
                        # simple dict since the "togglex" namespace used to be hybrid and still is.
                        # This led to #357 but the resolution is to just bypass parsing since
                        # the device message pipe has already processed the response with
                        # all its (working) euristics after returning from async_request_ack
                        self._togglex_mode = not self.is_on
                        self.extra_state_attributes = {
                            ATTR_TOGGLEX_MODE: self._togglex_mode
                        }
                if self._togglex_mode:
                    # previous test showed that we need TOGGLEX
                    await self.async_request_onoff(1)

        # 87: @nao-pon bulbs need a 'double' send when setting Temp
        if ATTR_COLOR_TEMP in kwargs:
            if self.manager.descriptor.firmwareVersion == "2.1.2":
                await self.manager.async_request_light_ack(light)

    async def async_request_onoff(self, onoff: int):
        if self._togglex_switch:
            await super().async_request_onoff(onoff)
        else:
            if await self.manager.async_request_light_ack(
                {mc.KEY_CHANNEL: self.channel, mc.KEY_ONOFF: onoff}
            ):
                self.update_onoff(onoff)

    def update_effect_map(self, light_effect_map: dict):
        """
        the list of available effects was changed (context at device level)
        so we'll just tell HA to update the state
        """
        self._light_effect_map = light_effect_map
        if light_effect_map:
            self.supported_features |= LightEntityFeature.EFFECT
            self.effect_list = list(light_effect_map.values())
        else:
            self.supported_features &= ~LightEntityFeature.EFFECT
            self.effect_list = None
        self.flush_state()

    def _inherited_parse_light(self, payload: dict):
        if mc.KEY_CAPACITY in payload:
            # despite of previous parsing, use capacity
            # value to effectively set this light color mode
            # this key is not present for instance in mod100 lights
            capacity = payload[mc.KEY_CAPACITY]
            if capacity & mc.LIGHT_CAPACITY_RGB:
                self.color_mode = ColorMode.RGB
            elif capacity & mc.LIGHT_CAPACITY_TEMPERATURE:
                self.color_mode = ColorMode.COLOR_TEMP
            elif capacity & mc.LIGHT_CAPACITY_LUMINANCE:
                self.color_mode = ColorMode.BRIGHTNESS

        self.effect = None
        if mc.KEY_EFFECT in payload:
            # here effect might be an int while our map keys might be 'str formatted'
            # so we'll use a flexible (robust? dumb?) approach here in mapping
            effect = payload[mc.KEY_EFFECT]
            if effect in self._light_effect_map:
                self.effect = self._light_effect_map.get(effect)
            elif isinstance(effect, int):
                for key, value in self._light_effect_map.items():
                    if isinstance(key, str) and key.isdigit():
                        if int(key) == effect:
                            self.effect = value
                            break
                else:
                    # we didnt find the effect even with effectId int casting
                    # so we hope it's positional....
                    effects = self._light_effect_map.values()
                    if effect < len(effects):
                        self.effect = effects[effect]  # type: ignore


class MLDNDLightEntity(me.MerossToggle, light.LightEntity):
    """
    light entity representing the device DND feature usually implemented
    through a light feature (presence light or so)
    """

    manager: MerossDevice

    PLATFORM = light.DOMAIN

    color_mode: ColorMode = ColorMode.ONOFF
    entity_category = me.EntityCategory.CONFIG
    supported_color_modes: set[ColorMode] = {ColorMode.ONOFF}

    def __init__(self, manager: MerossDevice):
        super().__init__(manager, None, mlc.DND_ID, mc.KEY_DNDMODE)
        EntityPollingStrategy(
            manager,
            mc.NS_APPLIANCE_SYSTEM_DNDMODE,
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


class LightMixin(
    MerossDevice if typing.TYPE_CHECKING else object
):  # pylint: disable=used-before-assignment
    """
    add to MerossDevice when creating actual device in setup
    in order to provide NS_APPLIANCE_CONTROL_LIGHT and
    NS_APPLIANCE_CONTROL_LIGHT_EFFECT capability
    """

    light_effect_map: dict[object, str] = {}  # map effect.Id to effect.Name

    def __init__(self, descriptor: MerossDeviceDescriptor, entry):
        super().__init__(descriptor, entry)

        if mc.NS_APPLIANCE_CONTROL_LIGHT_EFFECT in descriptor.ability:
            SmartPollingStrategy(self, mc.NS_APPLIANCE_CONTROL_LIGHT_EFFECT)

    def _init_light(self, digest: dict):
        MLLight(self, digest)

    def _parse_light(self, digest):
        self.namespace_handlers[mc.NS_APPLIANCE_CONTROL_LIGHT]._parse_generic(digest)

    def _handle_Appliance_Control_Light_Effect(self, header: dict, payload: dict):
        light_effect_map = {}
        for p_effect in payload.get(mc.KEY_EFFECT, []):
            light_effect_map[p_effect[mc.KEY_ID_]] = p_effect[mc.KEY_EFFECTNAME]
        if light_effect_map != self.light_effect_map:
            self.light_effect_map = light_effect_map
            for entity in self.entities.values():
                if isinstance(entity, MLLight):
                    entity.update_effect_map(light_effect_map)

    async def async_request_light_ack(self, payload):
        return await self.async_request_ack(
            mc.NS_APPLIANCE_CONTROL_LIGHT,
            mc.METHOD_SET,
            {mc.KEY_LIGHT: payload},
        )
