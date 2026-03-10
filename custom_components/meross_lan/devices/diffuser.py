from typing import TYPE_CHECKING, override

from ..helpers.namespaces import NamespaceHandler, mc, mlc, mn
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
from ..sensor import HumiditySensor, TemperatureSensor
from .spray import Spray

if TYPE_CHECKING:
    from typing import Final

    from ..helpers.device import Device, MerossMessage
    from ..merossclient.protocol.types import JsonDict
    from ..sensor import NumericSensor

    DIFFUSER_SENSOR_ENTITY_DEFS: Final

DIFFUSER_SENSOR_ENTITY_DEFS = {
    mc.KEY_HUMIDITY: HumiditySensor.ENTITY_DEF(),
    mc.KEY_TEMPERATURE: TemperatureSensor.ENTITY_DEF(device_scale=10),
}


def digest_init_diffuser(
    device: "Device", digest: "JsonDict", /
) -> "Device.DigestInitReturnType":
    """
    {
        "type": "mod100",
        "light": [{"channel": 0, "onoff": 0, "lmTime": 1639082117, "mode": 0, "luminance": 100, "rgb": 4129023}],
        "spray": [{"channel": 0, "mode": 2, "lmTime": 1644353195}]
    }
    """

    diffuser_light_handler = NamespaceHandler(
        mn.Appliance_Control_Diffuser_Light, device
    )
    diffuser_light_handler.register_entity_class(
        DiffuserLight, (light[mc.KEY_CHANNEL] for light in digest[mc.KEY_LIGHT])
    )

    diffuser_spray_handler = NamespaceHandler(
        mn.Appliance_Control_Diffuser_Spray, device
    )
    diffuser_spray_handler.register_entity_class(
        DiffuserSpray, (spray[mc.KEY_CHANNEL] for spray in digest[mc.KEY_SPRAY])
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
            # TODO: access entities by namespace handler parsers instead of by device.entities[key]
            # (we can store the entity in the handler when we create it)
            for key in DIFFUSER_SENSOR_ENTITY_DEFS:
                try:
                    value = message.payload[key][mc.KEY_VALUE]
                    try:
                        entity = device.entities[key]
                    except KeyError:
                        entity_def = DIFFUSER_SENSOR_ENTITY_DEFS[key]
                        entity = entity_def.type(None, device, **entity_def.kwargs)
                    entity.update_device_value(value)
                except KeyError:
                    continue

        NamespaceHandler(
            mn.Appliance_Control_Diffuser_Sensor,
            device,
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


class DiffuserLight(LightBase):
    """
    light entity for Meross diffuser (MOD100)
    """

    if TYPE_CHECKING:
        effect_list: list[str]

    ns = mn.Appliance_Control_Diffuser_Light

    def __init__(self, channel: int, manager: "Device", /):
        self.supported_color_modes = {ColorMode.RGB}
        LightBase.__init__(self, channel, manager, mc.DIFFUSER_LIGHT_MODE_LIST)

    @override
    def _parse_light(self, payload, /):
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

    ns = mn.Appliance_Control_Diffuser_Spray

    OPTIONS_MAP = {
        mc.DIFFUSER_SPRAY_MODE_OFF: Spray.OPTIONS_MAP[mc.SPRAY_MODE_OFF],
        mc.DIFFUSER_SPRAY_MODE_ECO: Spray.OPTIONS_MAP[mc.SPRAY_MODE_INTERMITTENT],
        mc.DIFFUSER_SPRAY_MODE_FULL: Spray.OPTIONS_MAP[mc.SPRAY_MODE_CONTINUOUS],
    }


NamespaceHandler.POLLING_CONFIG_MAP.update(
    {
        mn.Appliance_Control_Diffuser_Light: NamespaceHandler.POLLING_CONFIG_DIGEST_NS,
        mn.Appliance_Control_Diffuser_Spray: NamespaceHandler.POLLING_CONFIG_DIGEST_NS,
        mn.Appliance_Control_Diffuser_Sensor: NamespaceHandler.POLLING_CONFIG_SLOWSENSOR_NS,
    }
)
