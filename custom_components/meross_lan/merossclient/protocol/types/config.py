"""
A collection of typing definitions for payloads in Appliance.Config.*
"""

from . import TypedDict


class Wifi(TypedDict):
    ssid: str  # base64 encoded
    password: str  # base64 encoded or encrypted (WifiX)

    bssid: str
    channel: int
    signal: int
    encryption: int
    cipher: int


type WifiList = list[Wifi]
