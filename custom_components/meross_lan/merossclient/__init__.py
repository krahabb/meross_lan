"""
A collection of utilities to help managing the Meross device protocol
"""

import asyncio
from datetime import UTC, datetime
import importlib
import re
from time import gmtime, time
from typing import TYPE_CHECKING
import zoneinfo

from .protocol import const as mc, namespaces as mn

if TYPE_CHECKING:
    from datetime import tzinfo
    from types import ModuleType
    from typing import (
        Any,
        Callable,
        ClassVar,
        Container,
        Final,
        Generator,
        Iterable,
        Mapping,
        MutableSequence,
        NotRequired,
        Protocol,
        Sequence,
        TypedDict,
        Unpack,
    )

    from cloudapi import LatestVersionType

    from .protocol import types as mt

    _ASYNC_LOCK: Final[asyncio.Lock]
    _AVAILABLE_TIMEZONES: Final[list[str]]
    _ZONEINFO: Final[dict[str, zoneinfo.ZoneInfo]]
    _IMPORT_MODULE_CACHE: Final[dict[str, ModuleType]]

try:
    import json
    from random import randint

    class MEROSSDEBUG:
        # this will raise an OSError on non-dev machines missing the
        # debug configuration so the MEROSSDEBUG symbol will be invalidated
        data: dict = json.load(
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
# General purpose utilities for payload handling
#
def get_element_by_key[_T: "Mapping"](payload: list[_T], key: str, value) -> _T:
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


def get_element_by_key_safe[_T: "Mapping"](
    payload: list[_T], key: str, value
) -> _T | None:
    """
    scans the payload (expecting a list) looking for the first item matching
    the key value. Usually looking for the matching channel payload
    inside list payloads
    """
    for p in payload:
        try:
            if p[key] == value:
                return p
        except KeyError:
            continue
    return None


def delete_element_by_key(payload: list, key: str, value):
    """
    Scans the payload(list) removing (dict) elements whose 'key' matches value.
    """
    for p in tuple(payload):
        try:
            if p[key] == value:
                payload.remove(p)
        except KeyError:
            pass


def _meross_payload_merge_hasher(item):
    """
    This is a default hasher function used to identify items in lists
    representing indexed payloads like channel dicts, effects, triggers.
    It is used in combination with merge_lists (and merge_dicts) to recursively
    merge lists of dicts where each dict is identified by some 'indexing' key
    like 'channel' or 'id'.
    """
    # _hasher is used to check for list items equality in order to merge 2 lists
    item_type = type(item)
    if item_type is dict:
        # we're interested in merging dicts representing channels/indexed payloads
        # so we just brute-force use the 'channel' key if present
        try:
            # most commonly used key
            channel = item[mc.KEY_CHANNEL]
            # there might also be a 'subId' key used for indexing
            try:
                return (item[mc.KEY_SUBID], channel)
            except KeyError:
                return channel
        except KeyError:
            pass
        try:
            # hub subdevices payloads
            return item[mc.KEY_ID]
        except KeyError:
            pass

        try:
            # other indexed payloads like effects, triggers and the likes
            return item[mc.KEY_ID_]
        except KeyError:
            pass

        try:
            sub_item: tuple[str, Any] = next(iter(item.items()))
            return ":".join(
                (sub_item[0], str(_meross_payload_merge_hasher(sub_item[1])))
            )
        except:
            pass
        return id(mn.EMPTY_DICT)

    elif item_type is list:
        # might be whatever list of items.
        # This happens when a list is embedded in a list.
        # we assume this to be a very rare case not useful anyway.
        # but we need to be careful since the merging could 'explode' our
        # data structure if we don't handle this properly.
        # Also, the concept of the hasher function prevents us from
        # merging list of dicts embedded in lists anyway.
        try:
            return _meross_payload_merge_hasher(item[0])
        except IndexError:
            return id(mn.EMPTY_LIST)

    else:
        return item


def merge_dicts[_T: dict](
    original: _T, update: "Mapping", hasher: "Callable" = _meross_payload_merge_hasher
) -> _T:
    """
    Recursively merge two dictionaries with keys from 'update'
    overwriting those in 'original'. The original dict is modified
    in place and returned for convenience.
    """
    for update_key, update_value in update.items():
        try:
            original_value = original[update_key]
            original_type = type(original_value)
            if original_type == type(update_value):
                if original_type is dict:
                    original[update_key] = merge_dicts(
                        original_value, update_value, hasher
                    )
                    continue
                elif original_type is list:
                    original[update_key] = merge_lists(
                        original_value, update_value, hasher
                    )
                    continue
        except KeyError:
            pass

        original[update_key] = update_value
    return original


def merge_lists(
    original: "Iterable",
    update: "Iterable",
    hasher: "Callable" = _meross_payload_merge_hasher,
) -> list:
    """
    Recursively merge two lists based on a hasher function used to identify which
    items in the lists are to be considered the same and thus merged. The original list is not modified.
    This is mainly intended to merge list of dicts where each dict is also recursively merged based on
    some generic key matching as defined in hasher.
    Typical example is a list of channel payloads where each dict in the list has a 'channel' key.
    """
    try:
        return [
            value
            for value in merge_dicts(
                {hasher(item): item for item in original},
                {hasher(item): item for item in update},
                hasher,
            ).values()
        ]
    except TypeError:
        return list(update)


def update_dict_strict(dst_dict: "mt.JsonDict | Any", src_dict: "mt.JsonMapping"):
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
            if dst_type is type(value):
                if dst_type is dict:
                    update_dict_strict(dst_value, value)
                else:
                    dst_dict[key] = value


def update_dict_strict_by_key[_T: "mt.JsonMapping"](
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


def extract_dict_payloads[_T](payload: "_T | Sequence[_T]") -> "Iterable[_T]":
    """
    Helper generator to manage payloads which might carry list of payloads:
    payload = { "channel": 0, "onoff": 1}
    or
    payload = [{ "channel": 0, "onoff": 1}]
    """
    if type(payload) is list:
        for p in payload:
            yield p
    elif payload:  # assert isinstance(payload, dict)
        yield payload  # type: ignore


def datetime_from_epoch(epoch, tz: "tzinfo | None"):
    """
    converts an epoch (UTC seconds) in a datetime.
    Faster than datetime.fromtimestamp with less checks
    and no care for milliseconds.
    If tz is None it'll return a naive datetime in UTC coordinates
    """
    y, m, d, hh, mm, ss, weekday, jday, dst = gmtime(epoch)
    if tz is UTC:
        return datetime(y, m, d, hh, mm, min(ss, 59), 0, tz)
    elif tz is None:
        return datetime(y, m, d, hh, mm, min(ss, 59), 0, UTC).replace(tzinfo=None)
    else:
        return datetime(y, m, d, hh, mm, min(ss, 59), 0, UTC).astimezone(tz)


def simple_slug(value: str):
    """A very simple slugify implementation to avoid the overhead of a full slugify library.
    This is intended to work on slugification of meross symbols appearing in protocol payloads.
    """
    return value.lower().replace(".", "_")


def versiontuple(version: str) -> "mt.VersionTupleType":
    """
    Splits a version string like "1.2.3" into a tuple of integers (1,2,3)
    """
    return tuple(map(int, version.split(".")))


_ASYNC_LOCK = asyncio.Lock()
_AVAILABLE_TIMEZONES = []


async def async_available_timezones():
    if _AVAILABLE_TIMEZONES:
        return _AVAILABLE_TIMEZONES

    def _load():
        """
        These functions will use low levels imports and HA core 2024.5
        complains about executing it in the main loop thread. We'll
        so run these in an executor
        """
        return sorted(zoneinfo.available_timezones())

    async with _ASYNC_LOCK:
        if not _AVAILABLE_TIMEZONES:
            _AVAILABLE_TIMEZONES.extend(
                await asyncio.get_event_loop().run_in_executor(None, _load)
            )

    return _AVAILABLE_TIMEZONES


_ZONEINFO = {}


async def async_load_zoneinfo(key: str, /):
    """
    Creates a ZoneInfo instance from an executor.
    HA core 2024.5 might complain if ZoneInfo needs to load files (no cache hit)
    so we have to always demand this to an executor because the 'decision' to
    load is embedded inside the ZoneInfo initialization.
    A bit cumbersome though..
    """
    try:
        return _ZONEINFO[key]
    except KeyError:
        _ZONEINFO[key] = tz = await asyncio.get_event_loop().run_in_executor(
            None,
            zoneinfo.ZoneInfo,
            key,
        )
        return tz


_IMPORT_MODULE_CACHE = {}


async def async_import_module(name: str, package="merossclient", /):
    f_name = f"{package}.{name}"
    try:
        return _IMPORT_MODULE_CACHE[f_name]
    except KeyError:
        async with _ASYNC_LOCK:
            # check (again) the module was not asyncronously loaded when waiting the lock
            try:
                return _IMPORT_MODULE_CACHE[f_name]
            except KeyError:
                module = await asyncio.get_event_loop().run_in_executor(
                    None, importlib.import_module, name, package
                )
                _IMPORT_MODULE_CACHE[f_name] = module
                return module


class HostAddress:
    """
    Helper class to build an host:port representation for broker addresses
    carried in Meross payloads. TODO: add helper to build from firmware dicts
    in device descriptors.
    """

    host: str
    port: int

    __slots__ = (
        "host",
        "port",
    )

    @staticmethod
    def build(address: str, default_port=mc.MQTT_DEFAULT_PORT):
        """Splits the eventual :port suffix from domain and return (host, port)"""
        if (colon_index := address.find(":")) != -1:
            return HostAddress(address[0:colon_index], int(address[colon_index + 1 :]))
        else:
            return HostAddress(address, default_port)

    def __init__(self, host: str, port: int):
        self.host = host
        self.port = port

    def __hash__(self):
        return hash((self.host, self.port))

    def __eq__(self, value):
        return (
            isinstance(value, HostAddress)
            and (self.host == value.host)
            and (self.port == value.port)
        )

    def __str__(self) -> str:
        return f"{self.host}:{self.port}"


def get_macaddress_from_uuid(uuid: str):
    """Infers the device mac address from the UUID"""
    return ":".join(re.findall("..", uuid[-12:].lower()))


def fmt_macaddress(macaddress: str):
    """internal component macaddress representation (lowercase without dots/colons)"""
    return macaddress.replace(":", "").lower()


def is_device_online(payload: "mt.JsonMapping") -> bool:
    try:
        return payload[mc.KEY_ONLINE][mc.KEY_STATUS] == mc.STATUS_ONLINE
    except Exception:
        return False


def get_port_safe(p_dict: "mt.JsonMapping", key: str) -> int:
    """
    Parses the "firmware" dict in device descriptor (coming from NS_ALL)
    or the "debug" dict and returns the broker port value or what we know
    is the default for Meross.
    """
    try:
        return int(p_dict[key]) or mc.MQTT_DEFAULT_PORT
    except Exception:
        return mc.MQTT_DEFAULT_PORT


def get_active_broker(p_debug: "mt.JsonMapping"):
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


def get_subdevice_key_digest(digest: "mt.JsonMapping") -> str:
    """Parses the subdevice dict from the hub digest to identify it's 'type'.
    Raises StopIteration if unable to find a valid digest key."""
    return (
        p_key for p_key, p_value in digest.items() if type(p_value) is dict
    ).__next__()


class DeviceDescriptor:
    """
    Utility class to extract various info from Appliance.System.All/Ability
    device descriptor
    """

    if TYPE_CHECKING:

        DYNAMIC_ATTRS: Final[Mapping[str, Callable[["DeviceDescriptor"], Any]]]

        payload: Final[mt.JsonDict]
        channels: Final[Sequence[int]]
        # cached accessors to native keys in Appliance.System.All payload
        all: mt.system.All
        ability: mt.JsonMapping
        digest: mt.system.All_Digest
        control: mt.system.All_Control
        system: mt.system.All_System
        hardware: mt.system.Hardware
        firmware: mt.system.Firmware
        online: mt.system.Online
        type: str
        subType: str
        hardwareVersion: str
        uuid: str
        macAddress: str
        macAddress_fmt: str
        innerIp: str | None
        userId: str
        firmwareVersion: str
        time: mt.system.Time
        timezone: str | None
        is_hub: bool
        subdevices: list[mt.hub.Digest_SubDevice] | None
        server: Final[HostAddress]  # type: ignore
        secondServer: Final[HostAddress]  # type: ignore
        # computed cached helpers
        productname: str
        productnametype: str
        productmodel: str
        type_subtype: tuple[str, str]
        is_refoss: bool
        firmware_version: mt.VersionTupleType
        # devices with additional mcu firmware
        mcu: mt.mcu.Firmware | mt.JsonDict | None

    SINGLE_CHANNEL = (0,)
    TYPE_CHANNELS_MAP = {
        # some lookup when digest euristic parsing doesn't work
        mc.RefossModel.em06.name: (1, 2, 3, 4, 5, 6),
        mc.TYPE_HP110A: SINGLE_CHANNEL,  # Mp3 player/light device (Smart Cherub)
        mc.TYPE_MFC100: (
            2,
        ),  # This device is tricky since it exposes features on different channels
        "mrs100": SINGLE_CHANNEL,
        mc.TYPE_MS600: SINGLE_CHANNEL,
        "mts300": SINGLE_CHANNEL,
    }

    DYNAMIC_ATTRS = {
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
        "is_hub": lambda _self: mc.KEY_HUB in _self.digest,
        "subdevices": lambda _self: (
            _self.digest[mc.KEY_HUB][mc.KEY_SUBDEVICE] if _self.is_hub else None
        ),
        "server": lambda _self: HostAddress(
            _self.firmware[mc.KEY_SERVER], get_port_safe(_self.firmware, mc.KEY_PORT)
        ),
        "secondServer": lambda _self: HostAddress(
            _self.firmware[mc.KEY_SECONDSERVER],
            get_port_safe(_self.firmware, mc.KEY_SECONDPORT),
        ),
        "productname": lambda _self: get_productname(_self.type),
        "productnametype": lambda _self: get_productnametype(_self.type),
        "productmodel": lambda _self: f"{_self.type} {_self.hardware.get(mc.KEY_VERSION, '')}",
        "type_subtype": lambda _self: (_self.type, _self.subType),
        "is_refoss": lambda _self: mc.RefossModel.match(_self.type),
        "firmware_version": lambda _self: versiontuple(_self.firmwareVersion),
    }

    __slots__ = (
        "payload",
        "channels",
        "all",
        "ability",
        "mcu",
        "digest",
        "__dict__",
    )

    def __init__(self, payload: "mt.JsonDict"):
        self.payload = payload
        # infer supported channels from device type
        device_type = self.type
        for _type, _channels in DeviceDescriptor.TYPE_CHANNELS_MAP.items():
            if device_type.startswith(_type):
                self.channels = _channels
                break
        else:
            # infer from digest payload
            _channels = set()

            def _search_channels(d: "mt.JsonMapping"):
                try:
                    channel = d[mc.KEY_CHANNEL]
                    if isinstance(channel, int):
                        _channels.add(channel)
                except Exception:
                    for _value in d.values():
                        _value_type = type(_value)
                        if _value_type is dict:
                            _search_channels(_value)
                        elif _value_type is list:
                            for item in _value:
                                if type(item) is dict:
                                    _search_channels(item)

            _search_channels(self.digest)
            self.channels = tuple(_channels)

        # mcu firmware info need to be filled at runtime when/if needed by querying
        # firmware namespaces. When 'None' it means we have no mcu upgrade needs while
        # an empty dict means the device has mcu firmware but we don't know its version yet.
        if (mn.Appliance_Mcu_Firmware in self.ability) or (
            mn.Appliance_Mcu_Hp110_Firmware in self.ability
        ):
            self.mcu = {}
        else:
            self.mcu = None

    def __getattr__(self, name):
        value = DeviceDescriptor.DYNAMIC_ATTRS[name](self)
        setattr(self, name, value)
        return value

    def update(self, payload: "mt.JsonDict"):
        """
        reset the cached pointers
        """
        self.payload.update(payload)
        for key in DeviceDescriptor.DYNAMIC_ATTRS:
            # don't use hasattr() or so to inspect else the whole
            # dynamic attrs logic gets f...d
            try:
                delattr(self, key)
            except Exception:
                pass

    def update_time(self, p_time: "mt.system.Time"):
        self.system[mc.KEY_TIME] = p_time
        for key in (mc.KEY_TIME, mc.KEY_TIMEZONE):
            try:
                delattr(self, key)
            except Exception:
                pass

    @property
    def servers(self):
        """list of configured brokers in the device"""
        _servers: list[HostAddress] = []
        try:
            _servers.append(self.server)
            if self.secondServer != _servers[0]:
                _servers.append(self.secondServer)
        except KeyError:
            pass
        return _servers

    def build_upgrade_payload(
        self,
        latest_version: "LatestVersionType",
        /,
    ) -> "mt.control.Upgrade":
        assert (
            self.type == latest_version[mc.KEY_TYPE]
            and self.subType == latest_version[mc.KEY_SUBTYPE]
        )
        upgrade_payload: "mt.control.Upgrade" = {}
        if versiontuple(latest_version[mc.KEY_VERSION]) > self.firmware_version:
            upgrade_payload[mc.KEY_URL] = latest_version[mc.KEY_URL]
            upgrade_payload[mc.KEY_MD5] = latest_version[mc.KEY_MD5]
        if self.mcu is not None:
            mcu_latest_version = latest_version[mc.KEY_MCU][0]
            if versiontuple(mcu_latest_version[mc.KEY_VERSION]) > versiontuple(self.mcu[mc.KEY_VERSION]):  # type: ignore
                upgrade_payload[mc.KEY_MCU] = [
                    {
                        mc.KEY_TYPE: mcu_latest_version[mc.KEY_TYPE],
                        mc.KEY_URL: mcu_latest_version[mc.KEY_URL],
                        mc.KEY_MD5: mcu_latest_version[mc.KEY_MD5],
                    }
                ]

        return upgrade_payload
