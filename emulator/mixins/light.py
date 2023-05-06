""""""
from __future__ import annotations

import typing

from custom_components.meross_lan.merossclient import const as mc

from .. import MerossEmulator, MerossEmulatorDescriptor


class LightMixin(MerossEmulator if typing.TYPE_CHECKING else object):
    def __init__(self, descriptor: MerossEmulatorDescriptor, key):
        super().__init__(descriptor, key)

    def _SET_Appliance_Control_Light(self, header, payload):
        # need to override basic handler since lights turning on/off is tricky between
        # various firmwares: some supports onoff in light payload some use the togglex
        p_light = payload[mc.KEY_LIGHT]
        p_digest = self.descriptor.digest
        support_onoff_in_light = mc.KEY_ONOFF in p_digest[mc.KEY_LIGHT]
        # generally speaking set_light always turns on, unless the payload carries onoff = 0 and
        # the device is not using togglex
        if support_onoff_in_light:
            onoff = p_light.get(mc.KEY_ONOFF, 1)
            p_light[mc.KEY_ONOFF] = onoff
        else:
            onoff = 1
            p_light.pop(mc.KEY_ONOFF, None)
        if mc.KEY_TOGGLEX in p_digest:
            # fixed channel 0..that is..
            p_digest[mc.KEY_TOGGLEX][0][mc.KEY_ONOFF] = onoff
        p_digest[mc.KEY_LIGHT].update(p_light)
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
