"""
A collection of typing definitions for payloads in Appliance.Control.Diffuser.*
"""

from . import (
    ChannelOnOff,
    ChannelPayload,
    NotRequired,
    SensorDataL,
    TypedDict,
    _MerossPayloadType,
)


class Light_C(ChannelOnOff):
    lmTime: int  # 1639082117
    mode: int  # 0
    luminance: int  # 100
    rgb: int  # 4129023


class Light(_MerossPayloadType):
    """Appliance.Control.Diffuser.Light"""

    light: list[Light_C]


class Spray_C(ChannelPayload):
    mode: int  # 2
    lmTime: int  # 1644353195


class Spray(_MerossPayloadType):
    """Appliance.Control.Diffuser.Spray"""

    spray: list[Spray_C]


class Sensor(_MerossPayloadType):
    """Appliance.Control.Diffuser.Sensor"""
    type: str  # "mod100"
    humidity: SensorDataL  # {"value": 0, "lmTime": 0}
    temperature: SensorDataL  # {"value": 0, "lmTime": 0}
