""""""

from __future__ import annotations

import asyncio
from time import time
import typing

from custom_components.meross_lan.merossclient import const as mc, get_element_by_key
from emulator.mixins import MerossEmulatorDescriptor

if typing.TYPE_CHECKING:
    from .. import MerossEmulator


class RollerShutterMixin(MerossEmulator if typing.TYPE_CHECKING else object):

    # TODO: implement behavior for legacy devices without native position
    NATIVE_POSITION = True
    OPENDURATION = 20
    CLOSEDURATION = 20

    def __init__(self, descriptor: MerossEmulatorDescriptor, key: str):
        self._transition_unsub = None
        super().__init__(descriptor, key)
        p_position = self._get_namespace_state(
            mc.NS_APPLIANCE_ROLLERSHUTTER_POSITION, 0
        )
        p_position[mc.KEY_POSITION] = mc.ROLLERSHUTTER_POSITION_CLOSED
        p_state = self._get_namespace_state(mc.NS_APPLIANCE_ROLLERSHUTTER_STATE, 0)
        p_state[mc.KEY_STATE] = mc.ROLLERSHUTTER_STATE_IDLE

    def shutdown(self):
        self._transition_cancel()
        super().shutdown()

    def _transition_cancel(self):
        if self._transition_unsub:
            self._transition_unsub.cancel()
            self._transition_unsub = None

    def _SET_Appliance_RollerShutter_Position(self, header, payload):
        """payload = { "postion": {"channel": 0, "position": 100}}"""
        p_request = payload[mc.KEY_POSITION]
        channel = p_request[mc.KEY_CHANNEL]
        position_end = int(p_request[mc.KEY_POSITION])

        p_state = self._get_namespace_state(
            mc.NS_APPLIANCE_ROLLERSHUTTER_STATE, channel
        )
        p_state[mc.KEY_STATE] = mc.ROLLERSHUTTER_STATE_IDLE
        self._transition_cancel()

        if position_end == mc.ROLLERSHUTTER_POSITION_STOP:
            return mc.METHOD_SETACK, {}

        p_position = self._get_namespace_state(
            mc.NS_APPLIANCE_ROLLERSHUTTER_POSITION, channel
        )
        position_begin = p_position[mc.KEY_POSITION]
        if position_end == position_begin:
            return mc.METHOD_SETACK, {}

        if position_end > position_begin:
            if position_end > mc.ROLLERSHUTTER_POSITION_OPENED:
                position_end = mc.ROLLERSHUTTER_POSITION_OPENED
            p_state[mc.KEY_STATE] = mc.ROLLERSHUTTER_STATE_OPENING
            transition_duration = (
                (position_end - position_begin)
                * self.OPENDURATION
                / (mc.ROLLERSHUTTER_POSITION_OPENED - mc.ROLLERSHUTTER_POSITION_CLOSED)
            )
        else:
            if position_end < mc.ROLLERSHUTTER_POSITION_CLOSED:
                position_end = mc.ROLLERSHUTTER_POSITION_CLOSED
            p_state[mc.KEY_STATE] = mc.ROLLERSHUTTER_STATE_CLOSING
            transition_duration = (
                (position_begin - position_end)
                * self.CLOSEDURATION
                / (mc.ROLLERSHUTTER_POSITION_OPENED - mc.ROLLERSHUTTER_POSITION_CLOSED)
            )

        speed = (position_end - position_begin) / transition_duration
        time_begin = time()
        def _transition_callback():
            time_delta = time() - time_begin
            if time_delta >= transition_duration:
                self._transition_unsub = None
                p_state[mc.KEY_STATE] = mc.ROLLERSHUTTER_STATE_IDLE
                p_position[mc.KEY_POSITION] = position_end
                return
            p_position[mc.KEY_POSITION] = position_begin + int(speed * time_delta)
            self._transition_unsub = loop.call_later(1, _transition_callback)

        loop = asyncio.get_event_loop()
        self._transition_unsub = loop.call_later(1, _transition_callback)

        return mc.METHOD_SETACK, {}
