from hashlib import md5
from time import time
from typing import TYPE_CHECKING
from uuid import uuid4

from . import MerossKeyError, MerossProtocolError, const as mc, namespaces as mn
from .. import JSON_DECODER, JSON_ENCODER

if TYPE_CHECKING:
    from .types import KeyType, MerossHeaderType, MerossMessageType, MerossPayloadType


#
# Low level message building helpers
#
def compute_message_signature(messageid: str, key: str, timestamp, /):
    return md5(
        "".join((messageid, key, str(timestamp))).encode("utf-8"), usedforsecurity=False
    ).hexdigest()


def compute_message_encryption_key(uuid: str, key: str, mac: str, /):
    return md5(
        "".join((uuid[3:22], key[1:9], mac, key[10:28])).encode("utf-8"),
        usedforsecurity=False,
    ).hexdigest()


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
    The actual implementation will setup the slots
    """

    if TYPE_CHECKING:
        namespace: str
        method: str
        messageid: str
        payload: MerossPayloadType

    __slots__ = (
        "namespace",
        "method",
        "messageid",
        "payload",
        "_json_str",
    )

    def __init__(self, message: dict, json_str: str | None = None, /):
        self._json_str = json_str
        super().__init__(message)

    def json(self):
        if not self._json_str:
            self._json_str = JSON_ENCODER.encode(self)
        return self._json_str

    @staticmethod
    def decode(json_str: str, /):
        return MerossMessage(JSON_DECODER.decode(json_str), json_str)


class MerossResponse(MerossMessage):
    """Helper for messages received from a device"""

    def __init__(self, json_str: str, /):
        super().__init__(JSON_DECODER.decode(json_str), json_str)


class MerossRequest(MerossMessage):
    """Helper for messages to be sent"""

    def __init__(
        self,
        namespace: str,
        method: str,
        payload: "MerossPayloadType",
        key: str,
        from_: str = mc.HEADER_FROM_DEFAULT,
        triggerSrc: str = mc.HEADER_TRIGGERSRC_DEFAULT,
        /,
    ):
        self.namespace = namespace
        self.method = method
        self.payload = payload
        self.messageid = uuid4().hex
        timestamp = int(time())
        super().__init__(
            {
                mc.KEY_HEADER: {
                    mc.KEY_MESSAGEID: self.messageid,
                    mc.KEY_NAMESPACE: namespace,
                    mc.KEY_METHOD: method,
                    mc.KEY_PAYLOADVERSION: 1,
                    mc.KEY_TRIGGERSRC: triggerSrc,
                    mc.KEY_FROM: from_,
                    mc.KEY_TIMESTAMP: timestamp,
                    mc.KEY_TIMESTAMPMS: 0,
                    mc.KEY_SIGN: compute_message_signature(
                        self.messageid, key, timestamp
                    ),
                },
                mc.KEY_PAYLOAD: self.payload,
            }
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
        self.namespace = header[mc.KEY_NAMESPACE]
        self.method = header[mc.KEY_METHOD]
        self.messageid = header[mc.KEY_MESSAGEID]
        self.payload = payload
        header = header.copy()
        header.pop(mc.KEY_UUID, None)
        header[mc.KEY_TRIGGERSRC] = mc.HEADER_TRIGGERSRC_CLOUDCONTROL
        super().__init__(
            {
                mc.KEY_HEADER: header,
                mc.KEY_PAYLOAD: payload,
            }
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
        self.namespace = header[mc.KEY_NAMESPACE]
        self.method = mc.METHOD_ACK_MAP[header[mc.KEY_METHOD]]
        self.messageid = header[mc.KEY_MESSAGEID]
        self.payload = payload
        timestamp = int(time())
        super().__init__(
            {
                mc.KEY_HEADER: {
                    mc.KEY_MESSAGEID: self.messageid,
                    mc.KEY_NAMESPACE: self.namespace,
                    mc.KEY_METHOD: self.method,
                    mc.KEY_PAYLOADVERSION: 1,
                    mc.KEY_TRIGGERSRC: mc.HEADER_TRIGGERSRC_CLOUDCONTROL,
                    mc.KEY_FROM: from_,
                    mc.KEY_TIMESTAMP: timestamp,
                    mc.KEY_TIMESTAMPMS: 0,
                    mc.KEY_SIGN: compute_message_signature(
                        self.messageid, key, timestamp
                    ),
                },
                mc.KEY_PAYLOAD: payload,
            }
        )
