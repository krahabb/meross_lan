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
    get_mts_digest,
)

if typing.TYPE_CHECKING:
    from typing import Any

    from .. import MerossEmulator, MerossEmulatorDescriptor


class HubMixin(MerossEmulator if typing.TYPE_CHECKING else object):

    def __init__(self, descriptor: MerossEmulatorDescriptor, key):
        super().__init__(descriptor, key)
        # we have to sanitize our structures since it might happen some traces
        # have broken payloads due to errors while tracing but some of these
        # need to be consistently in place in order for the emulator to behave correctly
        # the most important ones being the 'Sensor.All' and 'Mts100.All' namespaces
        # to be in sync with the digest
        """
        Examples:
        digest:
        {
            "hub": {"hubId": -381895630, "mode": 0, "subdevice": [
                {"id": "120027D21C19", "status": 2},
                {"id": "01008C11", "status": 2, "scheduleBMode": 6},
                {"id": "0100783A", "status": 1, "scheduleBMode": 6, "onoff": 1, "lastActiveTime": 1646299642, "mts100v3": {"mode": 2}}
                ]}
        }

        "Appliance.Hub.Mts100.All":
        {
            "all": [
                {"id": "01008C11", "scheduleBMode": 6, "online": {"status": 2}},
                {"id": "0100783A", "scheduleBMode": 6, "online": {"status": 1, "lastActiveTime": 1646299642},
                    "togglex": {"onoff": 1},
                    "timeSync": {"state": 1},
                    "mode": {"state": 2},
                    "temperature": {"room": 120, "currentSet": 180, "custom": 225, "comfort": 240, "economy": 180, "max": 350, "min": 50, "heating": 1, "away": 120, "openWindow": 0}
                }
            ]
        }
        "Appliance.Hub.Mts100.Temperature":
        {
            "temperature": [
                {"id": "0100783A", "room": 120, "currentSet": 180, "custom": 225, "comfort": 240, "economy": 180, "max": 350, "min": 50, "heating": 1, "away": 120, "openWindow": 0}
            ]
        }
        """
        digest_subdevices = descriptor.digest[mc.KEY_HUB][mc.KEY_SUBDEVICE]
        namespaces = descriptor.namespaces
        ability = descriptor.ability
        if mc.NS_APPLIANCE_HUB_MTS100_ALL in ability:
            mts100_all: list | None = namespaces.setdefault(
                mc.NS_APPLIANCE_HUB_MTS100_ALL, {mc.KEY_ALL: []}
            )[mc.KEY_ALL]
        else:
            mts100_all = None
        if mc.NS_APPLIANCE_HUB_MTS100_TEMPERATURE in ability:
            mts100_temperature: list | None = namespaces.setdefault(
                mc.NS_APPLIANCE_HUB_MTS100_TEMPERATURE, {mc.KEY_TEMPERATURE: []}
            )[mc.KEY_TEMPERATURE]
        else:
            mts100_temperature = None
        if mc.NS_APPLIANCE_HUB_SENSOR_ALL in ability:
            sensor_all: list | None = namespaces.setdefault(
                mc.NS_APPLIANCE_HUB_SENSOR_ALL, {mc.KEY_ALL: []}
            )[mc.KEY_ALL]
        else:
            sensor_all = None
        for p_subdevice_digest in digest_subdevices:
            subdevice_id = p_subdevice_digest[mc.KEY_ID]
            p_mts_digest = get_mts_digest(p_subdevice_digest)
            if p_mts_digest is not None:
                # this subdevice is an mts like so we'll ensure it's
                # 'all' payload (at least) is set
                assert (
                    mts100_all is not None
                ), f"Hub emulator init: missing {mc.NS_APPLIANCE_HUB_MTS100_ALL}"
                p_subdevice_all = get_element_by_key_safe(
                    mts100_all,
                    mc.KEY_ID,
                    subdevice_id,
                )
                if not p_subdevice_all:
                    p_subdevice_all = {mc.KEY_ID: subdevice_id}
                    if mc.KEY_SCHEDULEBMODE in p_subdevice_digest:
                        p_subdevice_all[mc.KEY_SCHEDULEBMODE] = p_subdevice_digest[
                            mc.KEY_SCHEDULEBMODE
                        ]
                    if mc.KEY_STATUS in p_subdevice_digest:
                        p_subdevice_all[mc.KEY_ONLINE] = {
                            mc.KEY_STATUS: p_subdevice_digest[mc.KEY_STATUS]
                        }
                    if mc.KEY_ONOFF in p_subdevice_digest:
                        p_subdevice_all[mc.KEY_TOGGLEX] = {
                            mc.KEY_ONOFF: p_subdevice_digest[mc.KEY_ONOFF]
                        }
                    if mc.KEY_MODE in p_mts_digest:
                        p_subdevice_all[mc.KEY_MODE] = {
                            mc.KEY_STATE: p_mts_digest[mc.KEY_MODE]
                        }
                    if mts100_temperature:
                        # recover the temperature dict for this mts from the (eventual)
                        # Mts100.temperature namespace query
                        p_mts100_temperature = get_element_by_key_safe(
                            mts100_temperature,
                            mc.KEY_ID,
                            subdevice_id,
                        )
                        if p_mts100_temperature:
                            p_mts100_temperature = dict(p_mts100_temperature)
                            p_mts100_temperature.pop(mc.KEY_ID)
                            p_subdevice_all[mc.KEY_TEMPERATURE] = p_mts100_temperature

                    mts100_all.append(p_subdevice_all)
            else:
                # this subdevice is a sensor like so we'll ensure it's
                # 'all' payload (at least) is set
                assert (
                    sensor_all is not None
                ), f"Hub emulator init: missing {mc.NS_APPLIANCE_HUB_SENSOR_ALL}"
                p_subdevice_all = get_element_by_key_safe(
                    sensor_all,
                    mc.KEY_ID,
                    subdevice_id,
                )
                if not p_subdevice_all:
                    # TODO: build a reasonable default
                    pass

    def _get_subdevice_digest(self, subdevice_id: str):
        """returns the subdevice dict from the hub digest key"""
        return get_element_by_key(
            self.descriptor.digest[mc.KEY_HUB][mc.KEY_SUBDEVICE],
            mc.KEY_ID,
            subdevice_id,
        )

    def _get_subdevice_namespace(
        self, subdevice_id: str, namespace: str
    ) -> dict[str, Any]:
        """returns the subdevice namespace dict. It will create a default entry if not present
        and the device abilities supports the namespace."""
        namespaces = self.descriptor.namespaces
        if namespace in namespaces:
            subdevices_namespace: list = namespaces[namespace][
                NAMESPACE_TO_KEY[namespace]
            ]
            try:
                return get_element_by_key(
                    subdevices_namespace,
                    mc.KEY_ID,
                    subdevice_id,
                )
            except KeyError:
                p_subdevice = {mc.KEY_ID: subdevice_id}
                subdevices_namespace.append(p_subdevice)
        else:
            assert (
                namespace in self.descriptor.ability
            ), f"{namespace} not available in Hub abilities"
            p_subdevice = {mc.KEY_ID: subdevice_id}
            namespaces[namespace] = {NAMESPACE_TO_KEY[namespace]: [p_subdevice]}
        return p_subdevice

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

    def _SET_Appliance_Hub_Mts100_Adjust(self, header, payload):
        for p_subdevice in payload[mc.KEY_ADJUST]:
            subdevice_id = p_subdevice[mc.KEY_ID]
            p_subdevice_adjust = self._get_subdevice_namespace(
                subdevice_id, mc.NS_APPLIANCE_HUB_MTS100_ADJUST
            )
            p_subdevice_adjust[mc.KEY_TEMPERATURE] = p_subdevice[mc.KEY_TEMPERATURE]

        return mc.METHOD_SETACK, {}

    def _SET_Appliance_Hub_Mts100_Mode(self, header, payload):
        for p_mode in payload[mc.KEY_MODE]:
            subdevice_id = p_mode[mc.KEY_ID]
            p_subdevice_digest = self._get_subdevice_digest(subdevice_id)
            mts_digest = get_mts_digest(p_subdevice_digest)
            if mts_digest:
                if mc.KEY_MODE in mts_digest:
                    mts_digest[mc.KEY_MODE] = p_mode[mc.KEY_STATE]

            p_subdevice_all = self._get_mts100_all(subdevice_id)
            if mc.KEY_MODE in p_subdevice_all:
                # beware the "mode" key in "all" is embedded in the "mode" dict "state" key
                p_subdevice_all[mc.KEY_MODE][mc.KEY_STATE] = p_mode[mc.KEY_STATE]
            else:
                p_subdevice_all[mc.KEY_MODE] = {mc.KEY_STATE: p_mode[mc.KEY_STATE]}

            p_subdevice_mode = self._get_subdevice_namespace(
                subdevice_id, mc.NS_APPLIANCE_HUB_MTS100_MODE
            )
            p_subdevice_mode[mc.KEY_STATE] = p_mode[mc.KEY_STATE]

        return mc.METHOD_SETACK, {}

    def _SET_Appliance_Hub_Mts100_Temperature(self, header, payload):
        for p_subdevice in payload[mc.KEY_TEMPERATURE]:
            subdevice_id = p_subdevice[mc.KEY_ID]
            p_subdevice_temperature = self._get_subdevice_namespace(
                subdevice_id, mc.NS_APPLIANCE_HUB_MTS100_TEMPERATURE
            )
            p_subdevice_temperature.update(p_subdevice)

        return mc.METHOD_SETACK, {}

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
            p_subdevice_togglex[mc.KEY_ONOFF] = p_togglex[mc.KEY_ONOFF]

        return mc.METHOD_SETACK, {}
