from random import randint
from typing import TYPE_CHECKING

from custom_components.meross_lan.merossclient.protocol import (
    const as mc,
    namespaces as mn,
)

from . import Emulator

if TYPE_CHECKING:
    from . import EmulatorDescriptor


class FanMixin(Emulator if TYPE_CHECKING else object):

    NAMESPACES_DEFAULT: "Emulator.NSDefault" = {
        mn.Appliance_Control_FilterMaintenance: (
            Emulator.NSDefaultMode.MixOut,
            {mc.KEY_CHANNEL: 0, mc.KEY_LIFE: 100, mc.KEY_LMTIME: 0},
        ),
    }

    def __init__(self, descriptor: "EmulatorDescriptor", key):
        super().__init__(descriptor, key)

        if mc.KEY_FAN not in descriptor.digest:
            # map100
            self.update_namespace_state(
                mn.Appliance_Control_Fan,
                Emulator.NSDefaultMode.MixOut,
                {mc.KEY_CHANNEL: 0, mc.KEY_SPEED: 0, mc.KEY_MAXSPEED: 4},
            )

    def _scheduler(self):
        super()._scheduler()
        ns_name = mn.Appliance_Control_FilterMaintenance
        if ns_name in self.descriptor.ability:
            if lifedec := randint(0, 1):
                p_payload = self.namespaces[ns_name]
                p_payload_channel = p_payload[mc.KEY_FILTER][0]
                life = p_payload_channel[mc.KEY_LIFE]
                p_payload_channel[mc.KEY_LIFE] = life - lifedec
                p_payload_channel[mc.KEY_LMTIME] = self.epoch
                if self.mqtt_connected:
                    self.mqtt_publish_push(ns_name, p_payload)
