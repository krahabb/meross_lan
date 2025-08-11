"""
A collection of typing definitions for payloads
in Appliance.Control.* (excluding Appliance.Control.Sensor.* and Appliance.Control.Thermostat.*)
"""

from . import ChannelPayload


class TempUnit_C(ChannelPayload):
    tempUnit: int  # 1: Celsius 2: Fahreneit TODO add a select entity for configuration
