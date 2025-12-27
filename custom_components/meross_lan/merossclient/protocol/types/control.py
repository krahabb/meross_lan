"""
A collection of typing definitions for payloads in Appliance.Control.*
(excluding Appliance.Control.Sensor.* and Appliance.Control.Thermostat.*)
"""

from . import ChannelOnOff, ChannelPayload, HistoryData, TypedDict, _MerossPayloadType


class Beep_C(ChannelPayload):
    """Appliance.Control.Beep"""

    onoff: int


class ConsumptionH_C(ChannelPayload):
    """Appliance.Control.ConsumptionH"""

    total: int  # [Wh]
    data: list[HistoryData]


class Electricity_C(ChannelPayload):
    """Appliance.Control.Electricity"""

    current: int  # [mA]
    voltage: int  # 10ths of [V]
    power: int  # [mW]


class ElectricityX_C(Electricity_C):
    """Appliance.Control.ElectricityX"""

    voltage: int  # [mV]
    mConsume: int  # [Wh]
    factor: float  # power factor (0.0-1.0)


class TempUnit_C(ChannelPayload):
    """Appliance.Control.TempUnit"""

    tempUnit: int  # 1: Celsius 2: Fahreneit TODO add a select entity for configuration


class OverTemp(TypedDict):
    """Appliance.Control.OverTemp. Reverse engineered PUSH
    {"payload": {"overTemp": {"value": 1, "timestamp": 1622547802, "type": 1}}}.
    TODO: implement a binary sensor or so."""

    value: int  # 1: overtemp alarm active, 0: inactive (guessing)
    timestamp: int
    type: int  # 2 is the only detected type in app.


class PhysicalLock_C(ChannelOnOff):
    """Appliance.Control.PhysicalLock channel payload."""


class PhysicalLock(_MerossPayloadType):
    """Appliance.Control.PhysicalLock payload containing a list of channel payloads."""

    lock: list[PhysicalLock_C]
