""""""

import asyncio
from random import randint
import typing

from custom_components.meross_lan.merossclient import (
    const as mc,
    get_element_by_key,
    update_dict_strict,
    update_dict_strict_by_key,
)

if typing.TYPE_CHECKING:
    from .. import MerossEmulator


class GarageDoorMixin(MerossEmulator if typing.TYPE_CHECKING else object):

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
                    mc.NS_APPLIANCE_GARAGEDOOR_STATE,
                    {
                        "state": [{"channel": 0, "open": 1, "lmTime": 0}],
                        "reason": {"online": {"timestamp": self.epoch}},
                    },
                )

    def _SET_Appliance_GarageDoor_Config(self, header, payload):
        p_config = self.descriptor.namespaces[mc.NS_APPLIANCE_GARAGEDOOR_CONFIG][
            mc.KEY_CONFIG
        ]
        update_dict_strict(p_config, payload[mc.KEY_CONFIG])
        return mc.METHOD_SETACK, {}

    def _SET_Appliance_GarageDoor_MultipleConfig(self, header, payload):
        p_config: list = self.descriptor.namespaces[
            mc.NS_APPLIANCE_GARAGEDOOR_MULTIPLECONFIG
        ][mc.KEY_CONFIG]
        p_state: list = self.descriptor.digest[mc.KEY_GARAGEDOOR]
        for p_payload_channel in payload[mc.KEY_CONFIG]:
            """{"channel":3,"doorEnable":0,"timestamp":1699130748,"timestampMs":663,"signalClose":10000,"signalOpen":10000,"buzzerEnable":1}"""
            p_config_channel = update_dict_strict_by_key(p_config, p_payload_channel)
            p_config_channel[mc.KEY_TIMESTAMP] = self.epoch
            p_state_channel = get_element_by_key(
                p_state, mc.KEY_CHANNEL, p_payload_channel[mc.KEY_CHANNEL]
            )
            if (mc.KEY_DOORENABLE in p_state_channel) and (
                mc.KEY_DOORENABLE in p_payload_channel
            ):
                p_state_channel[mc.KEY_DOORENABLE] = p_payload_channel[
                    mc.KEY_DOORENABLE
                ]

        return mc.METHOD_SETACK, {}

    def _GET_Appliance_GarageDoor_State(self, header, payload):
        # return everything...at the moment we always query all
        p_garageDoor: list = self.descriptor.digest[mc.KEY_GARAGEDOOR]
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
        p_request = payload[mc.KEY_STATE]
        request_channel = p_request[mc.KEY_CHANNEL]
        request_open = p_request[mc.KEY_OPEN]

        p_state_channel = get_element_by_key(
            self.descriptor.digest[mc.KEY_GARAGEDOOR], mc.KEY_CHANNEL, request_channel
        )

        p_response = dict(p_state_channel)
        if request_open != p_state_channel[mc.KEY_OPEN]:

            def _state_update_callback():
                p_state_channel[mc.KEY_OPEN] = request_open

            asyncio.get_event_loop().call_later(
                self.OPENDURATION if request_open else self.CLOSEDURATION,
                _state_update_callback,
            )

        p_response[mc.KEY_EXECUTE] = 1
        return mc.METHOD_SETACK, {mc.KEY_STATE: p_response}
