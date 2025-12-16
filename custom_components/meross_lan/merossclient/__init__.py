"""
A collection of utilities to help managing the Meross device protocol
"""

import asyncio
import re
from time import time
from typing import TYPE_CHECKING

from .protocol import (
    b64decode,
    b64encode,
    compute_wifix_password,
    const as mc,
    namespaces as mn,
)
from .protocol.message import MerossRequest

if TYPE_CHECKING:
    from typing import (
        Any,
        Callable,
        ClassVar,
        Final,
        Generator,
        Iterable,
        Mapping,
        NotRequired,
        Protocol,
        TypedDict,
        Unpack,
    )

    from .protocol.message import MerossResponse
    from .protocol.namespaces import Namespace
    from .protocol.types import JsonDict, JsonList, MerossRequestType
    from .protocol.types.config import WifiList

    class LoggerT(Protocol):
        """Protocol definition for logger-like instances used in the library."""

        def getEffectiveLevel(self) -> int: ...
        def isEnabledFor(self, level: int) -> bool: ...
        def log(self, level: int, msg: str, *args, **kwargs) -> None: ...


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
def get_element_by_key[_T: dict](payload: list[_T], key: str, value) -> _T:
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


def get_element_by_key_safe[_T: dict](payload: list[_T], key: str, value) -> _T | None:
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


def delete_element_by_key(payload: "JsonList", key: str, value):
    """
    Scans the payload(list) removinf (dict) elements whose 'key' matches value.
    """
    for p in tuple(payload):
        try:
            if p[key] == value:
                payload.remove(p)
        except KeyError:
            pass


def merge_dicts(dict1: "Mapping", dict2: "Mapping") -> "Any":
    """
    Recursively merge two dictionaries.
    """
    result = dict(dict1)
    for key, value in dict2.items():
        if (type(value) is dict) and (key in result):
            result_value = result[key]
            if type(result_value) is dict:
                result[key] = merge_dicts(result_value, value)
                continue
        result[key] = value
    return result


def update_dict_strict(dst_dict: dict, src_dict: "Mapping"):
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


