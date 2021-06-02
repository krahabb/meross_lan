

from typing import Optional, Union

from .merossclient import KeyType, const as mc  # mEROSS cONST
from .meross_device import MerossDevice
from .light import MerossLanLight
from .logger import LOGGER

class MerossDeviceBulb(MerossDevice):

    def __init__(self, api, descriptor, entry) -> None:
        super().__init__(api, descriptor, entry)

        try:
            # we expect a well structured digest here since
            # we're sure 'light' key is there by __init__ device factory
            p_digest = self.descriptor.digest
            p_light = p_digest[mc.KEY_LIGHT]
            if isinstance(p_light, list):
                for l in p_light:
                    MerossLanLight(self, l)
            elif isinstance(p_light, dict):
                MerossLanLight(self, p_light)

        except Exception as e:
            LOGGER.warning("MerossDeviceBulb(%s) init exception:(%s)", self.device_id, str(e))


    def receive(
        self,
        namespace: str,
        method: str,
        payload: dict,
        replykey: KeyType
    ) -> bool:

        if super().receive(namespace, method, payload, replykey):
            return True

        if namespace == mc.NS_APPLIANCE_CONTROL_LIGHT:
            self._update_light(payload)
            return True

        if namespace == mc.NS_APPLIANCE_CONTROL_TOGGLEX:
            togglex = payload.get(mc.KEY_TOGGLEX)
            if isinstance(togglex, list):
                for t in togglex:
                    self.entities[t.get(mc.KEY_CHANNEL)]._set_onoff(t.get(mc.KEY_ONOFF))
            elif isinstance(togglex, dict):
                self.entities[togglex.get(mc.KEY_CHANNEL)]._set_onoff(togglex.get(mc.KEY_ONOFF))

        return False


    def _update_descriptor(self, payload: dict) -> bool:
        update = super()._update_descriptor(payload)

        p_digest = self.descriptor.digest
        if p_digest:
            self._update_light(p_digest)

        return update


    def _update_light(self, payload: dict) -> None:
        p_light = payload.get(mc.KEY_LIGHT)
        if isinstance(p_light, dict):
            self.entities[p_light.get(mc.KEY_CHANNEL)]._set_light(p_light)