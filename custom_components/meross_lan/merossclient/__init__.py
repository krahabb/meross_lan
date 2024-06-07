"""
    A collection of utilities to help managing the Meross device protocol
"""

import asyncio
from dataclasses import dataclass
from hashlib import md5
import json
import re
from time import time
import typing
from uuid import uuid4

from . import const as mc, namespaces as mn

MerossNamespaceType = str
MerossMethodType = str
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
MerossMessageType = typing.TypedDict(
    "MerossMessageType", {"header": MerossHeaderType, "payload": MerossPayloadType}
)
MerossRequestType = tuple[MerossNamespaceType, MerossMethodType, MerossPayloadType]
KeyType = typing.Union[MerossHeaderType, str, None]


try:
    from random import randint

    class MEROSSDEBUG:
        # this will raise an OSError on non-dev machines missing the
        # debug configuration so the MEROSSDEBUG symbol will be invalidated
        data = json.load(
            open(
                file="./custom_components/meross_lan/merossclient/debug.secret.json",
                mode="r",
                encoding="utf-8",
            )
        )

        cloudapi_login = data.get("login")
        cloudapi_device_devlist = data.get("Device_devList")
        cloudapi_device_latestversion = data.get("Device_latestVersion")

        mqtt_connect_probability = 50

        @staticmethod
        def mqtt_random_connect():
            return randint(0, 99) < MEROSSDEBUG.mqtt_connect_probability

        mqtt_disconnect_probability = 0

        @staticmethod
        def mqtt_random_disconnect():
            return randint(0, 99) < MEROSSDEBUG.mqtt_disconnect_probability

        # MerossHTTPClient debug patching
        http_disc_end = 0
        http_disc_duration = 25
        http_disc_probability = 0

        @staticmethod
        def http_random_timeout():
            if MEROSSDEBUG.http_disc_end:
                if time() < MEROSSDEBUG.http_disc_end:
                    raise asyncio.TimeoutError()
                MEROSSDEBUG.http_disc_end = 0
                return

            if randint(0, 99) < MEROSSDEBUG.http_disc_probability:
                MEROSSDEBUG.http_disc_end = time() + MEROSSDEBUG.http_disc_duration
                raise asyncio.TimeoutError()

except Exception:
    MEROSSDEBUG = None  # type: ignore


_json_encoder = json.JSONEncoder(
    ensure_ascii=False, check_circular=False, separators=(",", ":")
)
_json_decoder = json.JSONDecoder()


def json_dumps(obj):
    """Slightly optimized json.dumps with pre-configured encoder"""
    return _json_encoder.encode(obj)


def json_loads(s: str):
    """Slightly optimized json.loads with pre-configured decoder"""
    return _json_decoder.raw_decode(s)[0]


class MerossProtocolError(Exception):
    """
    signal a protocol error like:
    - missing header keys
    - application layer ERROR(s)

    - response is the full response payload
    - reason is an additional context error
    """

    def __init__(self, response, reason: object | None = None):
        self.response = response
        self.reason = reason
        super().__init__(reason)


class MerossKeyError(MerossProtocolError):
    """
    signal a protocol key error (wrong key)
    reported by device
    """

    def __init__(self, response: "MerossResponse"):
        super().__init__(response, "Invalid key")


class MerossSignatureError(MerossProtocolError):
    """
    signal a protocol signature error detected
    when validating the received header
    """

    def __init__(self, response: "MerossResponse"):
        super().__init__(response, "Signature error")


@dataclass
class HostAddress:
    __slots__ = (
        "host",
        "port",
    )
    host: str
    port: int

    @staticmethod
    def build(address: str, default_port=mc.MQTT_DEFAULT_PORT):
        """Splits the eventual :port suffix from domain and return (host, port)"""
        if (colon_index := address.find(":")) != -1:
            return HostAddress(address[0:colon_index], int(address[colon_index + 1 :]))
        else:
            return HostAddress(address, default_port)

    def __str__(self) -> str:
        return f"{self.host}:{self.port}"


