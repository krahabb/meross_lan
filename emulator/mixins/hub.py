""""""

from random import randint
from typing import TYPE_CHECKING

from custom_components.meross_lan.merossclient import (
    extract_dict_payloads,
    get_element_by_key,
    update_dict_strict,
)
from custom_components.meross_lan.merossclient.protocol import (
    const as mc,
    namespaces as mn,
)
from custom_components.meross_lan.merossclient.protocol.namespaces import hub as mn_h

from . import Emulator

if TYPE_CHECKING:
    from typing import Any

    from custom_components.meross_lan.merossclient.protocol import types as mt

    from . import EmulatorDescriptor


class HubMixin(Emulator if TYPE_CHECKING else object):

    MAXIMUM_RESPONSE_SIZE = 4000

    NAMESPACES_DEFAULT = {
        mn.Appliance_Config_Alarm: (
            Emulator.NSDefaultMode.MixOut,
            {mc.KEY_CHANNEL: 0, mc.KEY_ENABLE: 1, mc.KEY_VOLUME: 100, mc.KEY_SONG: 1},
        ),
        mn.Appliance_Control_Alarm: (
            Emulator.NSDefaultMode.MixOut,
            {mc.KEY_CHANNEL: 0, "event": {"security": {"value": 1}}},
        ),
    }

    # This is an initialization map for those namespaces actually implemented only by specific subdevices types
    # This will be used at runtime together with DIGEST_SUBID_NAMESPACES_MAP to setup only those defaults related
    # to subdevices type actually appearing in the Hub digest.
    # If the default definition carries a subid, then it will be applied only to the matching subdevice id,
    # otherwise (no subid in default) it will be applied to all subdevices of the matching type.
    SUBID_NAMESPACES_DEFAULT: "Emulator.NSDefault" = {
        mn.Appliance_Config_Alarm: (
            Emulator.NSDefaultMode.MixOut,
            [
                {  # gs559 mocked cfg: not sure this ns supports this
                    # the subId here is in order to not install this patch
                    mc.KEY_SUBID: "",
                    mc.KEY_CHANNEL: 0,
                    "enable": 1,
                    "volume": 100,
                    "song": 1,
                },
            ],
        ),
        mn.Appliance_Config_DeviceCfg: (
            Emulator.NSDefaultMode.MixOut,
            [
                {  # mst100 mocked cfg
                    mc.KEY_SUBID: "_1B00839E9A6D",
                    mc.KEY_CHANNEL: 0,
                    "mstCfg": {
                        "dura": 60,
                        "wfm": 1,
                        "calibration": {"waCon": 0, "onoff": 0, "lmTime": 0},
                    },
                },
                {  # mst200 mocked cfg
                    mc.KEY_SUBID: "_1B1091AFCF10",
                    mc.KEY_CHANNEL: 1,
                    "mstCfg": {
                        "dura": 60,
                        "wfm": 1,
                        "calibration": {"waCon": 0, "onoff": 0, "lmTime": 0},
                    },
                },
                {  # mst200 mocked cfg
                    mc.KEY_SUBID: "_1B1091AFCF10",
                    mc.KEY_CHANNEL: 2,
                    "mstCfg": {
                        "dura": 60,
                        "wfm": 1,
                        "calibration": {"waCon": 0, "onoff": 0, "lmTime": 0},
                    },
                },
            ],
        ),
        mn_h.Appliance_Hub_SubDevice_Beep: (
            Emulator.NSDefaultMode.MixOut,
            {mc.KEY_ONOFF: 0},
        ),
    }

    DIGEST_SUBID_NAMESPACES_MAP: dict[str, tuple[mn.Namespace, ...]] = {
        # this is a map subdevice type (as per digest) to the namespaces it supports
        mc.KEY_DOORWINDOW: (mn_h.Appliance_Hub_SubDevice_Beep,),
        mc.KEY_MST: (
            mn.Appliance_Config_DeviceCfg,
            mn_h.Appliance_Control_Water,
        ),
        mc.TYPE_MTS150: (mn_h.Appliance_Hub_SubDevice_Beep,),
        mc.KEY_SMOKEALARM: (mn.Appliance_Config_Alarm, mn.Appliance_Control_Alarm),
        mc.KEY_WATERLEAK: (mn_h.Appliance_Hub_SubDevice_Beep,),
    }

    def __init__(self, descriptor: "EmulatorDescriptor", key):
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
        subdevices = descriptor.subdevices
        namespaces = descriptor.namespaces
        ability = descriptor.ability

        ns_state: dict[mn.Namespace, mt.JsonList] = {
            ns: namespaces[ns].setdefault(ns.key, [])
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
                mn_h.Appliance_Hub_Sensor_WaterLeak,
                mn_h.Appliance_Hub_Battery,
                mn_h.Appliance_Hub_Online,
                mn_h.Appliance_Hub_ToggleX,
                # TODO: move the 2 next to SUBID_NAMESPACES_DEFAULT
                # since they are only relevant for specific subdevices types
                mn.Appliance_Control_Sensor_HistoryX,
                mn.Appliance_Control_Sensor_LatestX,
            )
            if ns in ability
        }

        # these maps help in generalizing the rules for
        # digest <-> ns_all payloads structure relationship
        NS_BASE_TO_DIGEST_MAP: dict[mn.Namespace, str] = {
            mn_h.Appliance_Hub_Online: mc.KEY_STATUS,
        }
        """digest structure common to both sensors and mtss"""
        NS_TO_DIGEST_MAP: dict[mn.Namespace, dict[mn.Namespace, str]] = {
            mn_h.Appliance_Hub_Mts100_All: NS_BASE_TO_DIGEST_MAP
            | {
                mn_h.Appliance_Hub_ToggleX: mc.KEY_ONOFF,
                mn_h.Appliance_Hub_Mts100_Mode: "",  # "" here means we're not defaulting to a digest key
                mn_h.Appliance_Hub_Mts100_Temperature: "",
            },
            mn_h.Appliance_Hub_Sensor_All: NS_BASE_TO_DIGEST_MAP,
        }
        """specialization based on subdevice type for digest <-> ns_all relationship"""

        # DEBUG/TESTING feature: remove a subdevice from hub definitions
        """
        if subdevice_id_remove := "28004811B776":
            delete_element_by_key(subdevices, mc.KEY_ID, subdevice_id_remove)
            for _, _ns_state in ns_state.items():
                delete_element_by_key(_ns_state, mc.KEY_ID, subdevice_id_remove)
        """

        p_subdevice_all: "mt.JsonDict"
        for subdevice in subdevices:
            subdevice_id = subdevice.id
            key_digest = subdevice.key_digest

            try:
                for subid_ns in self.DIGEST_SUBID_NAMESPACES_MAP[key_digest]:
                    if subid_ns not in ability:
                        continue
                    mixmode, payload = self.SUBID_NAMESPACES_DEFAULT[subid_ns]
                    # get the key name used to map the id/subid in the namespace index.
                    subid_ns_key = subid_ns.index_type.key[0]
                    assert subid_ns_key in (
                        mc.KEY_ID,
                        mc.KEY_SUBID,
                    ), f"Namespace {subid_ns} index type not supported for subid mapping"
                    for payload in extract_dict_payloads(payload):
                        try:
                            if payload[subid_ns_key] != subdevice_id:
                                continue
                            # Our payload default has a specific subdev binding and this is matching
                            self.update_namespace_state(subid_ns, mixmode, payload)
                        except KeyError:
                            # Our payload default doesn't have a specific subid instance binding
                            # so we use it whatever subid instance we're processing.
                            self.update_namespace_state(
                                subid_ns,
                                mixmode,
                                {subid_ns_key: subdevice_id} | payload,
                            )

            except KeyError:
                pass

            # detect first if it's an mts like or a sensor like
            for ns_all in (
                mn_h.Appliance_Hub_Mts100_All,
                mn_h.Appliance_Hub_Sensor_All,
            ):
                if ns_all in ns_state:
                    try:
                        p_subdevice_all = get_element_by_key(
                            ns_state[ns_all], subdevice_id, mc.KEY_ID
                        )
                        break
                    except KeyError:
                        continue
            else:
                if key_digest.startswith(mc.TYPE_MTS):
                    ns_all = mn_h.Appliance_Hub_Mts100_All
                    p_subdevice_all = {mc.KEY_ID: subdevice_id}
                    ns_state[ns_all].append(p_subdevice_all)
                else:
                    ns_all = None

            if not ns_all:
                # We cannot determine if it is a sensor or an mts like subdevice.
                match key_digest:
                    case mc.KEY_MST:
                        # mst devices have 'funny' syntax
                        if "waDet" in subdevice.digest[mc.KEY_MST]:  # type: ignore
                            # mst200
                            channels = (1, 2)
                            self.update_namespace_state(
                                mn_h.Appliance_Control_Water,
                                Emulator.NSDefaultMode.MixOut,
                                [
                                    {
                                        mc.KEY_SUBID: subdevice_id,
                                        mc.KEY_CHANNELS: [channel],
                                        mc.KEY_ONOFF: 2,
                                        "dura": 7200,
                                        "lmTime": 0,
                                    }
                                    for channel in channels
                                ],
                            )
                        else:
                            # mst100
                            channels = (0,)
                            self.update_namespace_state(
                                mn_h.Appliance_Control_Water,
                                Emulator.NSDefaultMode.MixOut,
                                {
                                    mc.KEY_SUBID: subdevice_id,
                                    mc.KEY_ONOFF: 2,
                                    "dura": 7200,
                                    "lmTime": 0,
                                },
                            )
                        self.update_namespace_state(
                            mn.Appliance_Config_DeviceCfg,
                            Emulator.NSDefaultMode.MixOut,
                            [
                                {
                                    mc.KEY_SUBID: subdevice_id,
                                    mc.KEY_CHANNEL: channel,
                                    "mstCfg": {
                                        "dura": 60,
                                        "wfm": 1,
                                        "calibration": {
                                            "waCon": 0,
                                            "onoff": 0,
                                            "lmTime": 0,
                                        },
                                    },
                                }
                                for channel in channels
                            ],
                        )
                continue

            # ns_all now tells us if its an mts like or a sensor
            # p_subdevice_all already carries the ns_all state (if present in trace)
            if ns_all is mn_h.Appliance_Hub_Mts100_All:
                # this subdevice is an mts like so we'll ensure its
                # 'all' payload (at least) is set. we'll also bind
                # the child dicts in 'all' to the corresponding specific
                # namespace payload for the subdevice id so that the
                # state is maintained consistent. For instance, the 'temperature' dict
                # in the subdevice ns_all payload is the same as the corresponding
                # payload in Mts100.Temperature
                if mc.KEY_SCHEDULEBMODE in subdevice.digest:
                    p_subdevice_all[mc.KEY_SCHEDULEBMODE] = subdevice.digest[
                        mc.KEY_SCHEDULEBMODE
                    ]
            for subnamespace, digest_key in NS_TO_DIGEST_MAP[ns_all].items():
                # here we'll link the specific ns_state to a corresponding
                # dict in p_subdevice_all payload. This will also
                # create a default corresponding subdevice ns_state should it be missing
                if subnamespace in ns_state:
                    # (sub)namespace is supported in abilities so we'll fix/setup it
                    try:
                        p_subdevice_substate = get_element_by_key(
                            ns_state[subnamespace], subdevice_id, mc.KEY_ID
                        )
                    except KeyError:
                        # we don't have the state in the specific ns
                        # so we default it eventually initializing with the digest data
                        p_subdevice_substate = {mc.KEY_ID: subdevice_id}
                        if digest_key in subdevice.digest:
                            p_subdevice_substate[digest_key] = subdevice.digest[
                                digest_key
                            ]
                        ns_state[subnamespace].append(p_subdevice_substate)
                    p_subdevice_all[subnamespace.key] = p_subdevice_substate

    def _scheduler(self):
        super()._scheduler()
        for subdevice in self.descriptor.subdevices:
            # we randomly change the status of subdevices to emulate
            # motion/smoke/doorwindow triggers
            digest = subdevice.digest
            if mc.KEY_DOORWINDOW in digest:
                if randint(0, 4) == 0:
                    digest[mc.KEY_DOORWINDOW][mc.KEY_STATUS] = 1
                else:
                    digest[mc.KEY_DOORWINDOW][mc.KEY_STATUS] = 0
            elif mc.KEY_SMOKEALARM in digest:
                a = randint(0, 2)
                if a == 0:
                    digest[mc.KEY_SMOKEALARM][mc.KEY_STATUS] = randint(17, 27)
                elif a == 1:
                    digest[mc.KEY_SMOKEALARM][mc.KEY_STATUS] = 170
            # TODO: add randomization for other subdevices payloads

    def _get_subdevice_namespace(
        self, subdevice_id: str, ns: mn.Namespace, *, force_create: bool = True
    ) -> "dict[str, Any]":
        """returns the subdevice namespace dict. It will create a default entry if not present
        and the device abilities supports the namespace."""
        assert (
            ns.index_type is mn.IndexType.id
        ), f"Namespace {ns} is not indexed by 'id'"
        try:
            subdevices_namespace: list = self.namespaces[ns][ns.key]
            try:
                return get_element_by_key(subdevices_namespace, subdevice_id, mc.KEY_ID)
            except KeyError:
                if not force_create:
                    raise
                p_subdevice = {mc.KEY_ID: subdevice_id}
                subdevices_namespace.append(p_subdevice)
        except KeyError:
            if not force_create:
                raise
            assert ns in self.descriptor.ability, f"{ns} not available in Hub abilities"
            p_subdevice = {mc.KEY_ID: subdevice_id}
            self.namespaces[ns] = {ns.key: [p_subdevice]}
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
            subdevice = self.descriptor.get_subdevice(subdevice_id)
            mts_digest = subdevice.digest[subdevice.key_digest]
            if mc.KEY_MODE in mts_digest:
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
            subdevice = self.descriptor.get_subdevice(subdevice_id)
            if mc.KEY_ONOFF in subdevice.digest:
                subdevice.digest[mc.KEY_ONOFF] = p_togglex[mc.KEY_ONOFF]
            p_subdevice_all = self._get_subdevice_all(subdevice_id)
            if mc.KEY_TOGGLEX in p_subdevice_all:
                # beware the "onoff" key in "all" is a embedded in the "togglex" dict
                p_subdevice_all[mc.KEY_TOGGLEX][mc.KEY_ONOFF] = p_togglex[mc.KEY_ONOFF]

            p_subdevice_togglex = self._get_subdevice_namespace(
                subdevice_id, mn_h.Appliance_Hub_ToggleX
            )
            p_subdevice_togglex[mc.KEY_ONOFF] = p_togglex[mc.KEY_ONOFF]

        return mc.METHOD_SETACK, {}
