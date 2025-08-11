"""
A collection of typing definitions for payloads

"""
from typing import Any, Mapping, NotRequired, TypedDict, Union

type MerossNamespaceType = str
type MerossMethodType = str
MerossHeaderType = TypedDict(
    "MerossHeaderType",
    {
        "messageId": str,
        "namespace": str,
        "method": str,
        "payloadVersion": int,
        "triggerSrc": NotRequired[str],
        "from": str,
        "uuid": NotRequired[str],
        "timestamp": int,
        "timestampMs": int,
        "sign": str,
    },
)
class _MerossPayloadType(TypedDict):
    pass

type MerossPayloadType = dict[str, Any]

class MerossMessageType(TypedDict):
    header: MerossHeaderType
    payload: MerossPayloadType

type MerossRequestType = tuple[MerossNamespaceType, MerossMethodType, MerossPayloadType]
type KeyType = Union[MerossHeaderType, str, None]


class ChannelPayload(TypedDict):
    channel: Any

class HubIdPayload(TypedDict):
    id: str

class HubSubIdPayload(ChannelPayload):
    subId: str