from random import randint
import typing

from custom_components.meross_lan.merossclient.protocol import (
    const as mc,
    namespaces as mn,
)

if typing.TYPE_CHECKING:
    from .. import MerossEmulator


class FanMixin(MerossEmulator if typing.TYPE_CHECKING else object):

    NAMESPACES_DEFAULT: "MerossEmulator.NamespacesDefault" = {
        mn.Appliance_Control_Fan.name: (
            mc.KEY_CHANNEL,
            0,
            {
                mc.KEY_SPEED: 0,
                mc.KEY_MAXSPEED: 4,
            },
        ),
        mn.Appliance_Control_FilterMaintenance.name: (
            mc.KEY_CHANNEL,
            0,
            {
                mc.KEY_LIFE: 100,
                mc.KEY_LMTIME: 0,
            },
        ),
    }

    def _scheduler(self):
        super()._scheduler()
        ns_name = mn.Appliance_Control_FilterMaintenance.name
        if ns_name in self.descriptor.ability:
            if lifedec := randint(0, 1):
                p_payload = self.namespaces[ns_name]
                p_payload_channel = p_payload[mc.KEY_FILTER][0]
                life = p_payload_channel[mc.KEY_LIFE]
                p_payload_channel[mc.KEY_LIFE] = life - lifedec
                p_payload_channel[mc.KEY_LMTIME] = self.epoch
                if self.mqtt_connected:
                    self.mqtt_publish_push(ns_name, p_payload)
