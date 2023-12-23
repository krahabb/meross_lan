""""""
from __future__ import annotations

from random import randint
import typing

from custom_components.meross_lan.merossclient import const as mc

if typing.TYPE_CHECKING:
    from .. import MerossEmulator, MerossEmulatorDescriptor


class HubMixin(MerossEmulator if typing.TYPE_CHECKING else object):

    def __init__(self, descriptor: MerossEmulatorDescriptor, key):
        super().__init__(descriptor, key)

    def _GET_Appliance_Hub_Sensor_All(self, header, payload):
        response_payload = self.descriptor.namespaces[
            mc.NS_APPLIANCE_HUB_SENSOR_ALL
        ]

        for p_subdevice in response_payload[mc.KEY_ALL]:
            if mc.KEY_DOORWINDOW in p_subdevice:
                if randint(0, 4) == 0:
                    p_subdevice[mc.KEY_DOORWINDOW][mc.KEY_STATUS] = 1
                else:
                    p_subdevice[mc.KEY_DOORWINDOW][mc.KEY_STATUS] = 0
            elif mc.KEY_SMOKEALARM in p_subdevice:
                a = randint(0, 2)
                if a == 0:
                    p_subdevice[mc.KEY_SMOKEALARM][mc.KEY_STATUS] = randint(17, 27)
                elif a == 1:
                    p_subdevice[mc.KEY_SMOKEALARM][mc.KEY_STATUS] = 170

        return mc.METHOD_GETACK, response_payload
