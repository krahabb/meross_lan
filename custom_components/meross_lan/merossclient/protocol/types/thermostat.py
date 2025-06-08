"""
A collection of typing definitions for payloads
in Appliance.Control.Thermostat
"""

import typing
from typing import TypedDict

from . import ChannelPayload


class ModeC_fan(TypedDict):
    fMode: int  # 0
    speed: int  # 0
    hTime: int  # 99999


class ModeC_more(TypedDict):
    hdStatus: int  # 0
    humi: int  # 495
    cStatus: int  # 0
    hStatus: int  # 0
    fStatus: int  # 0
    aStatus: int  # 0

class ModeC_targetTemp(TypedDict):
    heat: int # 2100
    cold: int # 2400


class ModeC(ChannelPayload):
    """{
        "fan": {
            "fMode": 0,
            "speed": 0,
            "hTime": 99999
        },
        "sensorTemp": 2200,
        "currentTemp": 2200,
        "more": {
            "hdStatus": 0,
            "humi": 495,
            "cStatus": 0,
            "hStatus": 0,
            "fStatus": 0,
            "aStatus": 0
        },
        "channel": 0,
        "mode": 3,
        "work": 2,
        "targetTemp": {
            "heat": 2100,
            "cold": 2400
        }
    }"""

    fan: ModeC_fan
    sensorTemp: int # 2200
    currentTemp: int # 2200
    more: ModeC_more
    mode: int # 3
    work: int # 2
    targetTemp: ModeC_targetTemp
