"""
A collection of typing definitions for payloads in Appliance.RollerShutter.*
"""

from . import ChannelOnOff, ChannelPayload, SensorData, TypedDict, _MerossPayloadType


class AdjustRequest_C(ChannelPayload):
    """Appliance.RollerShutter.Adjust"""

    value: int
    """1: start adjustment, 2: stop adjustment (Guessing...)"""


class AdjustResponse_C(ChannelPayload):
    """Appliance.RollerShutter.Adjust"""

    status: int


class Position_C(ChannelPayload):
    """Appliance.RollerShutter.Position"""

    position: int
    """0: fully closed, 100: fully opened, -1: stop"""


class Status_C(ChannelPayload):
    """Appliance.RollerShutter.Status"""

    state: int
    """0: idle, 1: opening, 2: closing"""
