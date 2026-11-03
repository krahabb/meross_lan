"""
A collection of typing definitions for payloads in Appliance.Control.Diffuser.*
"""

from . import TypedDict
from .. import types as mt


class Light(mt.ChannelOnOff):
    """Appliance.Control.Diffuser.Light channel payload."""

    lmTime: int
    mode: int
    luminance: int
    rgb: int


class Spray(mt.ChannelPayload):
    """Appliance.Control.Diffuser.Spray channel payload."""

    mode: int
    lmTime: int


class Sensor(mt._MerossPayloadType):
    """Appliance.Control.Diffuser.Sensor"""

    type: str  # "mod100"
    humidity: mt.SensorDataL  # {"value": 0, "lmTime": 0}
    temperature: mt.SensorDataL  # {"value": 0, "lmTime": 0}


class Digest(TypedDict):

    light: list[Light]
    spray: list[Spray]
