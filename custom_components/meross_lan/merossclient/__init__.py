"""
    A collection of utilities to help managing the Meross device protocol
"""
from __future__ import annotations

import asyncio
from hashlib import md5
from time import time
import typing
from uuid import uuid4

from . import const as mc

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
KeyType = typing.Union[MerossHeaderType, str, None]
ResponseCallbackType = typing.Callable[[bool, dict, dict], None]


try:
    import json
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

        cloud_profiles = [
            {
                mc.KEY_USERID_: "10000",
                mc.KEY_EMAIL: "100@meross.com",
                mc.KEY_KEY: "key1",
            },
            {
                mc.KEY_USERID_: "20000",
                mc.KEY_EMAIL: "200@meross.com",
                mc.KEY_KEY: "key2",
            },
            {
                mc.KEY_USERID_: "30000",
                mc.KEY_EMAIL: "300@meross.com",
                mc.KEY_KEY: "key3",
            },
            {
                mc.KEY_USERID_: "40000",
                mc.KEY_EMAIL: "400@meross.com",
                mc.KEY_KEY: "key4",
            },
            {
                mc.KEY_USERID_: "50000",
                mc.KEY_EMAIL: "500@meross.com",
                mc.KEY_KEY: "key5",
            },
        ]

        mqtt_client_log_enable = False

        mqtt_connect_probability = 50

        @staticmethod
        def mqtt_random_connect():
            return randint(0, 99) < MEROSSDEBUG.mqtt_connect_probability

        mqtt_disconnect_probability = 0

        @staticmethod
        def mqtt_random_disconnect():
            return randint(0, 99) < MEROSSDEBUG.mqtt_disconnect_probability

        # MerossHTTPClient debug patching
        http_client_log_enable = True
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

    def __init__(self, response: MerossMessageType):
        super().__init__(response, "Invalid key")


class MerossSignatureError(MerossProtocolError):
    """
    signal a protocol signature error detected
    when validating the received header
    """

    def __init__(self, response: MerossMessageType):
        super().__init__(response, "Signature error")


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
        return {mc.KEY_HEADER: key, mc.KEY_PAYLOAD: payload}
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
    header = header.copy()
    header.pop(mc.KEY_UUID, None)
    header[mc.KEY_TRIGGERSRC] = "CloudControl"
    return {
        mc.KEY_HEADER: header,
        mc.KEY_PAYLOAD: payload,
    }


def get_namespacekey(namespace: str) -> str:
    """
    return the 'well known' key for the provided namespace
    which is used as the root key of the associated payload
    This is usually the camelCase of the last split of the namespace
    """
    if namespace in mc.PAYLOAD_GET:
        return next(iter(mc.PAYLOAD_GET[namespace]))
    key = namespace.split(".")[-1]
    return key[0].lower() + key[1:]


def get_default_payload(namespace: str) -> dict:
    """
    when we query a device 'namespace' with a GET method the request payload
    is usually 'well structured' (more or less). We have a dictionary of
    well-known payloads else we'll use some heuristics
    """
    if namespace in mc.PAYLOAD_GET:
        return mc.PAYLOAD_GET[namespace]
    split = namespace.split(".")
    key = split[-1]
    return {key[0].lower() + key[1:]: [] if split[1] == "Hub" else {}}


def get_default_arguments(namespace: str):
    return namespace, mc.METHOD_GET, get_default_payload(namespace)


def get_message_signature(messageid: str, key: str, timestamp):
    return md5(
        "".join((messageid, key, str(timestamp))).encode("utf-8"), usedforsecurity=False
    ).hexdigest()


def get_replykey(header: MerossHeaderType, key: KeyType = None) -> KeyType:
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


def is_device_online(payload: dict) -> bool:
    try:
        return payload[mc.KEY_ONLINE][mc.KEY_STATUS] == mc.STATUS_ONLINE
    except Exception:
        return False


class MerossDeviceDescriptor:
    """
    Utility class to extract various info from Appliance.System.All
    device descriptor
    """

    all: dict
    ability: dict
    digest: dict
    system: dict
    hardware: dict
    firmware: dict
    type: str
    subType: str
    hardwareVersion: str
    uuid: str
    macAddress: str
    innerIp: str | None
    userId: str | None
    firmwareVersion: str
    time: dict
    timezone: str | None
    productname: str
    productnametype: str
    productmodel: str

    __slots__ = (
        "all",
        "ability",
        "digest",
        "__dict__",
    )

    _dynamicattrs = {
        mc.KEY_SYSTEM: lambda _self: _self.all.get(mc.KEY_SYSTEM, {}),
        mc.KEY_HARDWARE: lambda _self: _self.system.get(mc.KEY_HARDWARE, {}),
        mc.KEY_FIRMWARE: lambda _self: _self.system.get(mc.KEY_FIRMWARE, {}),
        mc.KEY_TYPE: lambda _self: _self.hardware.get(mc.KEY_TYPE, mc.MANUFACTURER),
        mc.KEY_SUBTYPE: lambda _self: _self.hardware.get(mc.KEY_SUBTYPE, ""),
        "hardwareVersion": lambda _self: _self.hardware.get(mc.KEY_VERSION, ""),
        mc.KEY_UUID: lambda _self: _self.hardware.get(mc.KEY_UUID),
        mc.KEY_MACADDRESS: lambda _self: _self.hardware.get(
            mc.KEY_MACADDRESS, mc.MEROSS_MACADDRESS
        ),
        mc.KEY_INNERIP: lambda _self: _self.firmware.get(mc.KEY_INNERIP),
        mc.KEY_USERID: lambda _self: str(_self.firmware.get(mc.KEY_USERID)),
        "firmwareVersion": lambda _self: _self.firmware.get(mc.KEY_VERSION, ""),
        mc.KEY_TIME: lambda _self: _self.system.get(mc.KEY_TIME, {}),
        mc.KEY_TIMEZONE: lambda _self: _self.time.get(mc.KEY_TIMEZONE),
        "productname": lambda _self: get_productnameuuid(_self.type, _self.uuid),
        "productnametype": lambda _self: get_productnametype(_self.type),
        "productmodel": lambda _self: f"{_self.type} {_self.hardware.get(mc.KEY_VERSION, '')}",
    }

    def __init__(self, payload: dict | None):
        if payload is None:
            self.all = {}
            self.ability = {}
            self.digest = {}
        else:
            self.all = payload.get(mc.KEY_ALL, {})
            self.ability = payload.get(mc.KEY_ABILITY, {})
            self.digest = self.all.get(mc.KEY_DIGEST, {})

    def __getattr__(self, name):
        value = MerossDeviceDescriptor._dynamicattrs[name](self)
        setattr(self, name, value)
        return value

    def update(self, payload: dict):
        """
        reset the cached pointers
        """
        self.all = payload.get(mc.KEY_ALL, self.all)
        self.digest = self.all.get(mc.KEY_DIGEST, {})
        for key in MerossDeviceDescriptor._dynamicattrs.keys():
            # don't use hasattr() or so to inspect else the whole
            # dynamic attrs logic gets f...d
            try:
                delattr(self, key)
            except Exception:
                pass

    def update_time(self, p_time: dict):
        self.system[mc.KEY_TIME] = p_time
        self.time = p_time
        self.timezone = p_time.get(mc.KEY_TIMEZONE)
