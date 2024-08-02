""""""

from random import randint
import typing

from custom_components.meross_lan.merossclient import (
    MerossRequest,
    const as mc,
    namespaces as mn,
)

if typing.TYPE_CHECKING:
    from .. import MerossEmulator, MerossEmulatorDescriptor


class FanMixin(MerossEmulator if typing.TYPE_CHECKING else object):
    def __init__(self, descriptor: "MerossEmulatorDescriptor", key):
        super().__init__(descriptor, key)

        if mc.KEY_FAN not in descriptor.digest:
            # map100 doesn't carry 'fan' digest key so
            # we'll ensure it's state is available in the namespaces
            self.update_namespace_state(
                mn.Appliance_Control_Fan.name,
                0,
                {
                    mc.KEY_SPEED: 0,
                    mc.KEY_MAXSPEED: 4,
                },
            )

        if mn.Appliance_Control_FilterMaintenance.name in descriptor.ability:
            self.update_namespace_state(
                mn.Appliance_Control_FilterMaintenance.name,
                0,
                {
                    mc.KEY_LIFE: 100,
                    mc.KEY_LMTIME: self.epoch,
                },
            )

    def _scheduler(self):
        super()._scheduler()
        if mn.Appliance_Control_FilterMaintenance.name in self.descriptor.ability:
            if lifedec := randint(0, 1):
                p_payload = self.descriptor.namespaces[
                    mn.Appliance_Control_FilterMaintenance.name
                ]
                p_payload_channel = p_payload[mc.KEY_FILTER][0]
                life = p_payload_channel[mc.KEY_LIFE]
                p_payload_channel[mc.KEY_LIFE] = life - lifedec
                p_payload_channel[mc.KEY_LMTIME] = self.epoch
                if self.mqtt_connected:
                    self.mqtt_publish_push(
                        mn.Appliance_Control_FilterMaintenance.name, p_payload
                    )
