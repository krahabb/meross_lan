import asyncio
from enum import StrEnum
from typing import TYPE_CHECKING

from .. import DeviceDescriptor, logging
from ..protocol import (
    b64decode,
    b64encode,
    compute_wifix_password,
    const as mc,
    namespaces as mn,
)
from ..protocol.message import MerossRequest

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

    from cloudapi import LatestVersionType

    from ..logging import LoggerType
    from ..protocol.message import MerossResponse
    from ..protocol.namespaces import Namespace
    from ..protocol.types import (
        JsonDict,
        JsonList,
        JsonMapping,
        MerossRequestType,
        VersionTupleType,
        config as mt_cf,
        control as mt_c,
        hub as mt_h,
        mcu as mt_m,
    )


class Transport(StrEnum):
    AUTO = "auto"
    MQTT = "mqtt"
    HTTP = "http"
    BLUETOOTH = "bluetooth"

    @staticmethod
    def from_str(label: str) -> "Transport":
        label = label.lower()
        for transport in Transport:
            if transport.value == label:
                return transport
        return Transport.AUTO


class AbstractClient(logging.Loggable):
    """Abstract base client providing common api for different transports (HTTP-MQTT-BT)."""

    if TYPE_CHECKING:

        class Args(logging.Loggable.Args):
            key: NotRequired[str]
            from_: NotRequired[str]
            trigger_src: NotRequired[str]
            descriptor: NotRequired[DeviceDescriptor]
            timeout: NotRequired[float]
            loop: NotRequired[asyncio.AbstractEventLoop]

        class RequestArgs(TypedDict):
            timeout: NotRequired[float]

        TRANSPORT: Final[Transport]

        key: str  # default key used to sign Meross protocol messages
        from_: str  # default value in 'from' header key
        trigger_src: str  # default value in 'triggerSrc' header key
        descriptor: DeviceDescriptor | None
        timeout: float
        loop: Final[asyncio.AbstractEventLoop]

    Transport = Transport
    TRANSPORT = Transport.AUTO

    TIMEOUT = 10

    # Using a 'placeholder' definition to ease including in diamond pattern hierarchies:
    # just add a __slots__ = cls._calc_slots(...) in actual classes to actually implement.
    __SLOTS__ = (
        "key",
        "from_",
        "trigger_src",
        "descriptor",
        "timeout",
        "loop",
    )

    def __init__(
        self, id, parent: "LoggerType | None" = None, /, **kwargs: "Unpack[Args]"
    ):
        self.key = kwargs.pop("key", mc.EMPTY_KEY)
        self.from_ = kwargs.pop("from_", mc.HEADER_FROM_DEFAULT)
        self.trigger_src = kwargs.pop("trigger_src", self.__class__.__name__)
        self.descriptor = kwargs.pop("descriptor", None)
        self.timeout = kwargs.pop("timeout", self.TIMEOUT)
        self.loop = kwargs.pop("loop", asyncio.get_running_loop())
        super().__init__(id, parent, **kwargs)

    @logging.abc.abstractmethod
    async def async_request_raw(
        self, request: MerossRequest, /, **kwargs: "Unpack[RequestArgs]"
    ) -> "MerossResponse":
        """Low level request sending/receiving method to be implemented by
        transport-specific implementations."""
        ...

    async def async_request(
        self, *args: "Unpack[MerossRequestType]", **kwargs: "Unpack[RequestArgs]"
    ) -> "MerossResponse":
        return await self.async_request_raw(
            MerossRequest(*args, self.key, self.from_, self.trigger_src), **kwargs
        )

    async def async_request_ns_payload(
        self, ns: "Namespace", /, **kwargs: "Unpack[RequestArgs]"
    ) -> "Any":
        return (
            (await self.async_request(*ns.request_default, **kwargs))
            .check()
            .payload[ns.key]
        )

    async def async_identify(self, *args, **kwargs: "Unpack[RequestArgs]"):
        self.descriptor = DeviceDescriptor(
            (
                await self.async_request(
                    *mn.Appliance_System_All.request_default, **kwargs
                )
            )
            .check()
            .payload
            | (
                await self.async_request(
                    *mn.Appliance_System_Ability.request_default, **kwargs
                )
            )
            .check()
            .payload
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

        p_wifilist: "mt_cf.WifiList" = await self.async_request_ns_payload(
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
            mn.Appliance_Config_Key,
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
        ns = mn.Appliance_Config_WifiX
        if (descriptor := self.descriptor) and (ns in descriptor.ability):
            password = compute_wifix_password(
                password,
                descriptor.type,
                descriptor.uuid,
                descriptor.macAddress,
            )
        else:
            ns = mn.Appliance_Config_Wifi
            password = b64encode(password.encode()).decode()

        return await self.async_request(
            ns,
            mc.METHOD_SET,
            {
                ns.key: {
                    mc.KEY_SSID: b64encode(ssid.encode()).decode(),
                    mc.KEY_PASSWORD: password,
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
            assert wifi_password, "Wifi password is required if ssid is set"

        if mqtt_host or key or userid:
            await self.async_configure_mqtt(
                host=mqtt_host, port=mqtt_port, key=key, userid=userid, **kwargs
            )

        if wifi_ssid:
            await self.async_configure_wifi(
                ssid=wifi_ssid, password=wifi_password, **kwargs
            )
