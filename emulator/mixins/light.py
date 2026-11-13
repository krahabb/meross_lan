""""""

from typing import TYPE_CHECKING

from custom_components.meross_lan.merossclient import (
    get_element_by_key,
    update_dict_strict,
)
from custom_components.meross_lan.merossclient.protocol import (
    const as mc,
    namespaces as mn,
    types as mt,
)

if TYPE_CHECKING:
    from typing import Final

    from . import Emulator, EmulatorDescriptor


class LightMixin(Emulator if TYPE_CHECKING else object):
    if TYPE_CHECKING:
        _light_onoff: Final[bool]
        _togglex_switch: Final[bool]
        _togglex_auto: bool | None

    def __init__(self, descriptor: "EmulatorDescriptor", **kwargs):
        super().__init__(descriptor, **kwargs)

        self.light_state: "mt.control.Light" = self.namespaces[
            mn.Appliance_Control_Light
        ][mn.Appliance_Control_Light.key]
        self._light_onoff = mc.KEY_ONOFF in self.light_state

        try:
            get_element_by_key(
                descriptor.digest[mc.KEY_TOGGLEX],
                descriptor.digest[mc.KEY_LIGHT],
            )
            # use TOGGLEX to (auto) switch
            self._togglex_switch = True
            # True: auto-switch - False: need TOGGLEX to switch
            self._togglex_auto = False
            assert not self._light_onoff
        except:
            self._togglex_switch = False
            self._togglex_auto = None

        assert self._light_onoff != self._togglex_switch

    def _SET_Appliance_Control_Light(self, header, payload):
        # need to override basic handler since lights turning on/off is tricky between
        # various firmwares: some supports onoff in light payload some use the togglex
        light_request: "mt.control.Light" = payload[mc.KEY_LIGHT]
        light_state = self.light_state
        channel = light_request[mc.KEY_CHANNEL]
        if channel != light_state[mc.KEY_CHANNEL]:
            raise Exception("wrong request channel")
        if self._light_onoff != (mc.KEY_ONOFF in light_request):
            raise Exception("'onoff' key mismatch in payload")
        update_dict_strict(light_state, light_request)
        if self.mqtt_connected:
            # publish even if it didn't change..not harming anyway
            self.mqtt_publish_push(
                mn.Appliance_Control_Light, {mc.KEY_LIGHT: light_state}
            )

        # generally speaking set_light always turns on, unless the payload carries onoff = 0 and
        # the device is not using togglex
        if self._togglex_switch:

            def _update_togglex(onoff):
                p_digest_togglex = self.descriptor.digest[mc.KEY_TOGGLEX][channel]
                if p_digest_togglex[mc.KEY_ONOFF] != onoff:
                    p_digest_togglex[mc.KEY_ONOFF] = onoff
                    if self.mqtt_connected:
                        self.mqtt_publish_push(
                            mn.Appliance_Control_ToggleX,
                            {mn.Appliance_Control_ToggleX.key: p_digest_togglex},
                        )

            if mc.KEY_ONOFF in light_state:
                _update_togglex(light_state[mc.KEY_ONOFF])
            else:
                if self._togglex_auto:
                    _update_togglex(1)

        return mc.METHOD_SETACK, {}

    def _SET_Appliance_Control_Light_Effect(self, header, payload):

        p_state_effect_list: list[dict] = self.namespaces[
            mn.Appliance_Control_Light_Effect
        ][mc.KEY_EFFECT]
        effect_id_enabled = None

        for p_effect in payload[mc.KEY_EFFECT]:
            effect_id = p_effect[mc.KEY_ID_]
            if p_effect.get(mc.KEY_ENABLE):
                effect_id_enabled = effect_id
            try:
                p_state_effect = get_element_by_key(
                    p_state_effect_list, effect_id, mc.KEY_ID_
                )
                p_state_effect.update(p_effect)
            except KeyError:
                p_state_effect_list.append(p_effect)

        # now check which effect is enabled (if any) and ensure it is the last
        # enabled by disabling any previously set
        index = 0
        effect_index = -1
        for p_effect in p_state_effect_list:
            if p_effect[mc.KEY_ENABLE]:
                if effect_id_enabled and (p_effect[mc.KEY_ID_] != effect_id_enabled):
                    p_effect[mc.KEY_ENABLE] = 0
                else:
                    effect_index = index
            index += 1

        p_light = self.descriptor.digest[mc.KEY_LIGHT]
        p_light_saved = dict(p_light)
        if effect_index == -1:
            p_light.pop(mc.KEY_EFFECT, None)
            p_light[mc.KEY_CAPACITY] = (
                p_light[mc.KEY_CAPACITY] & ~mc.LIGHT_CAPACITY_EFFECT
            )
        else:
            p_light[mc.KEY_EFFECT] = effect_index
            p_light[mc.KEY_CAPACITY] = (
                p_light[mc.KEY_CAPACITY] | mc.LIGHT_CAPACITY_EFFECT
            )
        if self.mqtt_connected and (p_light != p_light_saved):
            self.mqtt_publish_push(mn.Appliance_Control_Light, {mc.KEY_LIGHT: p_light})

        return mc.METHOD_SETACK, {}
