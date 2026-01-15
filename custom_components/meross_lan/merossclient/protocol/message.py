from functools import cached_property
import json
import os
from time import time
from typing import TYPE_CHECKING

from . import MerossKeyError, MerossProtocolError, const as mc, md5hexdigest

if TYPE_CHECKING:
    from .types import KeyType, MerossHeaderType, MerossPayloadType


#
# Optimized JSON encoding/decoding
#
JSON_ENCODER = json.JSONEncoder(
    ensure_ascii=False, check_circular=False, separators=(",", ":")
)
JSON_DECODER = json.JSONDecoder()


def json_dumps(obj):
    """Slightly optimized json.dumps with pre-configured encoder"""
    return JSON_ENCODER.encode(obj)


def json_loads(s: str):
    """Slightly optimized json.loads with pre-configured decoder"""
    return JSON_DECODER.raw_decode(s)[0]


def compute_message_signature(messageid: str, key: str, timestamp: int, /):
    return md5hexdigest(messageid, key, str(timestamp))


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


#
# 'Higher level' message representations
#
class MerossMessage(dict):
    """
    Base (almost) abstract class for different source of messages that
    need to be sent to the device (or received from).
    The actual implementation will setup the slots
    """

    @staticmethod
    def build(
        namespace: str,
        method: str,
        payload: "MerossPayloadType",
        key: str,
        *,
        messageid: str | None = None,
        from_: str = mc.HEADER_FROM_DEFAULT,
        triggerSrc: str = mc.HEADER_TRIGGERSRC_DEFAULT,
    ):
        timestamp = int(time())
        messageid = messageid or MerossMessage.generate_id()
        return MerossMessage(
            {
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
        )

    @staticmethod
    def build_keyhack(
        namespace: str,
        method: str,
        payload: "MerossPayloadType",
        key_header: "MerossHeaderType",
        from_: str = mc.HEADER_FROM_DEFAULT,
        triggerSrc: str = mc.HEADER_TRIGGERSRC_DEFAULT,
        /,
    ):
        key_header[mc.KEY_NAMESPACE] = namespace
        key_header[mc.KEY_METHOD] = method
        key_header[mc.KEY_PAYLOADVERSION] = 1
        key_header[mc.KEY_TRIGGERSRC] = triggerSrc
        key_header[mc.KEY_FROM] = from_
        return MerossMessage(
            {
                mc.KEY_HEADER: key_header,
                mc.KEY_PAYLOAD: payload,
            }
        )

    @staticmethod
    def decode(json: str, /):
        message = MerossMessage(JSON_DECODER.decode(json))
        message.json = json
        return message

    @staticmethod
    def generate_id():
        return "%032x" % int.from_bytes(os.urandom(16))

    @staticmethod
    def compute_encryption_key(uuid: str, key: str, mac: str, /):
        return md5hexdigest(uuid[3:22], key[1:9], mac, key[10:28]).encode()

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
    def uuid(self, /):
        return self.header.get(mc.KEY_UUID) or mc.RE_PATTERN_TOPIC_UUID.match(self.header[mc.KEY_FROM]).group(1)  # type: ignore

    @cached_property
    def payload(self) -> "MerossPayloadType":
        return self[mc.KEY_PAYLOAD]

    def check(self, /):
        """
        Does a formal check of the message structure also raising a
        typed exception if formally correct but carrying a protocol error
        """
        try:
            payload = self.payload
            self.namespace  # just to trigger possible KeyError for namespace and/or header
            if self.method == mc.METHOD_ERROR:
                if payload[mc.KEY_ERROR].get(mc.KEY_CODE) == mc.ERROR_INVALIDKEY:
                    raise MerossKeyError(self)
                else:
                    raise MerossProtocolError(self, payload[mc.KEY_ERROR])
            return self
        except KeyError as error:
            raise MerossProtocolError(self, str(error)) from error

    def compute_signature(self, key: str, /):
        return compute_message_signature(
            self.messageid,
            key,
            self.header[mc.KEY_TIMESTAMP],
        )


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
        uuid: str | None = None,
        /,
    ):
        if uuid:
            self.uuid = uuid
        self.namespace = namespace
        self.method = method
        self.payload = payload
        self.messageid = MerossMessage.generate_id()
        timestamp = int(time())
        MerossMessage.__init__(
            self,
            {
                mc.KEY_HEADER: {
                    mc.KEY_MESSAGEID: self.messageid,
                    mc.KEY_NAMESPACE: namespace,
                    mc.KEY_METHOD: method,
                    mc.KEY_PAYLOADVERSION: 1,
                    mc.KEY_TRIGGERSRC: trigger_src,
                    mc.KEY_FROM: from_,
                    mc.KEY_TIMESTAMP: timestamp,
                    mc.KEY_TIMESTAMPMS: 0,
                    mc.KEY_SIGN: compute_message_signature(
                        self.messageid, key, timestamp
                    ),
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

    def __init__(self, message: MerossMessage, payload: "MerossPayloadType", /):
        # The policy here is to only preset the properties which are almost always used
        # while processing this message. Generally speaking uuid is the most relevant while
        # namespace, method, messageid should only be used when VERBOSE/DEBUG logging is active.
        self.uuid = message.uuid
        header = message.header.copy()
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
        message: MerossMessage,
        payload: "MerossPayloadType",
        key: str,
        from_: str,
        /,
    ):
        # The policy here is to only preset the properties which are almost always used
        # while processing this message. Generally speaking uuid is the most relevant while
        # namespace, method, messageid should only be used when VERBOSE/DEBUG logging is active.
        self.uuid = message.uuid
        timestamp = int(time())
        MerossMessage.__init__(
            self,
            {
                mc.KEY_HEADER: {
                    mc.KEY_MESSAGEID: message.messageid,
                    mc.KEY_NAMESPACE: message.namespace,
                    mc.KEY_METHOD: mc.METHOD_ACK_MAP[message.method],
                    mc.KEY_PAYLOADVERSION: 1,
                    mc.KEY_TRIGGERSRC: mc.HEADER_TRIGGERSRC_CLOUDCONTROL,
                    mc.KEY_FROM: from_,
                    mc.KEY_TIMESTAMP: timestamp,
                    mc.KEY_TIMESTAMPMS: 0,
                    mc.KEY_SIGN: compute_message_signature(
                        message.messageid, key, timestamp
                    ),
                },
                mc.KEY_PAYLOAD: payload,
            },
        )