def get_macaddress_from_uuid(uuid: str):
    """Infers the device mac address from the UUID"""
    return ":".join(re.findall("..", uuid[-12:].lower()))


def fmt_macaddress(macaddress: str):
    """internal component macaddress representation (lowercase without dots/colons)"""
    return macaddress.replace(":", "").lower()


def build_message(
    namespace: str,
    method: str,
    payload: MerossPayloadType,
    key: KeyType,
    from_: str,
    messageid: str | None = None,
) -> MerossMessageType:
    if isinstance(key, dict):
        key[mc.KEY_NAMESPACE] = namespace
        key[mc.KEY_METHOD] = method
        key[mc.KEY_PAYLOADVERSION] = 1
        key[mc.KEY_FROM] = from_
        return {mc.KEY_HEADER: key, mc.KEY_PAYLOAD: payload}  # type: ignore
    else:
        messageid = messageid or uuid4().hex
        timestamp = int(time())
        return {
            mc.KEY_HEADER: {
                mc.KEY_MESSAGEID: messageid,
                mc.KEY_NAMESPACE: namespace,
                mc.KEY_METHOD: method,
                mc.KEY_PAYLOADVERSION: 1,
                mc.KEY_FROM: from_,
                # mc.KEY_FROM: "/app/0-0/subscribe",
                # "from": "/appliance/9109182170548290882048e1e9522946/publish",
                mc.KEY_TIMESTAMP: timestamp,
                mc.KEY_TIMESTAMPMS: 0,
                mc.KEY_SIGN: get_message_signature(messageid, key or "", timestamp),
            },
            mc.KEY_PAYLOAD: payload,
        }


def build_message_reply(
    header: MerossHeaderType,
    payload: MerossPayloadType,
) -> MerossMessageType:
    """
    builds a message by replying the full header. This is used
    in replies to some PUSH sent by devices where it appears
    (from meross broker protocol inspection - see #346)
    the broker doesn't calculate a new signature but just replies
    the incoming header data
    """
    header = header.copy()
    header.pop(mc.KEY_UUID, None)
    return {
        mc.KEY_HEADER: header,
        mc.KEY_PAYLOAD: payload,
    }


def get_message_signature(messageid: str, key: str, timestamp):
    return md5(
        "".join((messageid, key, str(timestamp))).encode("utf-8"), usedforsecurity=False
    ).hexdigest()


def get_message_uuid(header: MerossHeaderType):
    return header.get(mc.KEY_UUID) or mc.RE_PATTERN_TOPIC_UUID.match(header[mc.KEY_FROM]).group(1)  # type: ignore


def get_replykey(header: MerossHeaderType, key: KeyType) -> KeyType:
    """
    checks header signature against key:
    if ok return sign itsef else return the full header { "messageId", "timestamp", "sign", ...}
    in order to be able to use it in a reply scheme
    **UPDATE 28-03-2021**
    the 'reply scheme' hack doesnt work on mqtt but works on http: this code will be left since it works if the key is correct
    anyway and could be reused in a future attempt
    """
    if isinstance(key, str):
        sign = get_message_signature(
            header[mc.KEY_MESSAGEID], key, header[mc.KEY_TIMESTAMP]
        )
        if sign == header[mc.KEY_SIGN]:
            return key

    return header


def is_device_online(payload: dict) -> bool:
    try:
        return payload[mc.KEY_ONLINE][mc.KEY_STATUS] == mc.STATUS_ONLINE
    except Exception:
        return False


def get_port_safe(p_dict: dict, key: str) -> int:
    """
    Parses the "firmware" dict in device descriptor (coming from NS_ALL)
    or the "debug" dict and returns the broker port value or what we know
    is the default for Meross.
    """
    try:
        return int(p_dict[key]) or mc.MQTT_DEFAULT_PORT
    except Exception:
        return mc.MQTT_DEFAULT_PORT


