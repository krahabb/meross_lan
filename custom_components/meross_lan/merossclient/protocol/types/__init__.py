"""
A collection of typing definitions for payloads

"""

from typing import Any, Mapping, NotRequired, TypedDict, Union

type JsonDict = dict[str, Any]
"""Generic data-dict carried in Meross messages."""
type JsonList = list[JsonDict]
"""Generic data-list carried in Meross messages."""

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


type MerossPayloadType = JsonDict


class MerossMessageType(TypedDict):
    """Meross protocol message dictionary, containing header and payload."""

    header: MerossHeaderType
    payload: MerossPayloadType


type MerossRequestType = tuple[MerossNamespaceType, MerossMethodType, MerossPayloadType]
type KeyType = Union[MerossHeaderType, str, None]


class ChannelPayload(TypedDict):
    channel: int


class HubIdPayload(TypedDict):
    id: str


class HubSubIdPayload(ChannelPayload):
    subId: str


from . import config, control, sensor, system, thermostat
