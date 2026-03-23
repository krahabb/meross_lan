"""
A collection of typing definitions for payloads

"""

from typing import Any, Mapping, NotRequired, TypedDict, Union

type JsonDict = dict[str, Any]
type JsonMapping = Mapping[str, Any]
"""Generic data-dict carried in Meross messages."""
type JsonList = list[JsonDict]
"""Generic data-list carried in Meross messages."""
type JsonType = Union[JsonDict, JsonList, str, int, float, bool, None]
"""Generic data-type carried in Meross messages."""

type MerossNamespaceType = str
type MerossMethodType = str
MerossHeaderType = TypedDict(
    "MerossHeaderType",
    {
        "messageId": str,
        "namespace": str,
        "method": str,
        "payloadVersion": int,
        "triggerSrc": NotRequired[str],  # Older fw didn't support this. Newer need.
        "from": str,
        "uuid": NotRequired[str],  # Older fw didn't support this
        "timestamp": int,
        "timestampMs": int,
        "sign": str,
    },
)


class _MerossPayloadType(TypedDict):
    pass


type MerossPayloadType = JsonDict


class MerossMessageType(TypedDict):
    """Meross protocol message dictionary, containing header and payload."""

    header: MerossHeaderType
    payload: MerossPayloadType


type MerossRequestType = tuple[MerossNamespaceType, MerossMethodType, MerossPayloadType]
type KeyType = Union[MerossHeaderType, str, None]
type VersionTupleType = tuple[int, ...]
type PayloadIndexType = int | str


class ChannelPayload(TypedDict):
    """These payloads include a channel identifier and are typically included as a
    list payload in the specific ns payload message.
    i.e.
    {
        header: MerossHeaderType,
        payload: dict[str, list[ChannelPayload]] # where str is restricted to the ns.key
    }
    As an internal convention, inherited types (i.e. specific ns payloads)
    are coded with a _C suffix."""

    channel: int


class ChannelOnOff(ChannelPayload):
    """Common channel payload including an onoff field (various namespaces)."""

    onoff: int


class SensorDataL(TypedDict):
    """
    A common struct for sensor values reporting.
    """

    value: int
    lmTime: int


class SensorData(TypedDict):
    """
    A common struct usually appearing in a list of historical data points (LatestX, ConsumptionH).
    """

    value: int
    timestamp: int


from . import (
    config,
    control,
    diffuser,
    hub,
    mcu,
    rollershutter,
    sensor,
    system,
    thermostat,
)
