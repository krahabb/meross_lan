"""
A collection of typing definitions for payloads in Appliance.GarageDoor.*
"""

from . import NotRequired
from .. import types as mt


class StateRequest(mt.ChannelPayload):

    open: int  # 1: open, 0: closed


class State(mt.ChannelPayload):
    """Appliance.GarageDoor.State channel payload."""

    open: int  # 1: open, 0: closed
    lmTime: int
    doorEnable: NotRequired[int]  # appeared on msg200 fw:4.2.8
