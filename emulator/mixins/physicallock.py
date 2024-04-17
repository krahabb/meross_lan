""""""

from __future__ import annotations
from random import randint

import typing

from custom_components.meross_lan.merossclient import (
    MerossRequest,
    const as mc,
)

if typing.TYPE_CHECKING:
    from .. import MerossEmulator, MerossEmulatorDescriptor


class PhysicalLockMixin(MerossEmulator if typing.TYPE_CHECKING else object):
    def __init__(self, descriptor: MerossEmulatorDescriptor, key):
        super().__init__(descriptor, key)
        self.update_namespace_state(
            mc.NS_APPLIANCE_CONTROL_PHYSICALLOCK,
            0,
            {
                mc.KEY_ONOFF: 0,
            },
        )

    def _scheduler(self):
        super()._scheduler()
        p_payload = self.descriptor.namespaces[mc.NS_APPLIANCE_CONTROL_PHYSICALLOCK]
        if 0 == randint(0, 10):
            p_payload_channel = p_payload[mc.KEY_LOCK][0]
            onoff = p_payload_channel[mc.KEY_ONOFF]
            p_payload_channel[mc.KEY_ONOFF] = 1 - onoff
            if self.mqtt_connected:
                self.mqtt_publish_push(mc.NS_APPLIANCE_CONTROL_PHYSICALLOCK, p_payload)
