""""""

from __future__ import annotations

from random import randint
import typing

from custom_components.meross_lan.merossclient import (
    NAMESPACE_TO_KEY,
    const as mc,
    extract_dict_payloads,
    get_element_by_key,
    get_element_by_key_safe,
)

if typing.TYPE_CHECKING:
    from .. import MerossEmulator, MerossEmulatorDescriptor


class HubMixin(MerossEmulator if typing.TYPE_CHECKING else object):

    def __init__(self, descriptor: MerossEmulatorDescriptor, key):
        super().__init__(descriptor, key)

    def _get_subdevice_digest(self, subdevice_id: str):
        """returns the subdevice dict in the hub digest key"""
        return get_element_by_key(
            self.descriptor.digest[mc.KEY_HUB][mc.KEY_SUBDEVICE],
            mc.KEY_ID,
            subdevice_id,
        )

    def _get_subdevice_namespace(self, subdevice_id: str, namespace: str):
        """returns the subdevice namespace dict"""
        namespaces = self.descriptor.namespaces
        if namespace in namespaces:
            return get_element_by_key_safe(
                namespaces[namespace][NAMESPACE_TO_KEY[namespace]],
                mc.KEY_ID,
                subdevice_id,
            )
        return None

    def _get_mts100_all(self, subdevice_id: str):
        return self._get_subdevice_namespace(
            subdevice_id, mc.NS_APPLIANCE_HUB_MTS100_ALL
        )

    def _get_sensor_all(self, subdevice_id: str):
        return self._get_subdevice_namespace(
            subdevice_id, mc.NS_APPLIANCE_HUB_SENSOR_ALL
        )

    def _get_subdevice_all(self, subdevice_id: str):
        """returns the subdevice 'all' dict from either the Hub.Sensor.All or Hub.Mts100.All"""
        return self._get_mts100_all(subdevice_id) or self._get_sensor_all(subdevice_id)

    def _GET_Appliance_Hub_Sensor_All(self, header, payload):
        response_payload = self.descriptor.namespaces[mc.NS_APPLIANCE_HUB_SENSOR_ALL]

        for p_subdevice in response_payload[mc.KEY_ALL]:
            if mc.KEY_DOORWINDOW in p_subdevice:
                if randint(0, 4) == 0:
                    p_subdevice[mc.KEY_DOORWINDOW][mc.KEY_STATUS] = 1
                else:
                    p_subdevice[mc.KEY_DOORWINDOW][mc.KEY_STATUS] = 0
            elif mc.KEY_SMOKEALARM in p_subdevice:
                a = randint(0, 2)
                if a == 0:
                    p_subdevice[mc.KEY_SMOKEALARM][mc.KEY_STATUS] = randint(17, 27)
                elif a == 1:
                    p_subdevice[mc.KEY_SMOKEALARM][mc.KEY_STATUS] = 170

        return mc.METHOD_GETACK, response_payload

    def _SET_Appliance_Hub_Mts100_Mode(self, header, payload):
        for p_mode in payload[mc.KEY_MODE]:
            subdevice_id = p_mode[mc.KEY_ID]
            p_subdevice_digest = self._get_subdevice_digest(subdevice_id)
            for digest_mts_key in ("mts150", "mts100v3", "mts100"):
                # digest for mts valves has the usual fields plus a (sub)dict
                # named according to the model. Here we should find the mode
                if digest_mts_key in p_subdevice_digest:
                    mts_digest = p_subdevice_digest[digest_mts_key]
                    if mc.KEY_MODE in mts_digest:
                        mts_digest[mc.KEY_MODE] = p_mode[mc.KEY_STATE]
                    break

            p_subdevice_all = self._get_mts100_all(subdevice_id)
            if p_subdevice_all and mc.KEY_MODE in p_subdevice_all:
                # beware the "mode" key in "all" is embedded in the "mode" dict "state" key
                p_subdevice_all[mc.KEY_MODE][mc.KEY_STATE] = p_mode[mc.KEY_STATE]

            p_subdevice_mode = self._get_subdevice_namespace(
                subdevice_id, mc.NS_APPLIANCE_HUB_MTS100_MODE
            )
            if p_subdevice_mode and mc.KEY_STATE in p_subdevice_mode:
                p_subdevice_mode[mc.KEY_STATE] = p_mode[mc.KEY_STATE]

        return mc.METHOD_SETACK, {}

    def _SET_Appliance_Hub_ToggleX(self, header, payload):
        for p_togglex in extract_dict_payloads(payload[mc.KEY_TOGGLEX]):
            subdevice_id = p_togglex[mc.KEY_ID]
            p_subdevice_digest = self._get_subdevice_digest(subdevice_id)
            if mc.KEY_ONOFF in p_subdevice_digest:
                p_subdevice_digest[mc.KEY_ONOFF] = p_togglex[mc.KEY_ONOFF]

            p_subdevice_all = self._get_subdevice_all(subdevice_id)
            if p_subdevice_all and mc.KEY_TOGGLEX in p_subdevice_all:
                # beware the "onoff" key in "all" is a embedded in the "togglex" dict
                p_subdevice_all[mc.KEY_TOGGLEX][mc.KEY_ONOFF] = p_togglex[mc.KEY_ONOFF]

            p_subdevice_togglex = self._get_subdevice_namespace(
                subdevice_id, mc.NS_APPLIANCE_HUB_TOGGLEX
            )
            if p_subdevice_togglex and mc.KEY_ONOFF in p_subdevice_togglex:
                p_subdevice_togglex[mc.KEY_ONOFF] = p_togglex[mc.KEY_ONOFF]

        return mc.METHOD_SETACK, {}
