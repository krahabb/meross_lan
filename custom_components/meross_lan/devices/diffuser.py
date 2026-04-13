from typing import TYPE_CHECKING, override

from ..helpers.namespaces import EntityDefNamespaceHandler, mc
from ..light import (
    ATTR_BRIGHTNESS,
    ATTR_EFFECT,
    ATTR_RGB_COLOR,
    ATTR_TRANSITION,
    MSL_LUMINANCE_MAX,
    ColorMode,
    LightBase,
    brightness_to_native,
    native_to_brightness,
    native_to_rgb,
    rgb_to_native,
)
from ..sensor import SensorParser
from .spray import Spray

if TYPE_CHECKING:
    from typing import ClassVar, Final, Mapping, Unpack

    from ..helpers.device import Device, MerossMessage
    from ..merossclient.protocol import types as mt
    from ..merossclient.protocol.types import JsonDict


class DiffuserLight(LightBase):
    """
    light entity for Meross diffuser (MOD100)
    """

    if TYPE_CHECKING:
        ns_payload: mt.diffuser.Light
        effect_list: list[str]

    init_effect_list = mc.DIFFUSER_LIGHT_MODE_LIST
    _attr_supported_color_modes = {ColorMode.RGB}

    @override
    def __call__(self, payload: "mt.diffuser.Light", /):
        # taken from https://github.com/bwp91/homebridge-meross/blob/latest/lib/device/diffuser.js
        if self.ns_payload != payload:
            self.ns_payload = payload
            self.is_on = payload[mc.KEY_ONOFF]
            self.brightness = native_to_brightness(payload[mc.KEY_LUMINANCE])
            self.rgb_color = native_to_rgb(payload[mc.KEY_RGB])
            mode = payload[mc.KEY_MODE]
            if mode == mc.DIFFUSER_LIGHT_MODE_COLOR:
                self.color_mode = ColorMode.RGB
                self.effect = None
            else:
                self.color_mode = ColorMode.BRIGHTNESS
                self.effect = self.effect_list[mode]
            self.flush_state()

    @override
    async def async_turn_on(self, **kwargs):
        self.cancel_callback(self._transition_callback)

        _light = dict(self.ns_payload)
        _light[mc.KEY_ONOFF] = 1
        if ATTR_TRANSITION in kwargs:
            _t_duration = self._transition_setup(_light, kwargs)
            if self._t_rgb_end:
                _light[mc.KEY_MODE] = mc.DIFFUSER_LIGHT_MODE_COLOR
            elif self._t_temp_end:
                _light[mc.KEY_MODE] = mc.DIFFUSER_LIGHT_MODE_TEMPERATURE
        else:
            _t_duration = None
            if ATTR_BRIGHTNESS in kwargs:
                _light[mc.KEY_LUMINANCE] = brightness_to_native(kwargs[ATTR_BRIGHTNESS])
            elif not _light.get(mc.KEY_LUMINANCE, 0):
                _light[mc.KEY_LUMINANCE] = MSL_LUMINANCE_MAX
            if ATTR_EFFECT in kwargs:
                _light[mc.KEY_MODE] = self.effect_list.index(kwargs[ATTR_EFFECT])  # type: ignore
            elif ATTR_RGB_COLOR in kwargs:
                _light[mc.KEY_RGB] = rgb_to_native(kwargs[ATTR_RGB_COLOR])
                _light[mc.KEY_MODE] = mc.DIFFUSER_LIGHT_MODE_COLOR

        await self.async_request_parse(_light)
        if _t_duration:
            self._transition_schedule(_t_duration)

    @override
    async def async_turn_off(self, **kwargs):
        await self.async_request_payload({mc.KEY_ONOFF: 0})
        if self.is_on:
            self.is_on = False
            self.flush_state()


class DiffuserSpray(Spray):

    init_options_map = {
        mc.DIFFUSER_SPRAY_MODE_OFF: Spray.init_options_map[mc.SPRAY_MODE_OFF],
        mc.DIFFUSER_SPRAY_MODE_ECO: Spray.init_options_map[mc.SPRAY_MODE_INTERMITTENT],
        mc.DIFFUSER_SPRAY_MODE_FULL: Spray.init_options_map[mc.SPRAY_MODE_CONTINUOUS],
    }


class DiffuserSensor(EntityDefNamespaceHandler):

    if TYPE_CHECKING:
        # Override entity_defs since these are rather 'entity_args'
        # in order to minimize object creation (all the parsers are just SensorParsers)
        init_entity_defs: ClassVar[Mapping[str, SensorParser.Args]]
        entity_defs: Mapping[str, SensorParser.Args]

    POLLING_CONFIG_DEFAULT = EntityDefNamespaceHandler.POLLING_CONFIG_SLOWSENSOR

    init_entity_defs = {
        mc.KEY_HUMIDITY: SensorParser.HUMIDITY_ARGS,
        mc.KEY_TEMPERATURE: SensorParser.TEMPERATURE_ARGS,
    }

    def _handle(self, message: "MerossMessage", /):
        for key in self.entity_defs:
            try:
                value = message.payload[key][mc.KEY_VALUE]
                try:
                    self.parsers[key].update_device_value(value)
                except KeyError:
                    self.parsers[key] = self.parent.add_entity(
                        SensorParser(
                            None,
                            self.parent,
                            **(self.entity_defs[key] | {"device_value": value}),
                        )
                    )
            except KeyError:
                continue
