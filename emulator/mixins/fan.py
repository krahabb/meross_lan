""""""

from __future__ import annotations

import typing

from custom_components.meross_lan.merossclient import const as mc, extract_dict_payloads

if typing.TYPE_CHECKING:
    from .. import MerossEmulator, MerossEmulatorDescriptor


class FanMixin(MerossEmulator if typing.TYPE_CHECKING else object):
    def __init__(self, descriptor: MerossEmulatorDescriptor, key):
        super().__init__(descriptor, key)

        self.update_namespace_state(mc.NS_APPLIANCE_CONTROL_FAN, 0, {
                mc.KEY_SPEED: 0,
                mc.KEY_MAXSPEED: 4,
        })

    def _PUSH_Appliance_Control_FilterMaintenance(self, header, payload):
        return mc.METHOD_PUSH, {
            mc.KEY_FILTER: [
                {
                    mc.KEY_CHANNEL: 0,
                    mc.KEY_LIFE: 100,
                    mc.KEY_LMTIME: self.epoch,
                }
            ]
        }