def get_active_broker(p_debug: dict):
    """
    Parses the "debug" dict coming from NS_SYSTEM_DEBUG and returns
    current MQTT active broker
    """
    p_cloud = p_debug[mc.KEY_CLOUD]
    active_server: str = p_cloud[mc.KEY_ACTIVESERVER]
    if active_server == p_cloud[mc.KEY_MAINSERVER]:
        return HostAddress(active_server, get_port_safe(p_cloud, mc.KEY_MAINPORT))
    elif active_server == p_cloud[mc.KEY_SECONDSERVER]:
        return HostAddress(active_server, get_port_safe(p_cloud, mc.KEY_SECONDPORT))
    else:
        raise Exception(
            "Unable to detect active MQTT broker from current device debug info"
        )


def get_element_by_key(payload: list, key: str, value: object) -> dict:
    """
    scans the payload(list) looking for the first item matching
    the key value. Usually looking for the matching channel payload
    inside list payloads
    """
    for p in payload:
        if p.get(key) == value:
            return p
    raise KeyError(
        f"No match for key '{key}' on value:'{str(value)}' in {str(payload)}"
    )


def get_element_by_key_safe(payload, key: str, value) -> dict | None:
    """
    scans the payload (expecting a list) looking for the first item matching
    the key value. Usually looking for the matching channel payload
    inside list payloads
    """
    try:
        for p in payload:
            if p.get(key) == value:
                return p
    except Exception:
        return None


def update_dict_strict(dst_dict: dict, src_dict: dict):
    """Updates (merge) the dst_dict with values from src_dict checking
    their existence in dst_dict before applying. Used in emulators to update
    current state when receiving a SET payload. This is needed for testing so
    that we're sure the meross_lan client doesn't pollute the emulator device
    state with wrong or unexpected keys. TODO: we should also add a semantic
    value check to ensure it is valid."""
    for key, value in src_dict.items():
        if key in dst_dict:
            dst_dict[key] = value


def update_dict_strict_by_key(
    dst_lst: list[dict], src_dict: dict, key: str = mc.KEY_CHANNEL
) -> dict:
    """
    Much like get_element_by_key scans the dst list looking for the first item matching
    the key value to the corresponding one in src_dict. Usually looking for the matching
    channel payload inside list payloads. Before returning, merges the src_dict into
    the matched dst_dict
    """
    key_value = src_dict[key]
    for dst_dict in dst_lst:
        if dst_dict.get(key) == key_value:
            update_dict_strict(dst_dict, src_dict)
            return dst_dict
    raise KeyError(f"No match for key '{key}' on value:'{str(key_value)}' in {dst_lst}")


def extract_dict_payloads(payload):
    """
    Helper generator to manage payloads which might carry list of payloads:
    payload = { "channel": 0, "onoff": 1}
    or
    payload = [{ "channel": 0, "onoff": 1}]
    """
    if isinstance(payload, list):
        for p in payload:
            yield p
    elif payload:  # assert isinstance(payload, dict)
        yield payload


def get_productname(producttype: str) -> str:
    for _type, _name in mc.TYPE_NAME_MAP.items():
        if producttype.startswith(_type):
            return _name
    return producttype


def get_productnameuuid(producttype: str, uuid: str) -> str:
    return f"{get_productname(producttype)} ({uuid})"


def get_productnametype(producttype: str) -> str:
    name = get_productname(producttype)
    return f"{name} ({producttype})" if name is not producttype else producttype


def get_subdevice_type(p_subdevice_digest: dict):
    """Parses the subdevice dict from the hub digest to extract the
    specific dict carrying the specialized subdevice info."""
    for p_key, p_value in p_subdevice_digest.items():
        if isinstance(p_value, dict):
            return p_key, p_value
    return None, None


def get_mts_digest(p_subdevice_digest: dict) -> dict | None:
    """Parses the subdevice dict from the hub digest to identify if it's
    an mts-like (and so queried through 'Hub.Mts100.All')."""
    for digest_mts_key in mc.MTS100_ALL_TYPESET:
        # digest for mts valves has the usual fields plus a (sub)dict
        # named according to the model. Here we should find the mode
        if digest_mts_key in p_subdevice_digest:
            return p_subdevice_digest[digest_mts_key]
    return None


