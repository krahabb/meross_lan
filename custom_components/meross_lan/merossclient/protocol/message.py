from functools import cached_property
from time import time
from typing import TYPE_CHECKING
from uuid import uuid4

from . import (
    JSON_DECODER,
    JSON_ENCODER,
    MerossKeyError,
    MerossProtocolError,
    compute_message_signature,
    const as mc,
    namespaces as mn,
)

if TYPE_CHECKING:
    from .types import KeyType, MerossHeaderType, MerossMessageType, MerossPayloadType


#
# Low level message building helpers
#


def build_message(
    namespace: str,
    method: str,
    payload: "MerossPayloadType",
    messageid: str,
    key: str,
    from_: str = mc.HEADER_FROM_DEFAULT,
    triggerSrc: str = mc.HEADER_TRIGGERSRC_DEFAULT,
    /,
) -> "MerossMessageType":
    timestamp = int(time())
    return {
        mc.KEY_HEADER: {
            mc.KEY_MESSAGEID: messageid,
            mc.KEY_NAMESPACE: namespace,
            mc.KEY_METHOD: method,
            mc.KEY_PAYLOADVERSION: 1,
            mc.KEY_TRIGGERSRC: triggerSrc,
            mc.KEY_FROM: from_,
            mc.KEY_TIMESTAMP: timestamp,
            mc.KEY_TIMESTAMPMS: 0,
            mc.KEY_SIGN: compute_message_signature(messageid, key, timestamp),
        },
        mc.KEY_PAYLOAD: payload,
    }


def build_message_keyhack(
    namespace: str,
    method: str,
    payload: "MerossPayloadType",
    key_header: "MerossHeaderType",
    from_: str = mc.HEADER_FROM_DEFAULT,
    triggerSrc: str = mc.HEADER_TRIGGERSRC_DEFAULT,
    /,
) -> "MerossMessageType":
    key_header[mc.KEY_NAMESPACE] = namespace
    key_header[mc.KEY_METHOD] = method
    key_header[mc.KEY_PAYLOADVERSION] = 1
    key_header[mc.KEY_TRIGGERSRC] = triggerSrc
    key_header[mc.KEY_FROM] = from_
    return {mc.KEY_HEADER: key_header, mc.KEY_PAYLOAD: payload}


#
# Various helpers to extract some meaningful data from payloads
#
def get_message_uuid(header: "MerossHeaderType", /):
    return header.get(mc.KEY_UUID) or mc.RE_PATTERN_TOPIC_UUID.match(header[mc.KEY_FROM]).group(1)  # type: ignore


def get_replykey(header: "MerossHeaderType", key: "KeyType", /) -> "KeyType":
    """
    checks header signature against key:
    if ok return sign itsef else return the full header { "messageId", "timestamp", "sign", ...}
    in order to be able to use it in a reply scheme
    **UPDATE 28-03-2021**
    the 'reply scheme' hack doesnt work on mqtt but works on http: this code will be left since it works if the key is correct
    anyway and could be reused in a future attempt
    """
    if isinstance(key, str):
        sign = compute_message_signature(
            header[mc.KEY_MESSAGEID], key, header[mc.KEY_TIMESTAMP]
        )
        if sign == header[mc.KEY_SIGN]:
            return key

    return header


def check_message_strict(message: "MerossResponse | None", /):
    """
    Does a formal check of the message structure also raising a
    typed exception if formally correct but carrying a protocol error
    """
    if not message:
        raise MerossProtocolError(message, "No response")
    try:
        payload = message[mc.KEY_PAYLOAD]
        header = message[mc.KEY_HEADER]
        header[mc.KEY_NAMESPACE]
        if header[mc.KEY_METHOD] == mc.METHOD_ERROR:
            p_error = payload[mc.KEY_ERROR]
            if p_error.get(mc.KEY_CODE) == mc.ERROR_INVALIDKEY:
                raise MerossKeyError(message)
            else:
                raise MerossProtocolError(message, p_error)
        return message
    except KeyError as error:
        raise MerossProtocolError(message, str(error)) from error


