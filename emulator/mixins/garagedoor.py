""""""
from __future__ import annotations

import asyncio
import typing

from .. import MerossEmulator
from custom_components.meross_lan.merossclient import (
    const as mc,
    get_element_by_key,
)


class GarageDoorMixin(MerossEmulator if typing.TYPE_CHECKING else object):
    def _SET_Appliance_GarageDoor_Config(self, header, payload):
        p_config = self.descriptor.namespaces[mc.NS_APPLIANCE_GARAGEDOOR_CONFIG][
            mc.KEY_CONFIG
        ]
        p_request = payload[mc.KEY_CONFIG]
        for _key, _value in p_request.items():
            if _key in p_config:
                p_config[_key] = _value
        return mc.METHOD_SETACK, {}

    def _GET_Appliance_GarageDoor_State(self, header, payload):
        # return everything...at the moment we always query all
        p_garageDoor: list = self.descriptor.digest[mc.KEY_GARAGEDOOR]
        if len(p_garageDoor) == 1:
            # un-pack the list since real traces show no list
            # in this response payloads (we only have msg100 so far..)
            return mc.METHOD_GETACK, {mc.KEY_STATE: p_garageDoor[0]}
        else:
            return mc.METHOD_GETACK, {mc.KEY_STATE: p_garageDoor}

    def _SET_Appliance_GarageDoor_State(self, header, payload):
        p_request = payload[mc.KEY_STATE]
        request_channel = p_request[mc.KEY_CHANNEL]
        request_open = p_request[mc.KEY_OPEN]
        p_digest = self.descriptor.digest

        p_state = get_element_by_key(
            p_digest[mc.KEY_GARAGEDOOR], mc.KEY_CHANNEL, request_channel
        )

        p_response = dict(p_state)
        if request_open != p_state[mc.KEY_OPEN]:

            def _state_update_callback():
                p_state[mc.KEY_OPEN] = request_open

            loop = asyncio.get_event_loop()
            loop.call_later(2 if request_open else 10, _state_update_callback)

        p_response[mc.KEY_EXECUTE] = 1
        return mc.METHOD_SETACK, {mc.KEY_STATE: p_response}
