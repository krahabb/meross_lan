""""""

import asyncio
from random import randint
from typing import TYPE_CHECKING

from custom_components.meross_lan.merossclient import (
    get_element_by_key,
    update_dict_strict,
    update_dict_strict_by_index,
)
from custom_components.meross_lan.merossclient.protocol import (
    const as mc,
    namespaces as mn,
)

if TYPE_CHECKING:
    from custom_components.meross_lan.merossclient.protocol import types as mt

    from . import Emulator


class GarageDoorMixin(Emulator if TYPE_CHECKING else object):

    OPENDURATION = 2
    CLOSEDURATION = 10

    def _scheduler(self):
        super()._scheduler()
        if self.mqtt_connected:
            # emulate the 'glitch' caused by msg200 pushing state for channel 0
            # see #428
            p_garageDoor: list = self.descriptor.digest[mc.KEY_GARAGEDOOR]
            if len(p_garageDoor) == 3:
                self.mqtt_publish_push(
                    mn.Appliance_GarageDoor_State,
                    {
                        "state": [{"channel": 0, "open": 1, "lmTime": 0}],
                        "reason": {"online": {"timestamp": self.epoch}},
                    },
                )

    def _SET_Appliance_GarageDoor_Config(self, header, payload):
        p_config = self.namespaces[mn.Appliance_GarageDoor_Config][mc.KEY_CONFIG]
        update_dict_strict(p_config, payload[mc.KEY_CONFIG])
        return mc.METHOD_SETACK, {}

    def _SET_Appliance_GarageDoor_MultipleConfig(self, header, payload):
        p_config: list = self.namespaces[mn.Appliance_GarageDoor_MultipleConfig][
            mc.KEY_CONFIG
        ]
        p_state = self.descriptor.digest[mc.KEY_GARAGEDOOR]
        for p_channel_payload in payload[mc.KEY_CONFIG]:
            """{"channel":3,"doorEnable":0,"timestamp":1699130748,"timestampMs":663,"signalClose":10000,"signalOpen":10000,"buzzerEnable":1}"""
            p_channel_config = update_dict_strict_by_index(p_config, p_channel_payload)
            p_channel_config[mc.KEY_TIMESTAMP] = self.epoch
            p_channel_state = get_element_by_key(p_state, p_channel_payload)
            if (mc.KEY_DOORENABLE in p_channel_state) and (
                mc.KEY_DOORENABLE in p_channel_payload
            ):
                p_channel_state[mc.KEY_DOORENABLE] = p_channel_payload[
                    mc.KEY_DOORENABLE
                ]

        return mc.METHOD_SETACK, {}

    def _GET_Appliance_GarageDoor_State(self, header, payload):
        # return everything...at the moment we always query all
        p_garageDoor = self.descriptor.digest[mc.KEY_GARAGEDOOR]
        if len(p_garageDoor) == 1:
            # for msg100 we had, historically, just dict payloads
            # in this ns but now it appears as though some devices/queries
            # might return a list (#439). We'll introduce this randomness
            # here to test if meross_lan is able to manage both.
            if randint(0, 1) == 0:
                return mc.METHOD_GETACK, {mc.KEY_STATE: p_garageDoor[0]}
            else:
                return mc.METHOD_GETACK, {mc.KEY_STATE: p_garageDoor}
        else:
            return mc.METHOD_GETACK, {mc.KEY_STATE: p_garageDoor}

    def _SET_Appliance_GarageDoor_State(self, header, payload):
        p_channel_payload: "mt.garagedoor.State" = payload[mc.KEY_STATE]
        p_channel_state = get_element_by_key(
            self.descriptor.digest[mc.KEY_GARAGEDOOR], p_channel_payload
        )
        p_response = dict(p_channel_state)
        request_open = p_channel_payload[mc.KEY_OPEN]
        if request_open != p_channel_state[mc.KEY_OPEN]:

            def _state_update_callback():
                p_channel_state[mc.KEY_OPEN] = request_open

            asyncio.get_event_loop().call_later(
                self.OPENDURATION if request_open else self.CLOSEDURATION,
                _state_update_callback,
            )
        p_response[mc.KEY_EXECUTE] = 1
        return mc.METHOD_SETACK, {mc.KEY_STATE: p_response}
