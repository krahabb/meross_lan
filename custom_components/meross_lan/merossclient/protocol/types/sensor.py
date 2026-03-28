"""
A collection of typing definitions for payloads in Appliance.Control.Sensor.*
"""

from . import Any, ChannelPayload, SensorData, TypedDict


class SensorXRequest_C(ChannelPayload):
    """
    Request format for LatestX and likely HistoryX payloads.
    channel is not enough to query this ns but we need to also add a
    list of requested sensor 'keys' ("light", "presence" for example in ms600).
    """

    data: list[str]


class SensorX_C(ChannelPayload):
    """
    Response format for LatestX and likely HistoryX payloads.
    The 'data' dict contains sensor keys and a list of dicts.
    list of requested sensor 'keys' ("light", "presence" for example in ms600)
    """

    data: dict[str, list[dict[str, Any]]]


class LatestXRequest_C(SensorXRequest_C):
    pass


class LatestX_C(SensorX_C):
    data: dict[str, list[SensorData]]


class LatestX(TypedDict):
    """Appliance.Control.Sensor.LatestX
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

    latest: list[LatestX_C]
