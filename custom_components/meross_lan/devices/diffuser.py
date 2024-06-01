import typing

from ..helpers.namespaces import NamespaceHandler
from ..light import (
    ATTR_BRIGHTNESS,
    ATTR_EFFECT,
    ATTR_RGB_COLOR,
    ATTR_TRANSITION,
    MSL_LUMINANCE_MAX,
    ColorMode,
    MLLightBase,
    brightness_to_native,
    native_to_brightness,
    native_to_rgb,
    rgb_to_native,
)
from ..merossclient import const as mc
from ..sensor import MLHumiditySensor, MLTemperatureSensor
from .spray import MLSpray

if typing.TYPE_CHECKING:
    from ..meross_device import DigestInitReturnType, MerossDevice


DIFFUSER_SENSOR_CLASS_MAP: dict[
    str, type[MLHumiditySensor] | type[MLTemperatureSensor]
] = {
    mc.KEY_HUMIDITY: MLHumiditySensor,
    mc.KEY_TEMPERATURE: MLTemperatureSensor,
}


def digest_init_diffuser(
    device: "MerossDevice", digest: dict
) -> "DigestInitReturnType":
    """
    {
        "type": "mod100",
        "light": [{"channel": 0, "onoff": 0, "lmTime": 1639082117, "mode": 0, "luminance": 100, "rgb": 4129023}],
        "spray": [{"channel": 0, "mode": 2, "lmTime": 1644353195}]
    }
    """

    diffuser_light_handler = NamespaceHandler(
        device, mc.NS_APPLIANCE_CONTROL_DIFFUSER_LIGHT, entity_class=MLDiffuserLight
    )
    for light_digest in digest.get(mc.KEY_LIGHT, []):
        MLDiffuserLight(device, light_digest)

    diffuser_spray_handler = NamespaceHandler(
        device, mc.NS_APPLIANCE_CONTROL_DIFFUSER_SPRAY, entity_class=MLDiffuserSpray
    )
    for spray_digest in digest.get(mc.KEY_SPRAY, []):
        MLDiffuserSpray(device, spray_digest[mc.KEY_CHANNEL])

    if mc.NS_APPLIANCE_CONTROL_DIFFUSER_SENSOR in device.descriptor.ability:
        # former mod100 devices reported fake values for sensors, maybe the mod150 and/or a new firmware
        # are supporting correct values so we implement them (#243)
        def _handle_Appliance_Control_Diffuser_Sensor(header: dict, payload: dict):
            """
            {
                "type": "mod100",
                "humidity": {"value": 0, "lmTime": 0},
                "temperature": {"value": 0, "lmTime": 0}
            }
            """
            entities = device.entities
            for key in (mc.KEY_HUMIDITY, mc.KEY_TEMPERATURE):
                if key in payload:
                    try:
                        entities[key].update_native_value(
                            payload[key][mc.KEY_VALUE] / 10
                        )
                    except KeyError:
                        DIFFUSER_SENSOR_CLASS_MAP[key](
                            device, None, device_value=payload[key][mc.KEY_VALUE] / 10
                        )

        NamespaceHandler(
            device,
            mc.NS_APPLIANCE_CONTROL_DIFFUSER_SENSOR,
            handler=_handle_Appliance_Control_Diffuser_Sensor,
        )

    diffuser_light_parser = diffuser_light_handler.parse_list
    diffuser_spray_parser = diffuser_spray_handler.parse_list

    def digest_parse(digest: dict):
        """
        {
            "type": "mod100",
            "light": [{"channel": 0, "onoff": 0, "lmTime": 1639082117, "mode": 0, "luminance": 100, "rgb": 4129023}],
            "spray": [{"channel": 0, "mode": 2, "lmTime": 1644353195}]
        }
        """
        diffuser_light_parser(digest.get(mc.KEY_LIGHT, []))
        diffuser_spray_parser(digest.get(mc.KEY_SPRAY, []))

    return digest_parse, (diffuser_light_handler, diffuser_spray_handler)


class MLDiffuserLight(MLLightBase):
    """
    light entity for Meross diffuser (MOD100)
    """

    namespace = mc.NS_APPLIANCE_CONTROL_DIFFUSER_LIGHT

    def __init__(self, manager: "MerossDevice", digest: dict):

        self.supported_color_modes = {ColorMode.RGB}

        super().__init__(manager, digest, mc.DIFFUSER_LIGHT_MODE_LIST)

    # interface: MLLightBase
    async def async_request_light_ack(self, _light: dict):
        return await self.manager.async_request_ack(
            mc.NS_APPLIANCE_CONTROL_DIFFUSER_LIGHT,
            mc.METHOD_SET,
            {mc.KEY_LIGHT: [_light]},
        )

    def _flush_light(self, _light: dict):
        # taken from https://github.com/bwp91/homebridge-meross/blob/latest/lib/device/diffuser.js
        try:
            self.effect = None
            self.is_on = _light[mc.KEY_ONOFF]
            self.brightness = native_to_brightness(_light[mc.KEY_LUMINANCE])
            self.rgb_color = native_to_rgb(_light[mc.KEY_RGB])
            mode = _light[mc.KEY_MODE]
            if mode == mc.DIFFUSER_LIGHT_MODE_COLOR:
                self.color_mode = ColorMode.RGB
            else:
                self.color_mode = ColorMode.BRIGHTNESS
                self.effect = self.effect_list[mode]  # type: ignore
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

    # interface: LightEntity
    async def async_turn_on(self, **kwargs):
        if self._t_unsub:
            self._transition_cancel()

        _light = dict(self._light)
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

        if await self.async_request_light_ack(_light):
            self._flush_light(_light)
            if _t_duration:
                self._transition_schedule(_t_duration)

    async def async_turn_off(self, **kwargs):
        if await self.async_request_light_ack(
            {mc.KEY_CHANNEL: self.channel, mc.KEY_ONOFF: 0}
        ):
            self._light[mc.KEY_ONOFF] = 0
            self.update_onoff(0)


class MLDiffuserSpray(MLSpray):

    SPRAY_MODE_MAP = {
        mc.DIFFUSER_SPRAY_MODE_OFF: MLSpray.OPTION_SPRAY_MODE_OFF,
        mc.DIFFUSER_SPRAY_MODE_ECO: MLSpray.OPTION_SPRAY_MODE_ECO,
        mc.DIFFUSER_SPRAY_MODE_FULL: MLSpray.OPTION_SPRAY_MODE_CONTINUOUS,
    }

    namespace = mc.NS_APPLIANCE_CONTROL_DIFFUSER_SPRAY

    # interface: MLSpray
    async def async_request_spray_ack(self, payload: dict):
        return await self.manager.async_request_ack(
            self.namespace,
            mc.METHOD_SET,
            {self.key_namespace: [payload]},
        )
