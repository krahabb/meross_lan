""""""

from __future__ import annotations

import asyncio
from time import time
import typing

from custom_components.meross_lan.merossclient import (
    const as mc,
    extract_dict_payloads,
    get_element_by_key,
)
from emulator.mixins import MerossEmulatorDescriptor

if typing.TYPE_CHECKING:
    from typing import Final

    from .. import MerossEmulator


class _Transition:

    duration: Final

    def __init__(
        self, emulator: RollerShutterMixin, channel, position_begin, position_end
    ) -> None:
        assert channel not in emulator._transitions
        self.time_begin: Final = time()
        self.emulator: Final = emulator
        self.channel: Final = channel
        self.position_begin: Final = position_begin
        self.position_end: Final = position_end
        self.p_position: Final = emulator._get_namespace_state(
            mc.NS_APPLIANCE_ROLLERSHUTTER_POSITION, channel
        )
        self.p_state: Final = emulator._get_namespace_state(
            mc.NS_APPLIANCE_ROLLERSHUTTER_STATE, channel
        )
        if position_end > position_begin:
            if position_end > mc.ROLLERSHUTTER_POSITION_OPENED:
                position_end = mc.ROLLERSHUTTER_POSITION_OPENED
            self.p_state[mc.KEY_STATE] = mc.ROLLERSHUTTER_STATE_OPENING
            self.duration = (
                (position_end - position_begin)
                * RollerShutterMixin.OPENDURATION
                / (mc.ROLLERSHUTTER_POSITION_OPENED - mc.ROLLERSHUTTER_POSITION_CLOSED)
            )
        else:
            if position_end < mc.ROLLERSHUTTER_POSITION_CLOSED:
                position_end = mc.ROLLERSHUTTER_POSITION_CLOSED
            self.p_state[mc.KEY_STATE] = mc.ROLLERSHUTTER_STATE_CLOSING
            self.duration = (
                (position_begin - position_end)
                * RollerShutterMixin.CLOSEDURATION
                / (mc.ROLLERSHUTTER_POSITION_OPENED - mc.ROLLERSHUTTER_POSITION_CLOSED)
            )
        self.speed: Final = (position_end - position_begin) / self.duration
        self.callback_unsub = asyncio.get_event_loop().call_later(
            1, self._transition_callback
        )
        emulator._transitions[channel] = self

    def shutdown(self):
        self.emulator._transitions.pop(self.channel)
        self.p_state[mc.KEY_STATE] = mc.ROLLERSHUTTER_STATE_IDLE
        if self.callback_unsub:
            self.callback_unsub.cancel()
            self.callback_unsub = None

    def _transition_callback(self):
        self.callback_unsub = None
        time_delta = time() - self.time_begin
        if time_delta >= self.duration:
            self.shutdown()
            self.p_position[mc.KEY_POSITION] = self.position_end
            return
        self.callback_unsub = asyncio.get_event_loop().call_later(
            1, self._transition_callback
        )
        self.p_position[mc.KEY_POSITION] = self.position_begin + int(
            self.speed * time_delta
        )


class RollerShutterMixin(MerossEmulator if typing.TYPE_CHECKING else object):

    # TODO: implement behavior for legacy devices without native position
    NATIVE_POSITION = True
    OPENDURATION = 20
    CLOSEDURATION = 20

    def __init__(self, descriptor: MerossEmulatorDescriptor, key: str):
        self._transitions: dict[int, _Transition] = {}
        super().__init__(descriptor, key)
        # only 1 channel seen so far...even tho our transitions and message parsing
        # should already be multi-channel proof
        p_position = self._get_namespace_state(
            mc.NS_APPLIANCE_ROLLERSHUTTER_POSITION, 0
        )
        p_position[mc.KEY_POSITION] = mc.ROLLERSHUTTER_POSITION_CLOSED
        p_state = self._get_namespace_state(mc.NS_APPLIANCE_ROLLERSHUTTER_STATE, 0)
        p_state[mc.KEY_STATE] = mc.ROLLERSHUTTER_STATE_IDLE

    def shutdown(self):
        for transition in set(self._transitions.values()):
            transition.shutdown()
        super().shutdown()

    def _SET_Appliance_RollerShutter_Position(self, header, payload):
        """payload = { "postion": {"channel": 0, "position": 100}}"""
        for p_request in extract_dict_payloads(payload[mc.KEY_POSITION]):

            channel: int = p_request[mc.KEY_CHANNEL]

            if channel in self._transitions:
                self._transitions[channel].shutdown()

            position_end = int(p_request[mc.KEY_POSITION])
            if position_end == mc.ROLLERSHUTTER_POSITION_STOP:
                continue

            p_position = self._get_namespace_state(
                mc.NS_APPLIANCE_ROLLERSHUTTER_POSITION, channel
            )
            position_begin = p_position[mc.KEY_POSITION]
            if position_end == position_begin:
                continue

            _Transition(self, channel, position_begin, position_end)

        return mc.METHOD_SETACK, {}
