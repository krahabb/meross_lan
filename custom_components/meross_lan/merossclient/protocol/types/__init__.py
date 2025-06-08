"""
A collection of typing definitions for payloads

"""
import typing

type MerossNamespaceType = str
type MerossMethodType = str
MerossHeaderType = typing.TypedDict(
    "MerossHeaderType",
    {
        "messageId": str,
        "namespace": str,
        "method": str,
        "payloadVersion": int,
        "triggerSrc": typing.NotRequired[str],
        "from": str,
        "uuid": typing.NotRequired[str],
        "timestamp": int,
        "timestampMs": int,
        "sign": str,
    },
)
MerossPayloadType = dict[str, typing.Any]
class MerossMessageType(typing.TypedDict):
    header: MerossHeaderType
    payload: MerossPayloadType

MerossRequestType = tuple[MerossNamespaceType, MerossMethodType, MerossPayloadType]
type KeyType = typing.Union[MerossHeaderType, str, None]


class ChannelPayload(typing.TypedDict):
    channel: int

class HubIdPayload(typing.TypedDict):
    id: str

class HubSubIdPayload(ChannelPayload):
    subId: str