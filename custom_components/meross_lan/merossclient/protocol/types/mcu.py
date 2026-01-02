"""
A collection of typing definitions for payloads in Appliance.Mcu.*
"""

from . import TypedDict


class Firmware(TypedDict):
    """Appliance.Mcu.Firmware payload definition."""

    productId: str
    version: str
    type: str