def update_dict_strict_by_key[_T: "JsonDict"](
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


def extract_dict_payloads[_T](payload: _T | list[_T]) -> "Iterable[_T]":
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


class HostAddress:
    """
    Helper class to build an host:port representation for broker addresses
    carried in Meross payloads
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


def is_device_online(payload: "JsonDict") -> bool:
    try:
        return payload[mc.KEY_ONLINE][mc.KEY_STATUS] == mc.STATUS_ONLINE
    except Exception:
        return False


def get_port_safe(p_dict: "JsonDict", key: str) -> int:
    """
    Parses the "firmware" dict in device descriptor (coming from NS_ALL)
    or the "debug" dict and returns the broker port value or what we know
    is the default for Meross.
    """
    try:
        return int(p_dict[key]) or mc.MQTT_DEFAULT_PORT
    except Exception:
        return mc.MQTT_DEFAULT_PORT


def get_active_broker(p_debug: "JsonDict"):
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


def get_subdevice_type(p_subdevice_digest: "JsonDict"):
    """Parses the subdevice dict from the hub digest to extract the
    specific dict carrying the specialized subdevice info."""
    for p_key, p_value in p_subdevice_digest.items():
        if isinstance(p_value, dict):
            return p_key, p_value
    return None, None


def get_mts_digest(p_subdevice_digest: "JsonDict") -> "JsonDict | None":
    """Parses the subdevice dict from the hub digest to identify if it's
    an mts-like (and so queried through 'Hub.Mts100.All')."""
    for digest_mts_key in mc.MTS100_ALL_TYPESET:
        # digest for mts valves has the usual fields plus a (sub)dict
        # named according to the model. Here we should find the mode
        if digest_mts_key in p_subdevice_digest:
            return p_subdevice_digest[digest_mts_key]
    return None


class MerossDeviceDescriptor:
    """
    Utility class to extract various info from Appliance.System.All
    device descriptor
    """

    if TYPE_CHECKING:

        DYNAMIC_ATTRS: Final[Mapping[str, Callable[["MerossDeviceDescriptor"], Any]]]

        payload: Final[JsonDict]
        channels: Final[frozenset[int]]
        all: JsonDict
        ability: JsonDict
        digest: JsonDict
        control: JsonDict
        system: JsonDict
        hardware: JsonDict
        firmware: JsonDict
        online: JsonDict
        type: str
        subType: str
        hardwareVersion: str
        uuid: str
        macAddress: str
        macAddress_fmt: str
        innerIp: str | None
        userId: str
        firmwareVersion: str
        time: dict
        timezone: str | None
        productname: str
        productnametype: str
        productmodel: str
        is_refoss: bool

    NO_CHANNEL = frozenset()
    SINGLE_CHANNEL = frozenset({0})
    TYPE_CHANNELS_MAP = {
        # some lookup when digest euristic parsing doesn't work
        "em06": frozenset({1, 2, 3, 4, 5, 6}),
    }

    DYNAMIC_ATTRS = {
        # TODO: use cached_property
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
        "is_refoss": lambda _self: mc.RefossModel.match(_self.type),
    }

    __slots__ = (
        "payload",
        "channels",
        "all",
        "ability",
        "digest",
        "__dict__",
    )

    def __init__(self, payload: "JsonDict"):
        self.payload = payload
        # infer supported channels from device type
        device_type = self.type
        for _type, _channels in MerossDeviceDescriptor.TYPE_CHANNELS_MAP.items():
            if device_type.startswith(_type):
                self.channels = _channels
                break
        else:
            # infer from digest payload
            _channels = set()

            def _search_channels(d: dict):
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
            self.channels = (
                frozenset(_channels) if _channels else MerossDeviceDescriptor.NO_CHANNEL
            )

    def __getattr__(self, name):
        value = MerossDeviceDescriptor.DYNAMIC_ATTRS[name](self)
        setattr(self, name, value)
        return value

    def update(self, payload: "JsonDict"):
        """
        reset the cached pointers
        """
        self.payload.update(payload)
        for key in MerossDeviceDescriptor.DYNAMIC_ATTRS:
            # don't use hasattr() or so to inspect else the whole
            # dynamic attrs logic gets f...d
            try:
                delattr(self, key)
            except Exception:
                pass

    def update_time(self, p_time: "JsonDict"):
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


class _BaseClient:
    """Abstract base client providing common api for different transports (HTTP-MQTT-BT)."""

    if TYPE_CHECKING:

        class Args(TypedDict):
            key: NotRequired[str]
            from_: NotRequired[str]
            trigger_src: NotRequired[str]
            timeout: NotRequired[float]
            descriptor: NotRequired[MerossDeviceDescriptor]
            loop: NotRequired[asyncio.AbstractEventLoop]
            logger: NotRequired[LoggerT | None]

        class RequestArgs(TypedDict):
            timeout: NotRequired[float]

        key: str  # default key used to sign Meross protocol messages
        from_: str  # default value in 'from' header key
        trigger_src: str  # default value in 'triggerSrc' header key
        timeout: float
        descriptor: MerossDeviceDescriptor | None
        logger: LoggerT | None
        loop: Final[asyncio.AbstractEventLoop]

    LOG_DUMP = 5  # logging level for raw messages dumping
    TIMEOUT_DEFAULT = 10

    # Using a 'placeholder' definition to ease including in diamond pattern hierarchies:
    # just add a __slots__ = BaseClient.__SLOTS + (...) in actual classes to actually implement.
    __SLOTS__ = (
        "key",
        "timeout",
        "descriptor",
        "logger",
        "loop",
    )

    @classmethod
    def _calc_slots(cls, *slots: "Unpack[tuple[str, ...]]"):
        _slots = set(slots)
        for _base in cls.__mro__:
            try:
                _slots.update(_base.__SLOTS__)
            except AttributeError:
                pass
        return _slots

    def __init__(self, **kwargs: "Unpack[Args]"):
        self.key = kwargs.pop("key", "")
        self.from_ = kwargs.pop("from_", mc.HEADER_FROM_DEFAULT)
        self.trigger_src = kwargs.pop("trigger_src", self.__class__.__name__)
        self.timeout = kwargs.pop("timeout", self.TIMEOUT_DEFAULT)
        self.descriptor = kwargs.pop("descriptor", None)
        self.logger = kwargs.pop("logger", None)
        self.loop = kwargs.pop("loop", asyncio.get_running_loop())

    async def async_request_raw(
        self, request: MerossRequest, /, **kwargs: "Unpack[RequestArgs]"
    ) -> "MerossResponse":
        raise NotImplementedError("async_request_raw")

    async def async_request(
        self, *args: "Unpack[MerossRequestType]", **kwargs: "Unpack[RequestArgs]"
    ) -> "MerossResponse":
        return await self.async_request_raw(
            MerossRequest(*args, self.key, self.from_, self.trigger_src), **kwargs
        )

    async def async_request_ns(
        self, ns: "Namespace", /, **kwargs: "Unpack[RequestArgs]"
    ) -> "MerossResponse":
        return await self.async_request(*ns.request_default, **kwargs)

    async def async_request_ns_payload(
        self, ns: "Namespace", /, **kwargs: "Unpack[RequestArgs]"
    ) -> "Any":
        return (await self.async_request(*ns.request_default, **kwargs))[
            mc.KEY_PAYLOAD
        ][ns.key]

    async def async_identify(self, *args, **kwargs: "Unpack[RequestArgs]"):
        self.descriptor = MerossDeviceDescriptor(
            (await self.async_request_ns(mn.Appliance_System_All, **kwargs)).check()[
                mc.KEY_PAYLOAD
            ]
            | (
                await self.async_request_ns(mn.Appliance_System_Ability, **kwargs)
            ).check()[mc.KEY_PAYLOAD]
        )
        return self.descriptor

    async def async_get_ssid_scan(
        self,
        *,
        sort_key: str | None = mc.KEY_SIGNAL,
        **kwargs: "Unpack[RequestArgs]",
    ):
        """Returns a 'short-list' of available WiFi SSIDs. The native device scan includes
        multiple bssid(s) while this method only returns unique SSIDs ordered by 'sort-key'.
        sort_key must be a valid dict key available in the native payload
        (see protocol.types.config.Wifi)."""

        p_wifilist: "WifiList" = await self.async_request_ns_payload(
            mn.Appliance_Config_WifiList
        )
        if sort_key:
            p_wifilist = sorted(p_wifilist, key=lambda x: x[sort_key], reverse=True)

        ssid_list: list[str] = []
        for wifi in p_wifilist:
            try:
                # It looks like some ssid b64 encodings are 'weird' and we're unable to decode them as UTF-8
                # strings
                ssid = b64decode(wifi[mc.KEY_SSID]).rstrip(b"\0").decode()
                if ssid not in ssid_list:
                    ssid_list.append(ssid)
            except:
                pass

        return ssid_list

    async def async_configure_mqtt(
        self,
        *,
        host: str = "",
        port: int = mc.MQTT_DEFAULT_PORT,
        key: str = "",
        userid: str = "",  # will default to 0
        **kwargs: "Unpack[RequestArgs]",
    ):
        return await self.async_request(
            mn.Appliance_Config_Key.name,
            mc.METHOD_SET,
            {
                mn.Appliance_Config_Key.key: (
                    {
                        mc.KEY_GATEWAY: {
                            mc.KEY_HOST: host,
                            mc.KEY_PORT: port,
                            mc.KEY_SECONDHOST: host,
                            mc.KEY_SECONDPORT: port,
                        },
                        mc.KEY_KEY: key,
                        mc.KEY_USERID: userid,
                    }
                    if host
                    else {
                        mc.KEY_KEY: key,
                        mc.KEY_USERID: userid,
                    }
                ),
            },
        )

    async def async_configure_wifi(
        self,
        *,
        ssid: str,
        password: str,
        **kwargs: "Unpack[RequestArgs]",
    ):
        # TODO: check if WifiX supported or fallback to Wifi ns
        descriptor = self.descriptor
        assert descriptor, "Device descriptor is not set"
        ns_wifix = mn.Appliance_Config_WifiX
        if ns_wifix.name in descriptor.ability:
            return await self.async_request(
                ns_wifix.name,
                mc.METHOD_SET,
                {
                    ns_wifix.key: {
                        mc.KEY_SSID: b64encode(ssid.encode()).decode(),
                        mc.KEY_PASSWORD: compute_wifix_password(
                            password,
                            descriptor.type,
                            descriptor.uuid,
                            descriptor.macAddress,
                        ),
                    }
                },
            )
        else:
            return await self.async_request(
                mn.Appliance_Config_Wifi.name,
                mc.METHOD_SET,
                {
                    mn.Appliance_Config_Wifi.key: {
                        mc.KEY_SSID: b64encode(ssid.encode()).decode(),
                        mc.KEY_PASSWORD: b64encode(password.encode()).decode(),
                    }
                },
            )

    async def async_configure(
        self,
        *,
        mqtt_host: str = "",
        mqtt_port: int = 8883,
        key: str = "",
        userid: str = "",
        wifi_ssid: str = "",
        wifi_password: str = "",
        **kwargs: "Unpack[RequestArgs]",
    ):
        if wifi_ssid:
            assert self.descriptor, "Device descriptor is not set"
            assert wifi_password, "Wifi password is required if ssid is set"

        if mqtt_host or key or userid:
            await self.async_configure_mqtt(
                host=mqtt_host, port=mqtt_port, key=key, userid=userid, **kwargs
            )

        if wifi_ssid:
            await self.async_configure_wifi(
                ssid=wifi_ssid, password=wifi_password, **kwargs
            )
