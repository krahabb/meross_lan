"""
A collection of typing definitions for payloads in Appliance.Hub.*
"""

from . import TypedDict


class IdPayload(TypedDict):
    id: str


class SubIdPayload(TypedDict):
    subId: str
    channel: str


class Battery(IdPayload):
    """Application.Hub.Battery"""

    value: int

class ToggleX(IdPayload):
    """Application.Hub.ToggleX"""

    onoff: int  # 1: on, 0: off


class Beep(IdPayload):
    """Application.Hub.SubDevice.Beep"""

    onoff: int  # 1: on, 0: off


class Version(IdPayload):
    """Application.Hub.SubDevice.Version"""

    hardware: str
    firmware: str
