""""""

import typing

from custom_components.meross_lan.merossclient import (
    const as mc,
    get_element_by_key_safe,
    update_dict_strict,
)

if typing.TYPE_CHECKING:
    from .. import MerossEmulator, MerossEmulatorDescriptor


class LightMixin(MerossEmulator if typing.TYPE_CHECKING else object):
    def __init__(self, descriptor: "MerossEmulatorDescriptor", key):
        super().__init__(descriptor, key)

        if get_element_by_key_safe(
            descriptor.digest.get(mc.KEY_TOGGLEX),
            mc.KEY_CHANNEL,
            descriptor.digest[mc.KEY_LIGHT][mc.KEY_CHANNEL],
        ):
            self._togglex_switch = True  # use TOGGLEX to (auto) switch
            self._togglex_mode = (
                True  # True: need TOGGLEX to switch / False: auto-switch
            )
        else:
            self._togglex_switch = False
            self._togglex_mode = False

    def _SET_Appliance_Control_Light(self, header, payload):
        # need to override basic handler since lights turning on/off is tricky between
        # various firmwares: some supports onoff in light payload some use the togglex
        p_digest = self.descriptor.digest
        p_digest_light = p_digest[mc.KEY_LIGHT]
        p_light = payload[mc.KEY_LIGHT]
        channel = p_light[mc.KEY_CHANNEL]
        if channel != p_digest_light[mc.KEY_CHANNEL]:
            raise Exception("wrong request channel")
        # generally speaking set_light always turns on, unless the payload carries onoff = 0 and
        # the device is not using togglex
        if self._togglex_switch:
            p_light.pop(mc.KEY_ONOFF, None)
            if not self._togglex_mode:
                p_digest[mc.KEY_TOGGLEX][channel][mc.KEY_ONOFF] = 1
        else:
            p_light[mc.KEY_ONOFF] = p_light.get(mc.KEY_ONOFF, 1)
        update_dict_strict(p_digest_light, p_light)
        return mc.METHOD_SETACK, {}

    def _GET_Appliance_Control_Light_Effect(self, header, payload):
        if mc.NS_APPLIANCE_CONTROL_LIGHT_EFFECT not in self.descriptor.namespaces:
            raise Exception(
                f"{mc.NS_APPLIANCE_CONTROL_LIGHT_EFFECT} not available in emulator descriptor"
            )
        return (
            mc.METHOD_GETACK,
            self.descriptor.namespaces[mc.NS_APPLIANCE_CONTROL_LIGHT_EFFECT],
        )

    def _SET_Appliance_Control_Light_Effect(self, header, payload):

        for p_effect in payload[mc.KEY_EFFECT]:
            p_effect_id = p_effect[mc.KEY_ID_]
            p_effect_enable = p_effect.get(mc.KEY_ENABLE, 0)

            index = 0
            effect_index = -1
            for effect in self.descriptor.namespaces[mc.NS_APPLIANCE_CONTROL_LIGHT_EFFECT][
                mc.KEY_EFFECT
            ]:
                if effect[mc.KEY_ID_] == p_effect_id:
                    update_dict_strict(effect, p_effect)
                else:
                    if p_effect_enable:
                        effect[mc.KEY_ENABLE] = 0
                if effect[mc.KEY_ENABLE]:
                    assert effect_index == -1
                    effect_index = index
                index += 1

            p_digest_light: dict = self.descriptor.digest[mc.KEY_LIGHT]
            if effect_index == -1:
                p_digest_light.pop(mc.KEY_EFFECT, 0)
                p_digest_light[mc.KEY_CAPACITY] &= ~mc.LIGHT_CAPACITY_EFFECT
            else:
                p_digest_light[mc.KEY_EFFECT] = effect_index
                p_digest_light[mc.KEY_CAPACITY] |= mc.LIGHT_CAPACITY_EFFECT

        return mc.METHOD_SETACK, {}
