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
    update_dict_strict,
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
        ns_state: dict[str, list[dict]] = {}
        for namespace in (
            mc.NS_APPLIANCE_HUB_MTS100_ADJUST,
            mc.NS_APPLIANCE_HUB_MTS100_ALL,
            mc.NS_APPLIANCE_HUB_MTS100_MODE,
            mc.NS_APPLIANCE_HUB_MTS100_SCHEDULEB,
            mc.NS_APPLIANCE_HUB_MTS100_TEMPERATURE,
            mc.NS_APPLIANCE_HUB_SENSOR_ADJUST,
            mc.NS_APPLIANCE_HUB_SENSOR_ALL,
            mc.NS_APPLIANCE_HUB_ONLINE,
            mc.NS_APPLIANCE_HUB_TOGGLEX,
        ):
            if namespace in ability:
                key_namespace = NAMESPACE_TO_KEY[namespace]
                ns_state[namespace] = namespaces.setdefault(
                    namespace, {key_namespace: []}
                )[key_namespace]

        # these maps help in generalizing the rules for
        # digest <-> ns_all payloads structure relationship
        NS_BASE_TO_DIGEST_MAP: dict[str, str] = {
            mc.NS_APPLIANCE_HUB_ONLINE: mc.KEY_STATUS,
            mc.NS_APPLIANCE_HUB_TOGGLEX: mc.KEY_ONOFF,
        }
        """digest structure common to both sensors and mtss"""
        NS_TO_DIGEST_MAP: dict[str, dict[str, str]] = {
            mc.NS_APPLIANCE_HUB_MTS100_ALL: NS_BASE_TO_DIGEST_MAP
            | {
                mc.NS_APPLIANCE_HUB_MTS100_MODE: "",  # "" here means we're not defaulting to a digest key
                mc.NS_APPLIANCE_HUB_MTS100_TEMPERATURE: "",
            },
            mc.NS_APPLIANCE_HUB_SENSOR_ALL: NS_BASE_TO_DIGEST_MAP,
        }
        """specialization based on subdevice type for digest <-> ns_all relationship"""

        for p_subdevice_digest in digest_subdevices:
            subdevice_id = p_subdevice_digest[mc.KEY_ID]
            # detect first if it0s an mts like or a sensor like
            if p_subdevice_digest[mc.KEY_STATUS] == mc.STATUS_ONLINE:
                p_mts_digest = get_mts_digest(p_subdevice_digest)
                subdevice_ns = (
                    mc.NS_APPLIANCE_HUB_MTS100_ALL
                    if p_mts_digest is not None
                    else mc.NS_APPLIANCE_HUB_SENSOR_ALL
                )
                assert (
                    subdevice_ns in ns_state
                ), f"Hub emulator init: missing {subdevice_ns}"
                p_subdevice_all = get_element_by_key_safe(
                    ns_state[subdevice_ns],
                    mc.KEY_ID,
                    subdevice_id,
                )
            else:
                # the p_mts_digest could be missing from digest
                # when the valve is offline so we'll fallback to inspecting either
                # MTS100_ALL or SENSOR_ALL for clues..
                for subdevice_ns in (
                    mc.NS_APPLIANCE_HUB_MTS100_ALL,
                    mc.NS_APPLIANCE_HUB_SENSOR_ALL,
                ):
                    if subdevice_ns in ns_state:
                        p_subdevice_all = get_element_by_key_safe(
                            ns_state[subdevice_ns],
                            mc.KEY_ID,
                            subdevice_id,
                        )
                        if p_subdevice_all:
                            break
                else:
                    raise Exception(f"Cannot detect type for subdevice {subdevice_id}")

            # subdevice_ns now tells us if its an mts like or a sensor
            # p_subdevice_all already carries the ns_all state (if present in trace)
            if not p_subdevice_all:
                p_subdevice_all = {mc.KEY_ID: subdevice_id}
                ns_state[subdevice_ns].append(p_subdevice_all)

            if subdevice_ns is mc.NS_APPLIANCE_HUB_MTS100_ALL:
                # this subdevice is an mts like so we'll ensure it's
                # 'all' payload (at least) is set. we'll also bind
                # the child dicts in 'all' to teh corresponding specific
                # namespace payload for the subdevice id so that the
                # state is maintained consistent. For instance, the 'temperature' dict
                # in the subdevice ns_all payload is the same as the corresponding
                # payload in Mts100.Temperature

                if mc.KEY_SCHEDULEBMODE in p_subdevice_digest:
                    p_subdevice_all[mc.KEY_SCHEDULEBMODE] = p_subdevice_digest[
                        mc.KEY_SCHEDULEBMODE
                    ]

            for subnamespace, digest_key in NS_TO_DIGEST_MAP[subdevice_ns].items():
                # here we'll link the specific ns_state to a corresponding
                # dict in p_subdevice_all payload. This will also
                # create a default corresponding subdevice ns_state should it be missing
                if subnamespace in ns_state:
                    # (sub)namespace is supported in abilities so we'll fix/setup it
                    key_subnamespace = NAMESPACE_TO_KEY[subnamespace]
                    p_subdevice_substate = get_element_by_key_safe(
                        ns_state[subnamespace],
                        mc.KEY_ID,
                        subdevice_id,
                    )
                    if not p_subdevice_substate:
                        # we don't have the state in the specific ns
                        # so we default it eventually initializing with the digest data
                        p_subdevice_substate = {mc.KEY_ID: subdevice_id}
                        if digest_key in p_subdevice_digest:
                            p_subdevice_substate[digest_key] = p_subdevice_digest[
                                digest_key
                            ]
                        ns_state[subnamespace].append(p_subdevice_substate)
                    p_subdevice_all[key_subnamespace] = p_subdevice_substate

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
            mts_mode = p_mode[mc.KEY_STATE]
            p_subdevice_digest = self._get_subdevice_digest(subdevice_id)
            mts_digest = get_mts_digest(p_subdevice_digest)
            if mts_digest and mc.KEY_MODE in mts_digest:
                mts_digest[mc.KEY_MODE] = mts_mode

            p_subdevice_mode = self._get_subdevice_namespace(
                subdevice_id, mc.NS_APPLIANCE_HUB_MTS100_MODE
            )
            p_subdevice_mode[mc.KEY_STATE] = mts_mode

            if mts_mode in mc.MTS100_MODE_TO_CURRENTSET_MAP:
                p_subdevice_temperature = self._get_subdevice_namespace(
                    subdevice_id, mc.NS_APPLIANCE_HUB_MTS100_TEMPERATURE
                )
                p_subdevice_temperature[mc.KEY_CURRENTSET] = p_subdevice_temperature[
                    mc.MTS100_MODE_TO_CURRENTSET_MAP[mts_mode]
                ]

        return mc.METHOD_SETACK, {}

    def _SET_Appliance_Hub_Mts100_Temperature(self, header, payload):
        response_payload = []
        for p_subdevice in payload[mc.KEY_TEMPERATURE]:
            subdevice_id = p_subdevice[mc.KEY_ID]
            p_subdevice_temperature = self._get_subdevice_namespace(
                subdevice_id, mc.NS_APPLIANCE_HUB_MTS100_TEMPERATURE
            )
            update_dict_strict(p_subdevice_temperature, p_subdevice)

            p_subdevice_mode = self._get_subdevice_namespace(
                subdevice_id, mc.NS_APPLIANCE_HUB_MTS100_MODE
            )
            mts_mode = p_subdevice_mode[mc.KEY_STATE]
            if mts_mode in mc.MTS100_MODE_TO_CURRENTSET_MAP:
                p_subdevice_temperature[mc.KEY_CURRENTSET] = p_subdevice_temperature[
                    mc.MTS100_MODE_TO_CURRENTSET_MAP[mts_mode]
                ]
            response_payload.append(p_subdevice_temperature)

        return mc.METHOD_SETACK, {mc.KEY_TEMPERATURE: response_payload}

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
