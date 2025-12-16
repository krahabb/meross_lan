"""
A collection of typing definitions for payloads in Appliance.Control.*
(excluding Appliance.Control.Sensor.* and Appliance.Control.Thermostat.*)
"""

from . import ChannelPayload, HistoryData


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
