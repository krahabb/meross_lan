""""""

import typing

from custom_components.meross_lan.merossclient import (
    const as mc,
    get_element_by_key,
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

        if mc.NS_APPLIANCE_CONTROL_LIGHT_EFFECT in descriptor.ability:
            descriptor.namespaces.setdefault(
                mc.NS_APPLIANCE_CONTROL_LIGHT_EFFECT, {mc.KEY_EFFECT: []}
            )

    def _SET_Appliance_Control_Light(self, header, payload):
        # need to override basic handler since lights turning on/off is tricky between
        # various firmwares: some supports onoff in light payload some use the togglex
        p_digest = self.descriptor.digest
        p_digest_light: dict = p_digest[mc.KEY_LIGHT]
        p_digest_light_saved = dict(p_digest_light)
        p_light: dict = payload[mc.KEY_LIGHT]
        channel = p_light[mc.KEY_CHANNEL]
        if channel != p_digest_light[mc.KEY_CHANNEL]:
            raise Exception("wrong request channel")
        # generally speaking set_light always turns on, unless the payload carries onoff = 0 and
        # the device is not using togglex
        if self._togglex_switch:
            p_light.pop(mc.KEY_ONOFF, None)
            if not self._togglex_mode:
                p_digest_togglex: dict = p_digest[mc.KEY_TOGGLEX][channel]
                if not p_digest_togglex.get(mc.KEY_ONOFF):
                    p_digest_togglex[mc.KEY_ONOFF] = 1
                    if self.mqtt_connected:
                        self.mqtt_publish_push(
                            mc.NS_APPLIANCE_CONTROL_TOGGLEX,
                            {mc.KEY_TOGGLEX: p_digest_togglex},
                        )
        else:
            p_light[mc.KEY_ONOFF] = p_light.get(mc.KEY_ONOFF, 1)
        update_dict_strict(p_digest_light, p_light)

        if self.mqtt_connected and (p_digest_light != p_digest_light_saved):
            self.mqtt_publish_push(
                mc.NS_APPLIANCE_CONTROL_LIGHT, {mc.KEY_LIGHT: p_digest_light}
            )

        return mc.METHOD_SETACK, {}

    def _GET_Appliance_Control_Light_Effect(self, header, payload):
        return (
            mc.METHOD_GETACK,
            self.descriptor.namespaces[mc.NS_APPLIANCE_CONTROL_LIGHT_EFFECT],
        )

    def _SET_Appliance_Control_Light_Effect(self, header, payload):

        p_state_effect_list: list[dict] = self.descriptor.namespaces[
            mc.NS_APPLIANCE_CONTROL_LIGHT_EFFECT
        ][mc.KEY_EFFECT]
        effect_id_enabled = None

        for p_effect in payload[mc.KEY_EFFECT]:
            effect_id = p_effect[mc.KEY_ID_]
            if p_effect.get(mc.KEY_ENABLE):
                effect_id_enabled = effect_id
            try:
                p_state_effect = get_element_by_key(
                    p_state_effect_list, mc.KEY_ID_, effect_id
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

        p_light: dict = self.descriptor.digest[mc.KEY_LIGHT]
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
            self.mqtt_publish_push(
                mc.NS_APPLIANCE_CONTROL_LIGHT, {mc.KEY_LIGHT: p_light}
            )

        return mc.METHOD_SETACK, {}
