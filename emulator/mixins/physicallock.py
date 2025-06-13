""""""

from random import randint
from typing import TYPE_CHECKING

from custom_components.meross_lan.merossclient.protocol import (
    const as mc,
    namespaces as mn,
)

if TYPE_CHECKING:
    from .. import MerossEmulator


class PhysicalLockMixin(MerossEmulator if TYPE_CHECKING else object):
    NAMESPACES_DEFAULT: "MerossEmulator.NamespacesDefault" = {
        mn.Appliance_Control_PhysicalLock.name: (
            mc.KEY_CHANNEL,
            0,
            {
                mc.KEY_ONOFF: 0,
            },
        ),
    }

    def _scheduler(self):
        super()._scheduler()
        p_payload = self.namespaces[mn.Appliance_Control_PhysicalLock.name]
        if 0 == randint(0, 10):
            p_payload_channel = p_payload[mc.KEY_LOCK][0]
            onoff = p_payload_channel[mc.KEY_ONOFF]
            p_payload_channel[mc.KEY_ONOFF] = 1 - onoff
            if self.mqtt_connected:
                self.mqtt_publish_push(
                    mn.Appliance_Control_PhysicalLock.name, p_payload
                )
