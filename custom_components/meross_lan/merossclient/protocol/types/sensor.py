"""
A collection of typing definitions for payloads
in Appliance.Control.Sensor.*
"""

from . import ChannelPayload, TypedDict, _MerossPayloadType


class LatestXRequest_C(ChannelPayload):
    data: list[str]


class LatestXData(TypedDict, total=False):
    value: int
    timestamp: int

class LatestXResponse_C(ChannelPayload):
    data: dict[str, list[LatestXData]]

class LatestXResponse(_MerossPayloadType):
    """
    {
        "latest": [
            {
                "channel": 0,
                "data": {
                    "presence": [
                        {
                            "times": 0,
                            "distance": 760,
                            "value": 2,
                            "timestamp": 1725907895,
                        }
                    ],
                    "light": [
                        {
                            "timestamp": 1725907912,
                            "value": 24,
                        }
                    ],
                },
            }
        ]
    }
    Example taken from ms600
    """

    latest: list[LatestXResponse_C]