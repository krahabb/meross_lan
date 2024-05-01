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

    from .meross_device import DigestParseFunc, MerossDevice

ATTR_TOGGLEX_MODE = "togglex_mode"
#    map light Temperature effective range to HA mired(s):
#    right now we'll use a const approach since it looks like
#    any light bulb out there carries the same specs
#    MIRED <> 1000000/TEMPERATURE[K]
#    (thanks to @nao-pon #87)
MSLANY_MIRED_MIN = 153  # math.floor(1/(6500/1000000))
MSLANY_MIRED_MAX = 371  # math.ceil(1/(2700/1000000))


async def async_setup_entry(
    hass: "HomeAssistant", config_entry: "ConfigEntry", async_add_devices
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
    base 'abstract' class for meross light entities handling
    either
    NS_APPLIANCE_CONTROL_LIGHT -> specialized in MLLight
    NS_APPLIANCE_CONTROL_DIFFUSER_LIGHT -> specialized in MLDiffuserLight
    """

    PLATFORM = light.DOMAIN
    manager: "MerossDevice"

    namespace: typing.ClassVar[str]
    key_namespace = mc.KEY_LIGHT

    """
    internal copy of the actual meross light state
    """
    _light: dict
    """
    if the device supports effects, we'll map these to effect names
    to interact with HA api. This dict contains the effect key value
    used in the 'light' payload to the effect name
    """

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
        payload: dict,
        effect_list: list[str] | None = None,
    ):
        self._light = {}
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

        super().__init__(manager, payload.get(mc.KEY_CHANNEL))
        manager.register_parser(self.namespace, self)

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
    identified from devices carrying 'light' node in SYSTEM_ALL payload and/or
    NS_APPLIANCE_CONTROL_LIGHT in abilities
    """

    manager: "MerossDevice"

    namespace = mc.NS_APPLIANCE_CONTROL_LIGHT

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

    def __init__(
        self,
        manager: "MerossDevice",
        payload: dict,
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

        if get_element_by_key_safe(
            descriptor.digest.get(mc.KEY_TOGGLEX),
            mc.KEY_CHANNEL,
            payload.get(mc.KEY_CHANNEL),
        ):
            self._togglex_switch = True
            self._togglex_mode = None
            self.extra_state_attributes = {ATTR_TOGGLEX_MODE: None}
        else:
            self._togglex_switch = False
            self._togglex_mode = False

        """
        capacity is set in abilities when using mc.NS_APPLIANCE_CONTROL_LIGHT
        """
        self._capacity = capacity = ability[mc.NS_APPLIANCE_CONTROL_LIGHT].get(
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

        super().__init__(manager, payload, effect_list)
        if self._togglex_switch:
            manager.register_parser(mc.NS_APPLIANCE_CONTROL_TOGGLEX, self)

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
            light[mc.KEY_EFFECT] = self.effect_list.index(kwargs[ATTR_EFFECT])  # type: ignore
            capacity |= mc.LIGHT_CAPACITY_EFFECT
        else:
            light.pop(mc.KEY_EFFECT, None)
            capacity &= ~mc.LIGHT_CAPACITY_EFFECT

        light[mc.KEY_CAPACITY] = capacity

        if not self._togglex_switch:
            light[mc.KEY_ONOFF] = 1

        if await self.async_request_light_ack(light):
            self._light = {}  # invalidate so _parse_light will force-flush
            self._parse_light(light)
            await self._async_ensure_on()

        # 87: @nao-pon bulbs need a 'double' send when setting Temp
        if ATTR_COLOR_TEMP in kwargs:
            if self.manager.descriptor.firmwareVersion == "2.1.2":
                await self.async_request_light_ack(light)

    async def async_request_light_ack(self, payload: dict):
        return await self.manager.async_request_ack(
            mc.NS_APPLIANCE_CONTROL_LIGHT,
            mc.METHOD_SET,
            {mc.KEY_LIGHT: payload},
        )

    async def async_request_onoff(self, onoff: int):
        if self._togglex_switch:
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
                {mc.KEY_CHANNEL: self.channel, mc.KEY_ONOFF: onoff}
            ):
                self.update_onoff(onoff)

    async def _async_ensure_on(self):
        """
        Ensure the light is really on after sending a light/effect payload
        """
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

            if capacity & mc.LIGHT_CAPACITY_EFFECT:
                try:
                    self.effect = self.effect_list[payload[mc.KEY_EFFECT]]  # type: ignore
                except Exception:
                    # due to transient conditions this might happen now and then..
                    self.effect = None
            else:
                self.effect = None


class MLLightEffect(MLLight):
    """
    Specialized light entity for devices supporting Appliance.Control.Light.Effect.
    """

    __slots__ = (
        "_light_effect",
        "_light_effect_list",
    )

    def __init__(self, manager: "MerossDevice", payload: dict):
        self._light_effect: dict | None = None
        self._light_effect_list: list[dict] = []
        super().__init__(manager, payload, [])

        SmartPollingStrategy(
            manager,
            mc.NS_APPLIANCE_CONTROL_LIGHT_EFFECT,
            handler=self._handle_Appliance_Control_Light_Effect,
        )

    """
    async def async_turn_on(self, **kwargs):
        if ATTR_EFFECT in kwargs:
            effect_index = self.effect_list.index(kwargs[ATTR_EFFECT])  # type: ignore
            if effect_index == 0: # light.EFFECT_OFF
                effect_index = self._light.get(mc.KEY_EFFECT)
                if self.
                pass
            else:
                effect_index -= 1
                light_effect = self._light_effect_list[effect_index]
                light_effect[mc.KEY_ENABLE] = 1
                if await self.manager.async_request_ack(
                    mc.NS_APPLIANCE_CONTROL_LIGHT_EFFECT,
                    mc.METHOD_SET,
                    {mc.KEY_EFFECT: [light_effect]},
                ):
                    self._light_effect = light_effect
                    self.effect = light_effect[mc.KEY_EFFECTNAME]
                    self._light[mc.KEY_EFFECT] = effect_index
                    self._light[mc.KEY_CAPACITY] = mc.LIGHT_CAPACITY_EFFECT
                    self.flush_state()
                    await self._async_ensure_on()
                else:
                    light_effect[mc.KEY_ENABLE] = 0
            return

        await super().async_turn_on(**kwargs)

    def _parse_light(self, payload: dict):
        if self._light != payload:
            self._light = payload

            capacity = payload[mc.KEY_CAPACITY]
            if capacity & mc.LIGHT_CAPACITY_RGB:
                self.color_mode = ColorMode.RGB
            elif capacity & mc.LIGHT_CAPACITY_TEMPERATURE:
                self.color_mode = ColorMode.COLOR_TEMP
            elif capacity & mc.LIGHT_CAPACITY_LUMINANCE:
                self.color_mode = ColorMode.BRIGHTNESS

            if capacity & mc.LIGHT_CAPACITY_EFFECT:
                effect_index = payload[mc.KEY_EFFECT]
                try:
                    light_effect = self._light_effect_list[effect_index]
                except IndexError:
                    # our _light_effect_list is stale
                    light_effect = None
                    self.manager.polling_strategies[mc.NS_APPLIANCE_CONTROL_LIGHT_EFFECT].lastrequest = 0
            else:
                light_effect = None

            if self._light_effect != light_effect:
                if self._light_effect:
                    self._light_effect[mc.KEY_ENABLE] = 0
                if light_effect:
                    light_effect[mc.KEY_ENABLE] = 1
                    self.effect = light_effect[mc.KEY_EFFECTNAME]
                    self.color_mode = ColorMode.BRIGHTNESS
                else:
                    self.effect = None
                self._light_effect = light_effect




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
    """

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
                    "member": [{"temperature": 1, "luminance": 30}, {{"rgb": 1, "luminance": 30}],
                },
            ]
        }
        """

        light_effect_list = payload[mc.KEY_EFFECT]
        if self._light_effect_list != light_effect_list:
            self._light_effect_list = light_effect_list
            self._light_effect = effect = None
            self.effect_list = effect_list = [light.EFFECT_OFF]
            effect_index = 0
            for light_effect in light_effect_list:
                if light_effect[mc.KEY_ENABLE]:
                    self._light_effect = light_effect
                    effect = light_effect[mc.KEY_EFFECTNAME]
                    effect_list.append(effect)
                    self._light[mc.KEY_EFFECT] = effect_index
                else:
                    effect_list.append(light_effect[mc.KEY_EFFECTNAME])
                effect_index += 1
            """
            self.effect = effect
            capacity = self._light[mc.KEY_CAPACITY]
            if effect:
                self._light[mc.KEY_CAPACITY] = capacity | mc.LIGHT_CAPACITY_EFFECT
            else:
                self._light[mc.KEY_CAPACITY] = capacity & ~mc.LIGHT_CAPACITY_EFFECT
                self._light.pop(mc.KEY_EFFECT, None)
            """
            self.flush_state()


class MLLightMp3(MLLight):
    """
    Specialized light entity for devices supporting Appliance.Control.Mp3
    Actually this is should be an HP110.
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


def digest_init_light(device: "MerossDevice", digest: dict) -> "DigestParseFunc":
    """{ "channel": 0, "capacity": 4 }"""

    ability = device.descriptor.ability
    if mc.NS_APPLIANCE_CONTROL_LIGHT_EFFECT in ability:
        MLLightEffect(device, digest)
    elif mc.NS_APPLIANCE_CONTROL_MP3 in ability:
        MLLightMp3(device, digest)
    else:
        MLLight(device, digest)

    return device.namespace_handlers[mc.NS_APPLIANCE_CONTROL_LIGHT].parse_generic
