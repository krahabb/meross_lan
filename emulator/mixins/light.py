""""""
from __future__ import annotations

import typing

from custom_components.meross_lan.merossclient import (
    const as mc,
    get_element_by_key_safe,
)

from .. import MerossEmulator, MerossEmulatorDescriptor


class LightMixin(MerossEmulator if typing.TYPE_CHECKING else object):
    def __init__(self, descriptor: MerossEmulatorDescriptor, key):
        super().__init__(descriptor, key)

        if get_element_by_key_safe(
            descriptor.digest.get(mc.KEY_TOGGLEX),
            mc.KEY_CHANNEL,
            0,
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
        p_light = payload[mc.KEY_LIGHT]
        channel = p_light.get(mc.KEY_CHANNEL, 0)
        # generally speaking set_light always turns on, unless the payload carries onoff = 0 and
        # the device is not using togglex
        if self._togglex_switch:
            p_light.pop(mc.KEY_ONOFF, None)
            if not self._togglex_mode:
                p_digest[mc.KEY_TOGGLEX][channel][mc.KEY_ONOFF] = 1
        else:
            p_light[mc.KEY_ONOFF] = p_light.get(mc.KEY_ONOFF, 1)
        p_digest[mc.KEY_LIGHT] = p_light
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
