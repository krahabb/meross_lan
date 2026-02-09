from typing import TYPE_CHECKING

from ..helpers.namespaces import POLLING_STRATEGY_CONF, NamespaceHandler, mc, mlc, mn
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
from ..sensor import MLHumiditySensor, MLTemperatureSensor
from .spray import MLSpray

if TYPE_CHECKING:
    from typing import Final

    from ..helpers.device import Device, DigestInitReturnType, MerossMessage
    from ..merossclient.protocol import types as mt
    from ..sensor import MLNumericSensor

    DIFFUSER_SENSOR_ENTITY_DEFS: Final

DIFFUSER_SENSOR_ENTITY_DEFS = {
    mc.KEY_HUMIDITY: MLHumiditySensor.ENTITY_DEF(),
    mc.KEY_TEMPERATURE: MLTemperatureSensor.ENTITY_DEF(device_scale=10),
}


def digest_init_diffuser(device: "Device", digest: dict) -> "DigestInitReturnType":
    """
    {
        "type": "mod100",
        "light": [{"channel": 0, "onoff": 0, "lmTime": 1639082117, "mode": 0, "luminance": 100, "rgb": 4129023}],
        "spray": [{"channel": 0, "mode": 2, "lmTime": 1644353195}]
    }
    """

    diffuser_light_handler = NamespaceHandler(
        device, mn.Appliance_Control_Diffuser_Light
    )
    diffuser_light_handler.register_entity_class(
        MLDiffuserLight, (light[mc.KEY_CHANNEL] for light in digest[mc.KEY_LIGHT])
    )

    diffuser_spray_handler = NamespaceHandler(
        device, mn.Appliance_Control_Diffuser_Spray
    )
    diffuser_spray_handler.register_entity_class(
        MLDiffuserSpray, (spray[mc.KEY_CHANNEL] for spray in digest[mc.KEY_SPRAY])
    )

    if mn.Appliance_Control_Diffuser_Sensor in device.descriptor.ability:
        # former mod100 devices reported fake values for sensors, maybe the mod150 and/or a new firmware
        # are supporting correct values so we implement them (#243)
        def _handle_Appliance_Control_Diffuser_Sensor(message: "MerossMessage", /):
            """
            {
                "type": "mod100",
                "humidity": {"value": 0, "lmTime": 0},
                "temperature": {"value": 0, "lmTime": 0}
            }
            """
            entities = device.entities
            for key in DIFFUSER_SENSOR_ENTITY_DEFS:
                try:
                    value = message.payload[key][mc.KEY_VALUE]
                    try:
                        entity = entities[key]
                    except KeyError:
                        entity_def = DIFFUSER_SENSOR_ENTITY_DEFS[key]
                        entity = entity_def.type(
                            None, device, entity_key=key, **entity_def.kwargs
                        )
                    entity.update_device_value(value)
                except KeyError:
                    continue

        NamespaceHandler(
            device,
            mn.Appliance_Control_Diffuser_Sensor,
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
        diffuser_light_parser(digest[mc.KEY_LIGHT])
        diffuser_spray_parser(digest[mc.KEY_SPRAY])

    return digest_parse, (diffuser_light_handler, diffuser_spray_handler)


class MLDiffuserLight(MLLightBase):
    """
    light entity for Meross diffuser (MOD100)
    """

    if TYPE_CHECKING:
        effect_list: list[str]

    ns = mn.Appliance_Control_Diffuser_Light

    def __init__(self, channel: int, manager: "Device", /):
        self.supported_color_modes = {ColorMode.RGB}
        MLLightBase.__init__(self, channel, manager, mc.DIFFUSER_LIGHT_MODE_LIST)

    def _parse_light(self, payload, /):
        # taken from https://github.com/bwp91/homebridge-meross/blob/latest/lib/device/diffuser.js
        if self._payload_ns != payload:
            self._payload_ns = payload
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

    # interface: LightEntity
    @MLLightBase.ha_action
    async def async_turn_on(self, **kwargs):
        if self._t_unsub:
            self._transition_cancel()

        _light = dict(self._payload_ns)
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


class MLDiffuserSpray(MLSpray):

    ns = mn.Appliance_Control_Diffuser_Spray

    OPTIONS_MAP = {
        mc.DIFFUSER_SPRAY_MODE_OFF: MLSpray.OPTIONS_MAP[mc.SPRAY_MODE_OFF],
        mc.DIFFUSER_SPRAY_MODE_ECO: MLSpray.OPTIONS_MAP[mc.SPRAY_MODE_INTERMITTENT],
        mc.DIFFUSER_SPRAY_MODE_FULL: MLSpray.OPTIONS_MAP[mc.SPRAY_MODE_CONTINUOUS],
    }


POLLING_STRATEGY_CONF.update(
    {
        mn.Appliance_Control_Diffuser_Sensor: (
            mlc.PARAM_SENSOR_SLOW_UPDATE_PERIOD,
            mlc.PARAM_SENSOR_SLOW_UPDATE_CLOUD_PERIOD,
            100,
            NamespaceHandler.async_poll_smart,
        ),
    }
)
