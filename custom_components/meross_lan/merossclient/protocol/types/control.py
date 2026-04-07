"""
A collection of typing definitions for payloads in Appliance.Control.*
(excluding Appliance.Control.Sensor.* and Appliance.Control.Thermostat.*)
"""

from . import NotRequired, TypedDict
from .. import types as mt


class Beep(mt.ChannelOnOff):
    """Appliance.Control.Beep"""


class ConsumptionH(mt.ChannelPayload):
    """Appliance.Control.ConsumptionH"""

    total: int  # [Wh]
    data: list[mt.SensorData]


class Electricity(mt.ChannelPayload):
    """Appliance.Control.Electricity"""

    current: int  # [mA]
    voltage: int  # 10ths of [V]
    power: int  # [mW]


class ElectricityX(Electricity):
    """Appliance.Control.ElectricityX"""

    voltage: int  # [mV]
    mConsume: int  # [Wh]
    factor: float  # power factor (0.0-1.0)


class Fan(mt.ChannelPayload):
    """Appliance.Control.Fan channel payload."""

    speed: int
    maxSpeed: int


class Light(mt.ChannelPayload):
    """Appliance.Control.Light channel payload."""

    # "onoff" is not always available though..depends on model
    onoff: NotRequired[int]
    capacity: int
    luminance: int
    rgb: int
    temperature: int
    effect: int


class Light_Effect_Member(TypedDict):
    luminance: int
    rgb: NotRequired[int]
    temperature: NotRequired[int]


class Light_Effect(mt.IdPayload):
    """Appliance.Control.Light.Effect."""

    effectName: str
    iconName: str
    enable: int
    mode: int
    speed: int
    member: list[Light_Effect_Member]


class Spray(mt.ChannelPayload):
    """Appliance.Control.Spray channel payload."""

    mode: int  # 0: off, 1: on, 2: auto (guessing)
    lmTime: int
    lastMode: int
    onoffTime: int


class TempUnit(mt.ChannelPayload):
    """Appliance.Control.TempUnit channel payload."""

    tempUnit: int  # 1: Celsius 2: Fahreneit


class OverTemp(TypedDict):
    """Appliance.Control.OverTemp. Reverse engineered PUSH
    {"payload": {"overTemp": {"value": 1, "timestamp": 1622547802, "type": 1}}}.
    TODO: implement a binary sensor or so."""

    value: int  # 1: overtemp alarm active, 0: inactive (guessing)
    timestamp: int
    type: int  # 2 is the only detected type in app.


class PhysicalLock(mt.ChannelOnOff):
    """Appliance.Control.PhysicalLock channel payload."""


class Upgrade_Mcu(TypedDict):
    type: NotRequired[str]
    url: str
    md5: str


class Upgrade_SubDev(TypedDict):
    devid: str
    url: str
    md5: str


class Upgrade(TypedDict):
    """Appliance.Control.Upgrade payload definition."""

    # common fields for generic core device upgrade
    url: NotRequired[str]
    md5: NotRequired[str]
    # some devices expose an 'mcu' with its own fw upgrade info
    mcu: NotRequired[list[Upgrade_Mcu]]
    # hub subdevice upgrade info
    subdev: NotRequired[list[Upgrade_SubDev]]
