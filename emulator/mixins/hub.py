""""""

from random import randint
from typing import TYPE_CHECKING

from custom_components.meross_lan.merossclient import (
    get_element_by_key,
    get_element_by_key_safe,
    get_mts_digest,
    update_dict_strict,
)
from custom_components.meross_lan.merossclient.protocol import (
    const as mc,
    namespaces as mn,
)
from custom_components.meross_lan.merossclient.protocol.namespaces import hub as mn_h

if TYPE_CHECKING:
    from typing import Any

    from .. import MerossEmulator, MerossEmulatorDescriptor


class HubMixin(MerossEmulator if TYPE_CHECKING else object):

    NAMESPACES = mn.HUB_NAMESPACES

    MAXIMUM_RESPONSE_SIZE = 4000

    def __init__(self, descriptor: "MerossEmulatorDescriptor", key):
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
        ns_state: dict[mn.Namespace, list[dict]] = {}

        for ns in (
            mn_h.Appliance_Hub_Mts100_Adjust,
            mn_h.Appliance_Hub_Mts100_All,
            mn_h.Appliance_Hub_Mts100_Mode,
            mn_h.Appliance_Hub_Mts100_ScheduleB,
            mn_h.Appliance_Hub_Mts100_Temperature,
            mn_h.Appliance_Hub_Sensor_Adjust,
            mn_h.Appliance_Hub_Sensor_All,
            mn_h.Appliance_Hub_Sensor_Smoke,
            mn_h.Appliance_Hub_Sensor_DoorWindow,
            mn_h.Appliance_Hub_Online,
            mn_h.Appliance_Hub_ToggleX,
        ):
            if ns.name in ability:
                ns_state[ns] = namespaces.setdefault(ns.name, {ns.key: []})[ns.key]

        # these maps help in generalizing the rules for
        # digest <-> ns_all payloads structure relationship
        NS_BASE_TO_DIGEST_MAP: dict[mn.Namespace, str] = {
            mn_h.Appliance_Hub_Online: mc.KEY_STATUS,
            mn_h.Appliance_Hub_ToggleX: mc.KEY_ONOFF,
        }
        """digest structure common to both sensors and mtss"""
        NS_TO_DIGEST_MAP: dict[mn.Namespace, dict[mn.Namespace, str]] = {
            mn_h.Appliance_Hub_Mts100_All: NS_BASE_TO_DIGEST_MAP
            | {
                mn_h.Appliance_Hub_Mts100_Mode: "",  # "" here means we're not defaulting to a digest key
                mn_h.Appliance_Hub_Mts100_Temperature: "",
            },
            mn_h.Appliance_Hub_Sensor_All: NS_BASE_TO_DIGEST_MAP,
        }
        """specialization based on subdevice type for digest <-> ns_all relationship"""

        for p_subdevice_digest in digest_subdevices:
            subdevice_id = p_subdevice_digest[mc.KEY_ID]
            # detect first if it's an mts like or a sensor like
            if p_subdevice_digest[mc.KEY_STATUS] == mc.STATUS_ONLINE:
                p_mts_digest = get_mts_digest(p_subdevice_digest)
                subdevice_ns = (
                    mn_h.Appliance_Hub_Mts100_All
                    if p_mts_digest is not None
                    else mn_h.Appliance_Hub_Sensor_All
                )
                assert (
                    subdevice_ns in ns_state
                ), f"Hub emulator init: missing {subdevice_ns.name}"
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
                    mn_h.Appliance_Hub_Mts100_All,
                    mn_h.Appliance_Hub_Sensor_All,
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

            if subdevice_ns is mn_h.Appliance_Hub_Mts100_All:
                # this subdevice is an mts like so we'll ensure its
                # 'all' payload (at least) is set. we'll also bind
                # the child dicts in 'all' to the corresponding specific
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
                    p_subdevice_all[subnamespace.key] = p_subdevice_substate

    def _get_subdevice_digest(self, subdevice_id: str):
        """returns the subdevice dict from the hub digest key"""
        return get_element_by_key(
            self.descriptor.digest[mc.KEY_HUB][mc.KEY_SUBDEVICE],
            mc.KEY_ID,
            subdevice_id,
        )

    def _get_subdevice_namespace(
        self, subdevice_id: str, ns: mn.Namespace, *, force_create: bool = True
    ) -> "dict[str, Any]":
        """returns the subdevice namespace dict. It will create a default entry if not present
        and the device abilities supports the namespace."""
        try:
            subdevices_namespace: list = self.descriptor.namespaces[ns.name][ns.key]
            try:
                return get_element_by_key(
                    subdevices_namespace,
                    mc.KEY_ID,
                    subdevice_id,
                )
            except KeyError:
                if not force_create:
                    raise
                p_subdevice = {mc.KEY_ID: subdevice_id}
                subdevices_namespace.append(p_subdevice)
        except KeyError:
            if not force_create:
                raise
            assert (
                ns.name in self.descriptor.ability
            ), f"{ns.name} not available in Hub abilities"
            p_subdevice = {mc.KEY_ID: subdevice_id}
            self.descriptor.namespaces[ns.name] = {ns.key: [p_subdevice]}
        return p_subdevice

    def _get_mts100_all(self, subdevice_id: str, *, force_create: bool = True):
        return self._get_subdevice_namespace(
            subdevice_id, mn_h.Appliance_Hub_Mts100_All, force_create=force_create
        )

    def _get_sensor_all(self, subdevice_id: str, *, force_create: bool = True):
        return self._get_subdevice_namespace(
            subdevice_id, mn_h.Appliance_Hub_Sensor_All, force_create=force_create
        )

    def _get_subdevice_all(self, subdevice_id: str):
        """returns the subdevice 'all' dict from either the Hub.Sensor.All or Hub.Mts100.All"""
        try:
            return self._get_mts100_all(subdevice_id, force_create=False)
        except KeyError:
            # this is a sensor like subdevice
            # so we'll try to get the sensor all
            return self._get_sensor_all(subdevice_id, force_create=False)

    def _handler_default(self, method: str, namespace: str, payload: dict):
        if method == mc.METHOD_GET:
            ns = self.NAMESPACES[namespace]
            if ns.is_hub_namespace:
                ns_key = ns.key
                ns_key_channel = ns.key_channel
                response_payload = self.descriptor.namespaces[namespace]
                request_subdevices = payload[ns_key]
                if request_subdevices:
                    # client asked for defined set of ids
                    current_subdevices = response_payload[ns_key]
                    response_subdevices = []
                    for p_subdevice_id in request_subdevices:
                        if p_subdevice := get_element_by_key_safe(
                            current_subdevices,
                            ns_key_channel,
                            p_subdevice_id[ns_key_channel],
                        ):
                            response_subdevices.append(p_subdevice)
                    response_payload = {ns_key: response_subdevices}
                else:
                    # client request empty list -> device responds with full set
                    response_subdevices = response_payload[ns_key]

                # response_subdevices contains the list of subdevices state being returned.
                # we'll apply some randomization to the state to emulate signals
                for p_subdevice in response_subdevices:
                    if mc.KEY_DOORWINDOW in p_subdevice:
                        if randint(0, 4) == 0:
                            p_subdevice[mc.KEY_DOORWINDOW][mc.KEY_STATUS] = 1
                        else:
                            p_subdevice[mc.KEY_DOORWINDOW][mc.KEY_STATUS] = 0
                    elif mc.KEY_SMOKEALARM in p_subdevice:
                        a = randint(0, 2)
                        if a == 0:
                            p_subdevice[mc.KEY_SMOKEALARM][mc.KEY_STATUS] = randint(
                                17, 27
                            )
                        elif a == 1:
                            p_subdevice[mc.KEY_SMOKEALARM][mc.KEY_STATUS] = 170

                return mc.METHOD_GETACK, response_payload

        return super()._handler_default(method, namespace, payload)

    def _SET_Appliance_Hub_Mts100_Adjust(self, header, payload):
        for p_subdevice in payload[mc.KEY_ADJUST]:
            subdevice_id = p_subdevice[mc.KEY_ID]
            p_subdevice_adjust = self._get_subdevice_namespace(
                subdevice_id, mn_h.Appliance_Hub_Mts100_Adjust
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
                subdevice_id, mn_h.Appliance_Hub_Mts100_Mode
            )
            p_subdevice_mode[mc.KEY_STATE] = mts_mode

            if mts_mode in mc.MTS100_MODE_TO_CURRENTSET_MAP:
                p_subdevice_temperature = self._get_subdevice_namespace(
                    subdevice_id, mn_h.Appliance_Hub_Mts100_Temperature
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
                subdevice_id, mn_h.Appliance_Hub_Mts100_Temperature
            )
            update_dict_strict(p_subdevice_temperature, p_subdevice)

            p_subdevice_mode = self._get_subdevice_namespace(
                subdevice_id, mn_h.Appliance_Hub_Mts100_Mode
            )
            mts_mode = p_subdevice_mode[mc.KEY_STATE]
            if mts_mode in mc.MTS100_MODE_TO_CURRENTSET_MAP:
                p_subdevice_temperature[mc.KEY_CURRENTSET] = p_subdevice_temperature[
                    mc.MTS100_MODE_TO_CURRENTSET_MAP[mts_mode]
                ]
            response_payload.append(p_subdevice_temperature)

        return mc.METHOD_SETACK, {mc.KEY_TEMPERATURE: response_payload}

    def _SET_Appliance_Hub_Sensor_Adjust(self, header, payload):
        for p_subdevice in payload[mc.KEY_ADJUST]:
            subdevice_id = p_subdevice[mc.KEY_ID]
            p_subdevice_adjust = self._get_subdevice_namespace(
                subdevice_id, mn_h.Appliance_Hub_Sensor_Adjust
            )
            if mc.KEY_HUMIDITY in p_subdevice:
                p_subdevice_adjust[mc.KEY_HUMIDITY] = (
                    p_subdevice_adjust.get(mc.KEY_HUMIDITY, 0)
                    + p_subdevice[mc.KEY_HUMIDITY]
                )
            if mc.KEY_TEMPERATURE in p_subdevice:
                p_subdevice_adjust[mc.KEY_TEMPERATURE] = (
                    p_subdevice_adjust.get(mc.KEY_TEMPERATURE, 0)
                    + p_subdevice[mc.KEY_TEMPERATURE]
                )

        return mc.METHOD_SETACK, {}

    def _SET_Appliance_Hub_Sensor_Smoke(self, header, payload):
        for p_subdevice_request in payload[mc.KEY_SMOKEALARM]:
            subdevice_id = p_subdevice_request[mc.KEY_ID]
            if mc.KEY_INTERCONN in p_subdevice_request:
                p_subdevice_smoke = self._get_subdevice_namespace(
                    subdevice_id, mn_h.Appliance_Hub_Sensor_Smoke
                )
                p_subdevice_smoke[mc.KEY_INTERCONN] = p_subdevice_request[
                    mc.KEY_INTERCONN
                ]
                p_subdevice_all = self._get_sensor_all(subdevice_id)
                if mc.KEY_SMOKEALARM in p_subdevice_all:
                    p_subdevice_all[mc.KEY_SMOKEALARM][mc.KEY_INTERCONN] = (
                        p_subdevice_request[mc.KEY_INTERCONN]
                    )

        return mc.METHOD_SETACK, {}

    def _SET_Appliance_Hub_ToggleX(self, header, payload):
        for p_togglex in payload[mc.KEY_TOGGLEX]:
            subdevice_id = p_togglex[mc.KEY_ID]
            p_subdevice_digest = self._get_subdevice_digest(subdevice_id)
            if mc.KEY_ONOFF in p_subdevice_digest:
                p_subdevice_digest[mc.KEY_ONOFF] = p_togglex[mc.KEY_ONOFF]

            p_subdevice_all = self._get_subdevice_all(subdevice_id)
            if mc.KEY_TOGGLEX in p_subdevice_all:
                # beware the "onoff" key in "all" is a embedded in the "togglex" dict
                p_subdevice_all[mc.KEY_TOGGLEX][mc.KEY_ONOFF] = p_togglex[mc.KEY_ONOFF]

            p_subdevice_togglex = self._get_subdevice_namespace(
                subdevice_id, mn_h.Appliance_Hub_ToggleX
            )
            p_subdevice_togglex[mc.KEY_ONOFF] = p_togglex[mc.KEY_ONOFF]

        return mc.METHOD_SETACK, {}
