""""""

from random import randint
import typing

from custom_components.meross_lan.merossclient import MerossRequest, const as mc

if typing.TYPE_CHECKING:
    from .. import MerossEmulator, MerossEmulatorDescriptor


class FanMixin(MerossEmulator if typing.TYPE_CHECKING else object):
    def __init__(self, descriptor: "MerossEmulatorDescriptor", key):
        super().__init__(descriptor, key)

        self.update_namespace_state(
            mc.NS_APPLIANCE_CONTROL_FAN,
            0,
            {
                mc.KEY_SPEED: 0,
                mc.KEY_MAXSPEED: 4,
            },
        )

        if mc.NS_APPLIANCE_CONTROL_FILTERMAINTENANCE in descriptor.ability:
            self.update_namespace_state(
                mc.NS_APPLIANCE_CONTROL_FILTERMAINTENANCE,
                0,
                {
                    mc.KEY_LIFE: 100,
                    mc.KEY_LMTIME: self.epoch,
                },
            )

    def _scheduler(self):
        super()._scheduler()
        if mc.NS_APPLIANCE_CONTROL_FILTERMAINTENANCE in self.descriptor.ability:
            if lifedec := randint(0, 1):
                p_payload = self.descriptor.namespaces[
                    mc.NS_APPLIANCE_CONTROL_FILTERMAINTENANCE
                ]
                p_payload_channel = p_payload[mc.KEY_FILTER][0]
                life = p_payload_channel[mc.KEY_LIFE]
                p_payload_channel[mc.KEY_LIFE] = life - lifedec
                p_payload_channel[mc.KEY_LMTIME] = self.epoch
                if self.mqtt_connected:
                    self.mqtt_publish_push(
                        mc.NS_APPLIANCE_CONTROL_FILTERMAINTENANCE, p_payload
                    )
