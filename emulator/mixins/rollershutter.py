""""""

import asyncio
from time import time
import typing

from custom_components.meross_lan.helpers import clamp, versiontuple
from custom_components.meross_lan.merossclient import extract_dict_payloads
from custom_components.meross_lan.merossclient.protocol import (
    const as mc,
    namespaces as mn,
)
from emulator.mixins import MerossEmulatorDescriptor

if typing.TYPE_CHECKING:

    from .. import MerossEmulator

_SIGNAL_SCALE = 1000
_DURATION_SCALE = _SIGNAL_SCALE * (
    mc.ROLLERSHUTTER_POSITION_OPENED - mc.ROLLERSHUTTER_POSITION_CLOSED
)


class _Transition:

    duration: typing.Final

    def __init__(
        self,
        emulator: "RollerShutterMixin",
        channel: int,
        p_position: dict,
        position_end: int,
    ) -> None:
        assert channel not in emulator._transitions
        self.time_begin: typing.Final = time()
        self.emulator: typing.Final = emulator
        self.has_native_position: typing.Final = emulator.has_native_position
        self.channel: typing.Final = channel
        self.p_position: typing.Final = p_position
        self.position_begin: typing.Final = p_position[mc.KEY_POSITION]
        self.position_end: typing.Final = position_end
        self.p_state: typing.Final = emulator.get_namespace_state(
            mn.Appliance_RollerShutter_State.name, channel
        )
        p_config = emulator.get_namespace_state(
            mn.Appliance_RollerShutter_Config.name, channel
        )
        if self.has_native_position:
            if position_end > self.position_begin:
                self.p_state[mc.KEY_STATE] = mc.ROLLERSHUTTER_STATE_OPENING
                self.duration = (
                    (position_end - self.position_begin)
                    * p_config[mc.KEY_SIGNALOPEN]
                    / _DURATION_SCALE
                )
            else:
                self.p_state[mc.KEY_STATE] = mc.ROLLERSHUTTER_STATE_CLOSING
                self.duration = (
                    (self.position_begin - position_end)
                    * p_config[mc.KEY_SIGNALCLOSE]
                    / _DURATION_SCALE
                )
            self.speed: typing.Final = (
                position_end - self.position_begin
            ) / self.duration
        else:
            if position_end == mc.ROLLERSHUTTER_POSITION_OPENED:
                # when opening we'll set the position opened at the start of the transition
                self.p_position[mc.KEY_POSITION] = position_end
                self.p_state[mc.KEY_STATE] = mc.ROLLERSHUTTER_STATE_OPENING
                self.duration = p_config[mc.KEY_SIGNALOPEN] / _SIGNAL_SCALE
            else:  # position_end == mc.ROLLERSHUTTER_POSITION_CLOSED:
                # when closing we'll set the position closed only at the end
                # of the transition so that it stays opened until a full closing run is done.
                # This should be consistent with real device behavior (not sure though)
                self.p_state[mc.KEY_STATE] = mc.ROLLERSHUTTER_STATE_CLOSING
                self.duration = p_config[mc.KEY_SIGNALCLOSE] / _SIGNAL_SCALE

        self.callback_unsub = asyncio.get_event_loop().call_later(
            RollerShutterMixin.SIGNAL_TRANSITION_PERIOD, self._transition_callback
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
            self.p_position[mc.KEY_POSITION] = self.position_end
            self.shutdown()
            return
        self.callback_unsub = asyncio.get_event_loop().call_later(
            RollerShutterMixin.SIGNAL_TRANSITION_PERIOD, self._transition_callback
        )
        if self.has_native_position:
            self.p_position[mc.KEY_POSITION] = self.position_begin + int(
                self.speed * time_delta
            )


class RollerShutterMixin(MerossEmulator if typing.TYPE_CHECKING else object):

    # set open/close timeouts (in msec to align to device natives)
    # different so to test they're used correctly
    SIGNALCLOSE = 20000
    SIGNALOPEN = 30000
    # the internal sampling of the 'Transition'
    SIGNAL_TRANSITION_PERIOD = 1  # sec

    def __init__(self, descriptor: MerossEmulatorDescriptor, key: str):
        self._transitions: dict[int, _Transition] = {}
        super().__init__(descriptor, key)
        self.has_native_position = versiontuple(
            descriptor.firmwareVersion
        ) >= versiontuple("6.6.6")
        # only 1 channel seen so far...even tho our transitions and message parsing
        # should already be multi-channel proof
        self.update_namespace_state(
            mn.Appliance_RollerShutter_Config.name,
            0,
            {
                mc.KEY_SIGNALCLOSE: RollerShutterMixin.SIGNALCLOSE,
                mc.KEY_SIGNALOPEN: RollerShutterMixin.SIGNALOPEN,
            },
        )
        self.update_namespace_state(
            mn.Appliance_RollerShutter_Position.name,
            0,
            {
                mc.KEY_POSITION: mc.ROLLERSHUTTER_POSITION_CLOSED,
            },
        )
        self.update_namespace_state(
            mn.Appliance_RollerShutter_State.name,
            0,
            {
                mc.KEY_STATE: mc.ROLLERSHUTTER_STATE_IDLE,
            },
        )

    def shutdown(self):
        for transition in set(self._transitions.values()):
            transition.shutdown()
        super().shutdown()

    def _GET_Appliance_Control_ToggleX(self, header, payload):
        return mc.METHOD_GETACK, {"channel": 0}  # 'strange' format response in #447

    def _SET_Appliance_RollerShutter_Position(self, header, payload):
        """payload = { "postion": {"channel": 0, "position": 100}}"""
        for p_request in extract_dict_payloads(payload[mc.KEY_POSITION]):

            channel: int = p_request[mc.KEY_CHANNEL]

            if channel in self._transitions:
                self._transitions[channel].shutdown()

            position_end = int(p_request[mc.KEY_POSITION])
            if position_end == mc.ROLLERSHUTTER_POSITION_STOP:
                continue

            p_position = self.get_namespace_state(
                mn.Appliance_RollerShutter_Position.name, channel
            )
            if self.has_native_position:
                # accepts intermediate positioning
                position_end = clamp(
                    position_end,
                    mc.ROLLERSHUTTER_POSITION_CLOSED,
                    mc.ROLLERSHUTTER_POSITION_OPENED,
                )
                if position_end == p_position[mc.KEY_POSITION]:
                    continue
            else:
                # accepts only full run
                if position_end not in (
                    mc.ROLLERSHUTTER_POSITION_OPENED,
                    mc.ROLLERSHUTTER_POSITION_CLOSED,
                ):
                    continue
            _Transition(self, channel, p_position, position_end)

        return mc.METHOD_SETACK, {}
