from __future__ import annotations

from .merossclient import KeyType, MerossDeviceDescriptor, const as mc  # mEROSS cONST
from .meross_device import MerossDevice
from .light import MerossLanLight
from .helpers import LOGGER

class MerossDeviceBulb(MerossDevice):
    """
    Specialized class for light based devices
    """
    effect_dict_ids: dict[int, str] = dict()
    effect_dict_names: dict[str, int] = dict()
    effect_list: list[str] = list()

    def __init__(self, api, descriptor: MerossDeviceDescriptor, entry) -> None:
        super().__init__(api, descriptor, entry)

        try:
            # we expect a well structured digest here since
            # we're sure 'light' key is there by __init__ device factory
            p_digest = descriptor.digest
            p_light = p_digest[mc.KEY_LIGHT]
            if isinstance(p_light, list):
                for l in p_light:
                    MerossLanLight(self, l.get(mc.KEY_CHANNEL, 0), p_digest.get(mc.KEY_TOGGLEX))
            elif isinstance(p_light, dict):
                MerossLanLight(self, p_light.get(mc.KEY_CHANNEL, 0), p_digest.get(mc.KEY_TOGGLEX))

            if mc.NS_APPLIANCE_CONTROL_LIGHT_EFFECT in descriptor.ability:
                self.polling_dictionary.append(mc.NS_APPLIANCE_CONTROL_LIGHT_EFFECT)

        except Exception as e:
            LOGGER.warning("MerossDeviceBulb(%s) init exception:(%s)", self.device_id, str(e))


    def receive(
        self,
        namespace: str,
        method: str,
        payload: dict,
        header: dict
    ) -> bool:

        if super().receive(namespace, method, payload, header):
            return True

        if namespace == mc.NS_APPLIANCE_CONTROL_LIGHT:
            self._parse_light(payload.get(mc.KEY_LIGHT))
            return True

        if namespace == mc.NS_APPLIANCE_CONTROL_LIGHT_EFFECT:
            effect_dict_ids = dict()
            for p_effect in payload.get(mc.KEY_EFFECT, []):
                effect_dict_ids[int(p_effect[mc.KEY_ID_])] = p_effect[mc.KEY_EFFECTNAME]
            if effect_dict_ids != self.effect_dict_ids:
                effect_dict_names = dict()
                effect_list = list()
                for _id, _name in effect_dict_ids.items():
                    effect_list.append(_name)
                    effect_dict_names[_name] = _id
                self.effect_dict_ids = effect_dict_ids
                self.effect_dict_names = effect_dict_names
                self.effect_list = effect_list
                for entity in self.entities:
                    if isinstance(entity, MerossLanLight):
                        entity.update_effect_list()
            return True

        return False


    def _parse_light(self, payload) -> None:
        if isinstance(payload, dict):
            self.entities[payload.get(mc.KEY_CHANNEL)].update_light(payload)