#
# 'Higher level' message representations
#
class MerossMessage(dict):
    """
    Base (almost) abstract class for different source of messages that
    need to be sent to the device (or received from).
    """

    @cached_property
    def json(self):
        return JSON_ENCODER.encode(self)

    @cached_property
    def header(self) -> "MerossHeaderType":
        return self[mc.KEY_HEADER]

    @cached_property
    def namespace(self) -> str:
        return self.header[mc.KEY_NAMESPACE]

    @cached_property
    def method(self) -> str:
        return self.header[mc.KEY_METHOD]

    @cached_property
    def messageid(self) -> str:
        return self.header[mc.KEY_MESSAGEID]

    @cached_property
    def payload(self) -> "MerossPayloadType":
        return self[mc.KEY_PAYLOAD]

    @staticmethod
    def decode(json: str, /):
        message = MerossMessage(JSON_DECODER.decode(json))
        message.json = json
        return message


class MerossResponse(MerossMessage):
    """Helper for messages received from a device"""

    def __init__(self, json: str, /):
        MerossMessage.__init__(self, JSON_DECODER.decode(json))
        self.json = json


class MerossRequest(MerossMessage):
    """Helper for messages to be sent"""

    def __init__(
        self,
        namespace: str,
        method: str,
        payload: "MerossPayloadType",
        key: str = "",
        from_: str = mc.HEADER_FROM_DEFAULT,
        trigger_src: str = mc.HEADER_TRIGGERSRC_DEFAULT,
        /,
    ):
        messageid = uuid4().hex
        timestamp = int(time())
        MerossMessage.__init__(
            self,
            {
                mc.KEY_HEADER: {
                    mc.KEY_MESSAGEID: messageid,
                    mc.KEY_NAMESPACE: namespace,
                    mc.KEY_METHOD: method,
                    mc.KEY_PAYLOADVERSION: 1,
                    mc.KEY_TRIGGERSRC: trigger_src,
                    mc.KEY_FROM: from_,
                    mc.KEY_TIMESTAMP: timestamp,
                    mc.KEY_TIMESTAMPMS: 0,
                    mc.KEY_SIGN: compute_message_signature(messageid, key, timestamp),
                },
                mc.KEY_PAYLOAD: payload,
            },
        )


class MerossPushReply(MerossMessage):
    """
    Builds a message by replying the full header. This is used
    in replies to some PUSH sent by devices where it appears
    (from meross broker protocol inspection - see #346)
    the broker doesn't calculate a new signature but just replies
    the incoming header data.
    """

    def __init__(self, header: "MerossHeaderType", payload: "MerossPayloadType", /):
        header = header.copy()
        header.pop(mc.KEY_UUID, None)
        header[mc.KEY_TRIGGERSRC] = mc.HEADER_TRIGGERSRC_CLOUDCONTROL
        MerossMessage.__init__(
            self,
            {
                mc.KEY_HEADER: header,
                mc.KEY_PAYLOAD: payload,
            },
        )


class MerossAckReply(MerossMessage):
    """
    Builds a response ascknowledge message by signing an incoming messageId.
    """

    def __init__(
        self,
        header: "MerossHeaderType",
        payload: "MerossPayloadType",
        key: str,
        from_: str,
        /,
    ):
        messageid = header[mc.KEY_MESSAGEID]
        timestamp = int(time())
        MerossMessage.__init__(
            self,
            {
                mc.KEY_HEADER: {
                    mc.KEY_MESSAGEID: messageid,
                    mc.KEY_NAMESPACE: header[mc.KEY_NAMESPACE],
                    mc.KEY_METHOD: mc.METHOD_ACK_MAP[header[mc.KEY_METHOD]],
                    mc.KEY_PAYLOADVERSION: 1,
                    mc.KEY_TRIGGERSRC: mc.HEADER_TRIGGERSRC_CLOUDCONTROL,
                    mc.KEY_FROM: from_,
                    mc.KEY_TIMESTAMP: timestamp,
                    mc.KEY_TIMESTAMPMS: 0,
                    mc.KEY_SIGN: compute_message_signature(messageid, key, timestamp),
                },
                mc.KEY_PAYLOAD: payload,
            },
        )
