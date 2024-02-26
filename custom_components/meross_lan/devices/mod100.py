from __future__ import annotations

import typing

from ..helpers import reverse_lookup
from ..helpers.namespaces import PollingStrategy
from ..light import (
    ATTR_BRIGHTNESS,
    ATTR_EFFECT,
    ATTR_RGB_COLOR,
    ColorMode,
    LightEntityFeature,
    MLLightBase,
    _rgb_to_int,
    _sat_1_100,
)
from ..merossclient import const as mc  # mEROSS cONST
from ..select import (
    OPTION_SPRAY_MODE_CONTINUOUS,
    OPTION_SPRAY_MODE_ECO,
    OPTION_SPRAY_MODE_OFF,
    MLSpray,
)
from ..sensor import MLHumiditySensor, MLTemperatureSensor

if typing.TYPE_CHECKING:
    from ..meross_device import MerossDevice


class MLDiffuserLight(MLLightBase):
    """
    light entity for Meross diffuser (MOD100)
    """

    manager: DiffuserMixin

    _light_effect_map = mc.DIFFUSER_LIGHT_EFFECT_MAP
    # HA core entity attributes:
    effect_list = list(_light_effect_map.values())
    supported_color_modes = {ColorMode.RGB}
    supported_features: LightEntityFeature = LightEntityFeature.EFFECT

    async def async_turn_on(self, **kwargs):
        light = dict(self._light)
        light[mc.KEY_CHANNEL] = self.channel
        light[mc.KEY_ONOFF] = 1

        if ATTR_RGB_COLOR in kwargs:
            light[mc.KEY_RGB] = _rgb_to_int(kwargs[ATTR_RGB_COLOR])

        # Brightness must always be set in payload
        if ATTR_BRIGHTNESS in kwargs:
            light[mc.KEY_LUMINANCE] = _sat_1_100(kwargs[ATTR_BRIGHTNESS] * 100 // 255)
        elif not light.get(mc.KEY_LUMINANCE, 0):
            light[mc.KEY_LUMINANCE] = 100

        if ATTR_EFFECT in kwargs:
            mode = reverse_lookup(self._light_effect_map, kwargs[ATTR_EFFECT])
            if mode is not None:
                light[mc.KEY_MODE] = mode
            else:
                if mc.KEY_MODE not in light:
                    light[mc.KEY_MODE] = mc.DIFFUSER_LIGHT_MODE_COLOR
        else:
            if mc.KEY_MODE not in light:
                light[mc.KEY_MODE] = mc.DIFFUSER_LIGHT_MODE_COLOR

        if await self.manager.async_request_light_ack(light):
            self._parse_light(light)

    async def async_turn_off(self, **kwargs):
        if await self.manager.async_request_light_ack(
            {mc.KEY_CHANNEL: self.channel, mc.KEY_ONOFF: 0}
        ):
            self.update_onoff(0)

    def _inherited_parse_light(self, payload: dict):
        if mc.KEY_MODE in payload:
            # taken from https://github.com/bwp91/homebridge-meross/blob/latest/lib/device/diffuser.js
            mode = payload[mc.KEY_MODE]
            self.effect = self._light_effect_map.get(mode)
            if self.effect is None:
                # we're missing the effect for this mode so the device firmware
                # is newer than our knowledge. Lets make a copy of our _light_effect_map
                # which is by design a class instance
                self.effect = "mode_" + str(mode)
                self._light_effect_map = dict(self._light_effect_map)
                self._light_effect_map[mode] = self.effect
                self.effect_list = list(self._light_effect_map.values())


class DiffuserMixin(
    MerossDevice if typing.TYPE_CHECKING else object
):  # pylint: disable=used-before-assignment
    SPRAY_MODE_MAP = {
        mc.DIFFUSER_SPRAY_MODE_OFF: OPTION_SPRAY_MODE_OFF,
        mc.DIFFUSER_SPRAY_MODE_ECO: OPTION_SPRAY_MODE_ECO,
        mc.DIFFUSER_SPRAY_MODE_FULL: OPTION_SPRAY_MODE_CONTINUOUS,
    }

    # interface: MerossDevice
    def _init_diffuser(self, digest: dict):
        """
        "diffuser":
        {
            "type": "mod100",
            "light": [{"channel": 0, "onoff": 0, "lmTime": 1639082117, "mode": 0, "luminance": 100, "rgb": 4129023}],
            "spray": [{"channel": 0, "mode": 2, "lmTime": 1644353195}]
        }
        """
        self._type = digest.get(mc.KEY_TYPE, "")
        for light_digest in digest.get(mc.KEY_LIGHT, []):
            light = MLDiffuserLight(self, light_digest)
            self.register_parser(mc.NS_APPLIANCE_CONTROL_DIFFUSER_LIGHT, light)
        for spray_digest in digest.get(mc.KEY_SPRAY, []):
            spray = MLSpray(
                self, spray_digest[mc.KEY_CHANNEL], DiffuserMixin.SPRAY_MODE_MAP
            )
            self.register_parser(mc.NS_APPLIANCE_CONTROL_DIFFUSER_SPRAY, spray)

        if mc.NS_APPLIANCE_CONTROL_DIFFUSER_SENSOR in self.descriptor.ability:
            # former mod100 devices reported fake values for sensors, maybe the mod150 and/or a new firmware
            # are supporting correct values so we implement them (#243)
            MLHumiditySensor(self, None)
            MLTemperatureSensor(self, None)
            PollingStrategy(self, mc.NS_APPLIANCE_CONTROL_DIFFUSER_SENSOR, item_count=1)

    def _parse_diffuser(self, digest: dict):
        """
        "diffuser":
        {
            "type": "mod100",
            "light": [{"channel": 0, "onoff": 0, "lmTime": 1639082117, "mode": 0, "luminance": 100, "rgb": 4129023}],
            "spray": [{"channel": 0, "mode": 2, "lmTime": 1644353195}]
        }
        """
        self.namespace_handlers[mc.NS_APPLIANCE_CONTROL_DIFFUSER_LIGHT]._parse_list(
            digest.get(mc.KEY_LIGHT, [])
        )
        self.namespace_handlers[mc.NS_APPLIANCE_CONTROL_DIFFUSER_SPRAY]._parse_list(
            digest.get(mc.KEY_SPRAY, [])
        )

    # interface: self
    def _handle_Appliance_Control_Diffuser_Sensor(self, header: dict, payload: dict):
        """
        {
            "type": "mod100",
            "humidity": {"value": 0, "lmTime": 0},
            "temperature": {"value": 0, "lmTime": 0}
        }
        """
        entities = self.entities
        for key in (mc.KEY_HUMIDITY, mc.KEY_TEMPERATURE):
            if key in payload:
                entities[key].update_native_value(payload[key][mc.KEY_VALUE] / 10)

    async def async_request_light_ack(self, payload):
        return await self.async_request_ack(
            mc.NS_APPLIANCE_CONTROL_DIFFUSER_LIGHT,
            mc.METHOD_SET,
            {mc.KEY_TYPE: self._type, mc.KEY_LIGHT: [payload]},
        )

    async def async_request_spray_ack(self, payload):
        return await self.async_request_ack(
            mc.NS_APPLIANCE_CONTROL_DIFFUSER_SPRAY,
            mc.METHOD_SET,
            {mc.KEY_TYPE: self._type, mc.KEY_SPRAY: [payload]},
        )