def check_message_strict(message: "MerossResponse | None"):
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


class MerossMessage(dict):
    """
    Base (almost) abstract class for different source of messages that
    need to be sent to the device (or received from).
    The actual implementation will setup the slots
    """

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

    def __init__(self, message: dict, json_str: str | None = None):
        self._json_str = json_str
        super().__init__(message)

    def json(self):
        if not self._json_str:
            self._json_str = _json_encoder.encode(self)
        return self._json_str

    @staticmethod
    def decode(json_str: str):
        return MerossMessage(_json_decoder.decode(json_str), json_str)


class MerossResponse(MerossMessage):
    """Helper for messages received from a device"""

    def __init__(self, json_str: str):
        super().__init__(_json_decoder.decode(json_str), json_str)


class MerossRequest(MerossMessage):
    """Helper for messages to be sent"""

    def __init__(
        self,
        key: str,
        namespace: str,
        method: str = mc.METHOD_GET,
        payload: MerossPayloadType | None = None,
        from_: str = mc.MANUFACTURER,
    ):
        self.namespace = namespace
        self.method = method
        self.messageid = uuid4().hex
        if payload is None:
            if method is mc.METHOD_GET:
                self.payload = mn.NAMESPACES[namespace].payload_get
            else:
                assert method is mc.METHOD_PUSH
                self.payload = mn.Namespace.DEFAULT_PUSH_PAYLOAD
        else:
            self.payload = payload
        timestamp = int(time())
        super().__init__(
            {
                mc.KEY_HEADER: {
                    mc.KEY_MESSAGEID: self.messageid,
                    mc.KEY_NAMESPACE: namespace,
                    mc.KEY_METHOD: method,
                    mc.KEY_PAYLOADVERSION: 1,
                    mc.KEY_FROM: from_,
                    mc.KEY_TIMESTAMP: timestamp,
                    mc.KEY_TIMESTAMPMS: 0,
                    mc.KEY_SIGN: get_message_signature(self.messageid, key, timestamp),
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

    def __init__(self, header: MerossHeaderType, payload: MerossPayloadType):
        self.namespace = header[mc.KEY_NAMESPACE]
        self.method = header[mc.KEY_METHOD]
        self.messageid = header[mc.KEY_MESSAGEID]
        self.payload = payload
        header = header.copy()
        header.pop(mc.KEY_UUID, None)
        header[mc.KEY_TRIGGERSRC] = "CloudControl"
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
        self, key: str, header: MerossHeaderType, payload: MerossPayloadType, from_: str
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
                    mc.KEY_FROM: from_,
                    mc.KEY_TRIGGERSRC: "CloudControl",
                    mc.KEY_TIMESTAMP: timestamp,
                    mc.KEY_TIMESTAMPMS: 0,
                    mc.KEY_SIGN: get_message_signature(self.messageid, key, timestamp),
                },
                mc.KEY_PAYLOAD: payload,
            }
        )


