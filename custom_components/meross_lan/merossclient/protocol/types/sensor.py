"""
A collection of typing definitions for payloads in Appliance.Control.Sensor.*
"""

from . import Any, ChannelPayload, NotRequired, SensorData, TypedDict


class LatestValue(TypedDict):
    timestamp: int
    temp: NotRequired[int]
    humi: NotRequired[int]


class Latest(ChannelPayload):
    """Appliance.Control.Sensor.Latest
    {
        "latest": [
            {
                "value": [{"humi": 596, "timestamp": 1718302844}],
                "channel": 0,
                "capacity": 2,
            }
        ]
    }
    """

    value: list[LatestValue]
    capacity: int  # Flags: 1 has temp - 2 has humi


class LatestXRequest(ChannelPayload):
    """
    Request format for LatestX and likely HistoryX payloads.
    channel is not enough to query this ns but we need to also add a
    list of requested sensor 'keys' ("light", "presence" for example in ms600).
    """

    data: list[str]


class LatestX(ChannelPayload):
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

    data: dict[str, list[SensorData]]
