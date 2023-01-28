"""
    A collection of utilities to help managing the Meross device protocol
"""
from typing import Union
from uuid import uuid4
from hashlib import md5
from time import time

from . import const as mc


KeyType = Union[dict, str, None]

class MerossProtocolError(Exception):
    """
    signal a protocol error like:
    - missing header keys
    - application layer ERROR(s)

    reason is an error payload (dict) if the protocol is formally correct
    and the device replied us with "method" : "ERROR"
    and "payload" : { "error": { "code": (int), "detail": (str) } }
    in a more general case it could be the exception raised by accessing missing
    fields or a "signature error" in our validation
    """
    def __init__(self, reason):
        super().__init__()
        self.reason = reason


class MerossKeyError(MerossProtocolError):
    """
    signal a protocol key error (wrong key)
    reported by device
    """

class MerossSignatureError(MerossProtocolError):
    """
    signal a protocol signature error detected
    when validating the received header
    """
    def __init__(self):
        super().__init__("Signature error")


def build_payload(
    namespace:str,
    method:str,
    payload:dict,
    key:KeyType,
    from_:str,
    messageid:str | None = None
)-> dict:
    if isinstance(key, dict):
        key[mc.KEY_NAMESPACE] = namespace
        key[mc.KEY_METHOD] = method
        key[mc.KEY_PAYLOADVERSION] = 1
        key[mc.KEY_FROM] = from_
        return {
            mc.KEY_HEADER: key,
            mc.KEY_PAYLOAD: payload
        }
    else:
        if messageid is None:
            messageid = uuid4().hex
        timestamp = int(time())
        return {
            mc.KEY_HEADER: {
                mc.KEY_MESSAGEID: messageid,
                mc.KEY_NAMESPACE: namespace,
                mc.KEY_METHOD: method,
                mc.KEY_PAYLOADVERSION: 1,
                mc.KEY_FROM: from_,
                #mc.KEY_FROM: "/app/0-0/subscribe",
                #"from": "/appliance/9109182170548290882048e1e9522946/publish",
                mc.KEY_TIMESTAMP: timestamp,
                mc.KEY_TIMESTAMPMS: 0,
                mc.KEY_SIGN: md5((messageid + (key or "") + str(timestamp)).encode('utf-8')).hexdigest()
            },
            mc.KEY_PAYLOAD: payload
        }

def get_namespacekey(namespace: str) -> str:
    """
    return the 'well known' key for the provided namespace
    which is used as the root key of the associated payload
    This is usually the camelCase of the last split of the namespace
    """
    if namespace in mc.PAYLOAD_GET:
        return next(iter(mc.PAYLOAD_GET[namespace]))
    key = namespace.split('.')[-1]
    return key[0].lower() + key[1:]

def build_default_payload_get(namespace: str) -> dict:
    """
    when we query a device 'namespace' with a GET method the request payload
    is usually 'well structured' (more or less). We have a dictionary of
    well-known payloads else we'll use some heuristics
    """
    if namespace in mc.PAYLOAD_GET:
        return mc.PAYLOAD_GET[namespace]
    split = namespace.split('.')
    key = split[-1]
    return { key[0].lower() + key[1:]: [] if split[1] == 'Hub' else {} }

def get_replykey(header: dict, key:KeyType = None) -> KeyType:
    """
    checks header signature against key:
    if ok return sign itsef else return the full header { "messageId", "timestamp", "sign", ...}
    in order to be able to use it in a reply scheme
    **UPDATE 28-03-2021**
    the 'reply scheme' hack doesnt work on mqtt but works on http: this code will be left since it works if the key is correct
    anyway and could be reused in a future attempt
    """
    if isinstance(key, str):
        sign = md5((header[mc.KEY_MESSAGEID] + key + str(header[mc.KEY_TIMESTAMP])).encode('utf-8')).hexdigest()
        if sign == header[mc.KEY_SIGN]:
            return key

    return header

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


class MerossDeviceDescriptor:
    """
        Utility class to extract various info from Appliance.System.All
        device descriptor
    """
    all = {}
    ability: dict
    digest: dict
    time: dict | None
    timezone: str | None

    _dynamicattrs = {
        mc.KEY_SYSTEM: lambda _self: _self.all.get(mc.KEY_SYSTEM, {}),
        mc.KEY_HARDWARE: lambda _self: _self.system.get(mc.KEY_HARDWARE, {}),
        mc.KEY_FIRMWARE: lambda _self: _self.system.get(mc.KEY_FIRMWARE, {}),
        mc.KEY_TYPE: lambda _self: _self.hardware.get(mc.KEY_TYPE, mc.MANUFACTURER),
        mc.KEY_UUID: lambda _self: _self.hardware.get(mc.KEY_UUID),
        mc.KEY_MACADDRESS: lambda _self: _self.hardware.get(mc.KEY_MACADDRESS, mc.MEROSS_MACADDRESS),
        mc.KEY_INNERIP: lambda _self: _self.firmware.get(mc.KEY_INNERIP),
        mc.KEY_TIME: lambda _self: _self.system.get(mc.KEY_TIME, {}),
        mc.KEY_TIMEZONE: lambda _self: _self.time.get(mc.KEY_TIMEZONE),
        'productname': lambda _self: get_productnameuuid(_self.type, _self.uuid),
        'productmodel': lambda _self: f"{_self.type} {_self.hardware.get(mc.KEY_VERSION, '')}"
    }

    def __init__(self, payload: dict):
        self.ability = payload.get(mc.KEY_ABILITY, {})
        self.update(payload)

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
            try:
                delattr(self, key)
            except Exception:
                continue

    def update_time(self, p_time: dict):
        self.system[mc.KEY_TIME] = p_time
        self.time = p_time
        self.timezone = p_time.get(mc.KEY_TIMEZONE)