class MerossDeviceDescriptor:
    """
    Utility class to extract various info from Appliance.System.All
    device descriptor
    """

    all: dict
    ability: dict
    digest: dict
    control: dict
    system: dict
    hardware: dict
    firmware: dict
    online: dict
    type: str
    subType: str
    hardwareVersion: str
    uuid: str
    macAddress: str
    macAddress_fmt: str
    innerIp: str | None
    userId: str | None
    firmwareVersion: str
    time: dict
    timezone: str | None
    productname: str
    productnametype: str
    productmodel: str

    __slots__ = (
        "payload",
        "all",
        "ability",
        "digest",
        "__dict__",
    )

    _dynamicattrs = {
        mc.KEY_ALL: lambda _self: _self.payload.get(mc.KEY_ALL, {}),
        mc.KEY_ABILITY: lambda _self: _self.payload.get(mc.KEY_ABILITY, {}),
        mc.KEY_DIGEST: lambda _self: _self.all.get(mc.KEY_DIGEST, {}),
        mc.KEY_CONTROL: lambda _self: _self.all.get(mc.KEY_CONTROL, {}),
        mc.KEY_SYSTEM: lambda _self: _self.all.get(mc.KEY_SYSTEM, {}),
        mc.KEY_HARDWARE: lambda _self: _self.system.get(mc.KEY_HARDWARE, {}),
        mc.KEY_FIRMWARE: lambda _self: _self.system.get(mc.KEY_FIRMWARE, {}),
        mc.KEY_ONLINE: lambda _self: _self.system.get(mc.KEY_ONLINE, {}),
        mc.KEY_TYPE: lambda _self: _self.hardware.get(mc.KEY_TYPE, mc.MANUFACTURER),
        mc.KEY_SUBTYPE: lambda _self: _self.hardware.get(mc.KEY_SUBTYPE, ""),
        "hardwareVersion": lambda _self: _self.hardware.get(mc.KEY_VERSION, ""),
        mc.KEY_UUID: lambda _self: _self.hardware.get(mc.KEY_UUID),
        mc.KEY_MACADDRESS: lambda _self: _self.hardware.get(mc.KEY_MACADDRESS, ""),
        "macAddress_fmt": lambda _self: fmt_macaddress(_self.macAddress),
        mc.KEY_INNERIP: lambda _self: _self.firmware.get(mc.KEY_INNERIP),
        mc.KEY_USERID: lambda _self: str(_self.firmware.get(mc.KEY_USERID)),
        "firmwareVersion": lambda _self: _self.firmware.get(mc.KEY_VERSION, ""),
        mc.KEY_TIME: lambda _self: _self.system.get(mc.KEY_TIME, {}),
        mc.KEY_TIMEZONE: lambda _self: _self.time.get(mc.KEY_TIMEZONE),
        "productname": lambda _self: get_productnameuuid(_self.type, _self.uuid),
        "productnametype": lambda _self: get_productnametype(_self.type),
        "productmodel": lambda _self: f"{_self.type} {_self.hardware.get(mc.KEY_VERSION, '')}",
    }

    def __init__(self, payload: dict):
        self.payload = payload

    def __getattr__(self, name):
        value = MerossDeviceDescriptor._dynamicattrs[name](self)
        setattr(self, name, value)
        return value

    def update(self, payload: dict):
        """
        reset the cached pointers
        """
        self.payload |= payload
        for key in MerossDeviceDescriptor._dynamicattrs.keys():
            # don't use hasattr() or so to inspect else the whole
            # dynamic attrs logic gets f...d
            try:
                delattr(self, key)
            except Exception:
                pass

    def update_time(self, p_time: dict):
        self.system[mc.KEY_TIME] |= p_time
        for key in (mc.KEY_TIME, mc.KEY_TIMEZONE):
            try:
                delattr(self, key)
            except Exception:
                pass

    @property
    def main_broker(self) -> HostAddress:
        """list of configured brokers in the device"""
        fw = self.firmware
        return HostAddress(fw[mc.KEY_SERVER], get_port_safe(fw, mc.KEY_PORT))

    @property
    def alt_broker(self) -> HostAddress:
        """list of configured brokers in the device"""
        fw = self.firmware
        return HostAddress(
            fw[mc.KEY_SECONDSERVER], get_port_safe(fw, mc.KEY_SECONDPORT)
        )

    @property
    def brokers(self) -> list[HostAddress]:
        """list of configured brokers in the device"""
        _brokers: list[HostAddress] = []
        fw = self.firmware
        if server := fw.get(mc.KEY_SERVER):
            _brokers.append(HostAddress(server, get_port_safe(fw, mc.KEY_PORT)))
        if second_server := fw.get(mc.KEY_SECONDSERVER):
            if second_server != server:
                _brokers.append(
                    HostAddress(second_server, get_port_safe(fw, mc.KEY_SECONDPORT))
                )
        return _brokers
