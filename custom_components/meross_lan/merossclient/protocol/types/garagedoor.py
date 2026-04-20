"""
A collection of typing definitions for payloads in Appliance.GarageDoor.*
"""

from . import NotRequired
from .. import types as mt


class Config(mt.ChannelPayload):
    """Appliance.GarageDoor.Config channel payload."""

    doorEnable: int  # 1: enabled, 0: disabled
    doorOpenDuration: int
    doorCloseDuration: int


class MultipleConfig(mt.ChannelPayload):
    """Appliance.GarageDoor.MultipleConfig channel payload."""

    doorEnable: int
    timestamp: int
    timestampMs: int
    doorCloseDuration: NotRequired[int]  # appeared on msg200 fw:4.2.8
    doorOpenDuration: NotRequired[int]  # appeared on msg200 fw:4.2.8
    signalClose: int
    signalOpen: int
    buzzerEnable: int


class StateRequest(mt.ChannelPayload):

    open: int  # 1: open, 0: closed


class State(mt.ChannelPayload):
    """Appliance.GarageDoor.State channel payload."""

    open: int  # 1: open, 0: closed
    lmTime: int
    doorEnable: NotRequired[int]  # appeared on msg200 fw:4.2.8
