from __future__ import annotations

from ..merossclient import const as mc  # mEROSS cONST

from ..light import MLLight
from ..select import MLSpray
#from ..sensor import MLSensor, DEVICE_CLASS_TEMPERATURE, DEVICE_CLASS_HUMIDITY

class DiffuserMixin:


    def _init_diffuser(self, payload):
        """
        "diffuser":
        {
            "type": "mod100",
            "light": [{"channel": 0, "onoff": 0, "lmTime": 1639082117, "mode": 0, "luminance": 100, "rgb": 4129023}],
            "spray": [{"channel": 0, "mode": 2, "lmTime": 1644353195}]
        }
        """
        light = payload.get(mc.KEY_LIGHT)
        if isinstance(light, list):
            for l in light:
                MLLight(self, l, mc.KEY_LIGHT, mc.NS_APPLIANCE_CONTROL_DIFFUSER_LIGHT)
        spray = payload.get(mc.KEY_SPRAY)
        if isinstance(spray, list):
            for s in spray:
                MLSpray(self, s.get(mc.KEY_CHANNEL, 0), mc.KEY_SPRAY, mc.NS_APPLIANCE_CONTROL_DIFFUSER_SPRAY)
        """
        it looks by the trace we have so far temp and hum are reporting fake (0) values
        if payload.get(mc.KEY_TYPE) == mc.TYPE_MOD100:
            self._sensor_temperature = MerossLanSensor(self, DEVICE_CLASS_TEMPERATURE, DEVICE_CLASS_TEMPERATURE)
            self._sensor_humidity = MerossLanSensor(self, DEVICE_CLASS_HUMIDITY, DEVICE_CLASS_HUMIDITY)
        """

    def _handle_Appliance_Control_Diffuser_Spray(self,
    namespace: str, method: str, payload: dict, header: dict):
        """
        {
            "type": "mod100",
            "spray": [{"channel": 0, "mode": 2, "lmTime": 1644353195}]
        }
        """
        self._parse_diffuser_spray(payload.get(mc.KEY_SPRAY))


    def _handle_Appliance_Control_Diffuser_Light(self,
    namespace: str, method: str, payload: dict, header: dict):
        self._parse_diffuser_light(payload.get(mc.KEY_LIGHT))


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


    def _parse_diffuser_spray(self, payload: dict):
        self._parse__generic(mc.KEY_SPRAY, payload, mc.KEY_SPRAY)


    def _parse_diffuser_light(self, payload: dict):
        self._parse__generic(mc.KEY_LIGHT, payload, mc.KEY_LIGHT)


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
