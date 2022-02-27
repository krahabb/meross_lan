from __future__ import annotations

from ..merossclient import const as mc  # mEROSS cONST
from ..light import (
    MLLightBase,
    COLOR_MODE_RGB, COLOR_MODE_UNKNOWN,
    SUPPORT_BRIGHTNESS, SUPPORT_COLOR, SUPPORT_EFFECT,
    ATTR_RGB_COLOR, ATTR_BRIGHTNESS, ATTR_EFFECT,
    _rgb_to_int,
    _sat_1_100
)
from ..select import (
    MLSpray,
    OPTION_SPRAY_MODE_OFF, OPTION_SPRAY_MODE_CONTINUOUS, OPTION_SPRAY_MODE_ECO,
)
#from ..sensor import MLSensor, DEVICE_CLASS_TEMPERATURE, DEVICE_CLASS_HUMIDITY
from ..helpers import reverse_lookup



class MLDiffuserLight(MLLightBase):
    """
    light entity for Meross diffuser (MOD100)
    """
    _attr_supported_color_modes = {COLOR_MODE_RGB}
    _attr_supported_features = SUPPORT_EFFECT|SUPPORT_BRIGHTNESS|SUPPORT_COLOR


    def __init__(
        self,
        device: 'MerossDevice',
        payload: dict):
        super().__init__(
            device,
            payload.get(mc.KEY_CHANNEL, 0),
            None,
            None,
            None)
        """
        self._light = {
			"onoff": 0,
			"channel": channel,
			"rgb": 16753920,
			"luminance": 100,
			"mode": 0,
		}
        """
        self._light = dict()

        self._light_effect_map = {
            mc.DIFFUSER_LIGHT_MODE_RAINBOW: "Rainbow",
            mc.DIFFUSER_LIGHT_MODE_COLOR: "Color",
            mc.DIFFUSER_LIGHT_MODE_TEMPERATURE: "Temperature",
        }
        self._attr_effect_list = list(self._light_effect_map.values())


    async def async_turn_on(self, **kwargs) -> None:
        light = dict(self._light)
        light[mc.KEY_CHANNEL] = self.channel
        light[mc.KEY_ONOFF] = 1

        if ATTR_RGB_COLOR in kwargs:
            rgb = kwargs[ATTR_RGB_COLOR]
            light[mc.KEY_RGB] = _rgb_to_int(rgb)

        # Brightness must always be set in payload
        if ATTR_BRIGHTNESS in kwargs:
            light[mc.KEY_LUMINANCE] = _sat_1_100(kwargs[ATTR_BRIGHTNESS] * 100 // 255)
        else:
            if mc.KEY_LUMINANCE not in light:
                light[mc.KEY_LUMINANCE] = 100

        if ATTR_EFFECT in kwargs:
            effect = kwargs[ATTR_EFFECT]
            mode = reverse_lookup(self._light_effect_map, effect)
            if mode is not None:
                light[mc.KEY_MODE] = mode
            else:
                if mc.KEY_MODE not in light:
                    light[mc.KEY_MODE] = mc.DIFFUSER_LIGHT_MODE_COLOR
        else:
            if mc.KEY_MODE not in light:
                light[mc.KEY_MODE] = mc.DIFFUSER_LIGHT_MODE_COLOR

        def _ack_callback():
            self._parse_light(light)

        self.device.request_light(light, _ack_callback)


    async def async_turn_off(self, **kwargs) -> None:
        def _ack_callback():
            self.update_onoff(0)

        self.device.request_light(
            { mc.KEY_CHANNEL: self.channel, mc.KEY_ONOFF: 0 },
            _ack_callback)


    def _inherited_parse_light(self, payload: dict):
        if mc.KEY_MODE in payload:
            # taken from https://github.com/bwp91/homebridge-meross/blob/latest/lib/device/diffuser.js
            mode = payload[mc.KEY_MODE]
            self._attr_effect = self._light_effect_map.get(mode)
            if self._attr_effect is None:
                self._attr_effect = "mode_" + str(mode)
                self._light_effect_map[mode] = self._attr_effect
                self._attr_effect_list = list(self._light_effect_map.values())



class DiffuserMixin:

    SPRAY_MODE_MAP = {
        mc.DIFFUSER_SPRAY_MODE_OFF: OPTION_SPRAY_MODE_OFF,
        mc.DIFFUSER_SPRAY_MODE_ECO: OPTION_SPRAY_MODE_ECO,
        mc.DIFFUSER_SPRAY_MODE_FULL: OPTION_SPRAY_MODE_CONTINUOUS,
    }


    def _init_diffuser(self, payload):
        """
        "diffuser":
        {
            "type": "mod100",
            "light": [{"channel": 0, "onoff": 0, "lmTime": 1639082117, "mode": 0, "luminance": 100, "rgb": 4129023}],
            "spray": [{"channel": 0, "mode": 2, "lmTime": 1644353195}]
        }
        """
        self._type = payload.get(mc.KEY_TYPE, '')
        light = payload.get(mc.KEY_LIGHT)
        if isinstance(light, list):
            for l in light:
                MLDiffuserLight(self, l)
        spray = payload.get(mc.KEY_SPRAY)
        if isinstance(spray, list):
            for s in spray:
                MLSpray(
                    self,
                    s.get(mc.KEY_CHANNEL, 0),
                    self.SPRAY_MODE_MAP)
        """
        it looks by the trace we have so far temp and hum are reporting fake (0) values
        if payload.get(mc.KEY_TYPE) == mc.TYPE_MOD100:
            self._sensor_temperature = MerossLanSensor(self, DEVICE_CLASS_TEMPERATURE, DEVICE_CLASS_TEMPERATURE)
            self._sensor_humidity = MerossLanSensor(self, DEVICE_CLASS_HUMIDITY, DEVICE_CLASS_HUMIDITY)
        """

    def _handle_Appliance_Control_Diffuser_Light(self,
    namespace: str, method: str, payload: dict, header: dict):
        self._parse_diffuser_light(payload.get(mc.KEY_LIGHT))


    def _handle_Appliance_Control_Diffuser_Spray(self,
    namespace: str, method: str, payload: dict, header: dict):
        """
        {
            "type": "mod100",
            "spray": [{"channel": 0, "mode": 2, "lmTime": 1644353195}]
        }
        """
        self._parse_diffuser_spray(payload.get(mc.KEY_SPRAY))


    def _handle_Appliance_Control_Diffuser_Sensor(self,
    namespace: str, method: str, payload: dict, header: dict):
        """
        {
            "type": "mod100",
            "humidity": {"value": 0, "lmTime": 0},
            "temperature": {"value": 0, "lmTime": 0}
        }
        """
        return


    def _parse_diffuser_light(self, payload: dict):
        self._parse__generic(mc.KEY_LIGHT, payload)


    def _parse_diffuser_spray(self, payload: dict):
        self._parse__generic(mc.KEY_SPRAY, payload, mc.KEY_SPRAY)


    def _parse_diffuser(self, payload: dict):
        """
        "diffuser":
        {
            "type": "mod100",
            "light": [{"channel": 0, "onoff": 0, "lmTime": 1639082117, "mode": 0, "luminance": 100, "rgb": 4129023}],
            "spray": [{"channel": 0, "mode": 2, "lmTime": 1644353195}]
        }
        """
        for key, value in payload.items():
            _parse = getattr(self, f"_parse_diffuser_{key}", None)
            if _parse is not None:
                _parse(value)


    def request_light(self, payload, callback):
        self.request(
            mc.NS_APPLIANCE_CONTROL_DIFFUSER_LIGHT,
            mc.METHOD_SET,
            {
                mc.KEY_TYPE: self._type,
                mc.KEY_LIGHT: [ payload ]
            },
            callback)


    def request_spray(self, payload, callback):
        self.request(
            mc.NS_APPLIANCE_CONTROL_DIFFUSER_SPRAY,
            mc.METHOD_SET,
            {
                mc.KEY_TYPE: self._type,
                mc.KEY_SPRAY: [ payload ]
            },
            callback)
