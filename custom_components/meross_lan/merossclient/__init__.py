"""
A collection of utilities to help managing the Meross device protocol
"""

import asyncio
from dataclasses import dataclass
import json
import re
from time import time
from typing import TYPE_CHECKING
from uuid import uuid4

from .protocol import const as mc

if TYPE_CHECKING:
    from typing import Any, Iterable

    from protocol.message import MerossResponse
    from protocol.types import MerossRequestType

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


#
# General purpose utilities for payload handling
#
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
            dst_value = dst_dict[key]
            dst_type = type(dst_value)
            src_type = type(value)
            if dst_type is dict:
                if src_type is dict:
                    update_dict_strict(dst_value, value)
            elif dst_type is list:
                if src_type is list:
                    dst_dict[key] = value  # lists ?!
            else:
                dst_dict[key] = value


def update_dict_strict_by_key[_T: "dict[str, Any]"](
    dst_lst: "Iterable[_T]", src_dict: _T, key: str = mc.KEY_CHANNEL
) -> _T:
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


@dataclass
class HostAddress:
    """
    Helper class to build an host:port representation for broker addresses
    carried in Meross payloads
    """

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


#
#
#
class MerossDeviceDescriptor:
    """
    Utility class to extract various info from Appliance.System.All
    device descriptor
    """

    if TYPE_CHECKING:
        all: dict[str, Any]
        ability: dict[str, Any]
        digest: dict[str, Any]
        control: dict[str, Any]
        system: dict[str, Any]
        hardware: dict[str, Any]
        firmware: dict[str, Any]
        online: dict[str, Any]
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
