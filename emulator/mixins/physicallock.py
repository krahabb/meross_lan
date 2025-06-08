""""""

from random import randint
import typing

from custom_components.meross_lan.merossclient.protocol import (
    const as mc,
    namespaces as mn,
)

if typing.TYPE_CHECKING:
    from .. import MerossEmulator, MerossEmulatorDescriptor


class PhysicalLockMixin(MerossEmulator if typing.TYPE_CHECKING else object):
    def __init__(self, descriptor: "MerossEmulatorDescriptor", key):
        super().__init__(descriptor, key)
        self.update_namespace_state(
            mn.Appliance_Control_PhysicalLock.name,
            0,
            {
                mc.KEY_ONOFF: 0,
            },
        )

    def _scheduler(self):
        super()._scheduler()
        p_payload = self.descriptor.namespaces[mn.Appliance_Control_PhysicalLock.name]
        if 0 == randint(0, 10):
            p_payload_channel = p_payload[mc.KEY_LOCK][0]
            onoff = p_payload_channel[mc.KEY_ONOFF]
            p_payload_channel[mc.KEY_ONOFF] = 1 - onoff
            if self.mqtt_connected:
                self.mqtt_publish_push(
                    mn.Appliance_Control_PhysicalLock.name, p_payload
                )
