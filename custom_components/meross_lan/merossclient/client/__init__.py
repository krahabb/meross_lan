from enum import StrEnum
from typing import TYPE_CHECKING

from .. import DeviceDescriptor, logging
from ..protocol import (
    a2b_base64,
    b2a_base64,
    compute_wifix_password,
    const as mc,
    namespaces as mn,
)
from ..protocol.message import MerossRequest, MerossResponse

if TYPE_CHECKING:
    from types import CoroutineType
    from typing import (
        Any,
        Callable,
        ClassVar,
        Coroutine,
        Final,
        Generator,
        Iterable,
        Mapping,
        NotRequired,
        Protocol,
        Self,
        TypedDict,
        Unpack,
    )

    from cloudapi import LatestVersionType

    from ..device import Device
    from ..logging import LoggerType
    from ..protocol.message import MerossMessage
    from ..protocol.namespaces import Namespace
    from ..protocol.types import (
        JsonDict,
        JsonList,
        JsonMapping,
        MerossPayloadType,
        MerossRequestType,
        VersionTupleType,
        config as mt_cf,
        control as mt_c,
        hub as mt_h,
        mcu as mt_m,
    )


class Direction(StrEnum):
    RX = "RX"
    TX = "TX"


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

        class Broadcast[_T, *_argsT](logging.Loggable.Broadcast[_T, *_argsT]):
            pass

        class Args(logging.Loggable.Args):
            key: NotRequired[str]
            from_: NotRequired[str]
            trigger_src: NotRequired[str]
            descriptor: NotRequired[DeviceDescriptor]
            timeout: NotRequired[float]

        class ConnectArgs(TypedDict):
            timeout: NotRequired[float]

        class RequestRawArgs(TypedDict):
            timeout: NotRequired[float]
            uuid: NotRequired[str]

        class RequestArgs(RequestRawArgs):
            key: NotRequired[str]
            from_: NotRequired[str]
            trigger_src: NotRequired[str]

        type AsyncRequestFunc = Callable[
            [str, str, MerossPayloadType], CoroutineType[Any, Any, MerossMessage]
        ]

        class ConfigureMQTTArgs(RequestArgs):
            host: NotRequired[str]  # doesnt set broker if missing/empty
            port: NotRequired[int]  # default: mc.MQTT_DEFAULT_PORT
            new_key: NotRequired[str]  # default: actually configured key
            userid: NotRequired[str]  # default: 0

        class ConfigureWifiArgs(RequestArgs):
            ssid: str
            password: str

        class ConfigureArgs(ConfigureMQTTArgs, ConfigureWifiArgs):
            pass

        TRANSPORT: Final[Transport]

        # defaults for message construction
        key: str
        from_: str
        trigger_src: str

        descriptor: DeviceDescriptor | None
        timeout: float

        is_connected: Final[bool]
        last_tx_message: MerossMessage | None
        last_tx_epoch: float
        last_rx_message: MerossMessage | None
        last_rx_epoch: float

        connect_broadcast: Final[Broadcast[None, Self]]
        disconnect_broadcast: Final[Broadcast[None, Self]]
        tx_broadcast: Final[Broadcast[None, MerossMessage, Self]]
        rx_broadcast: Final[Broadcast[MerossMessage, MerossMessage, Self]]

        device: Final[Device]  # type: ignore[assignment]
        """Instance of device this client is attached to. This is set only by the Device.add_client
        (thorough on_device_add) and Device.remove_client methods, so it should be considered read-only
        for client implementations."""

    Direction = Direction
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
        "is_connected",
        "last_tx_message",
        "last_tx_epoch",
        "last_rx_message",
        "last_rx_epoch",
        "connect_broadcast",
        "disconnect_broadcast",
        "tx_broadcast",
        "rx_broadcast",
        "device",
    )

    def __init__(
        self, id, parent: "LoggerType | None" = None, /, **kwargs: "Unpack[Args]"
    ):
        self.key = kwargs.pop("key", mc.EMPTY_KEY)
        self.from_ = kwargs.pop("from_", mc.HEADER_FROM_DEFAULT)
        self.trigger_src = kwargs.pop("trigger_src", self.__class__.__name__)
        self.descriptor = kwargs.pop("descriptor", None)
        self.timeout = kwargs.pop("timeout", self.TIMEOUT)
        super().__init__(id, parent, **kwargs)
        self.is_connected = False
        self.last_tx_message = None
        self.last_tx_epoch = 0
        self.last_rx_message = None
        self.last_rx_epoch = 0
        self.connect_broadcast = self.Broadcast(self)
        self.disconnect_broadcast = self.Broadcast(self)
        self.tx_broadcast = self.Broadcast(self)
        self.rx_broadcast = self.Broadcast(self)

    async def async_shutdown(self):
        try:
            self.device.remove_client(self)
        except AttributeError:
            pass  # might be not linked ...

        await self.async_disconnect()
        await super().async_shutdown()

    @logging.abc.abstractmethod
    async def async_connect(self, /, **kwargs: "Unpack[ConnectArgs]"): ...
    @logging.abc.abstractmethod
    async def async_disconnect(self, /): ...

    def on_connect(self, /):
        """Signals client successful connection."""
        self.is_connected = True  # type: ignore
        self.log(logging.DEBUG, "Connected")
        self.connect_broadcast.broadcast(self)

    def on_disconnect(self, /):
        """Signals client disconnection."""
        self.is_connected = False  # type: ignore[assignment]
        self.log(logging.DEBUG, "Disconnected")
        self.disconnect_broadcast.broadcast(self)

    def on_tx(self, message: "MerossMessage", *args):
        """Signals client message transmission."""
        self.last_tx_message = message
        self.last_tx_epoch = self.time()
        # By design: we only log when no tx_broadcast listeners are present
        # since we suppose that could be used to implement custom logging/trace behavior
        if self.tx_broadcast:
            self.tx_broadcast.broadcast(message, self)
        else:
            self.log_message(message, Direction.TX)

    def on_rx_raw(self, raw: bytes | bytearray, /):
        """Processes client raw message reception before returning from async_request_raw.
        This could be overriden to implement more message processing in the receiving pipeline.
        """
        return self.on_rx(MerossResponse(raw.decode()))

    def on_rx(self, message: "MerossMessage", *args):
        """Signals client message reception. This is called by default by on_rx_raw after parsing the
        raw message into a MerossMessage, but it could be called directly by transport implementations if needed.
        """
        self.last_rx_message = message
        self.last_rx_epoch = self.time()
        # By design: we only log when no rx_broadcast listeners are present
        # since we suppose that could be used to implement custom logging/trace behavior
        if self.rx_broadcast:
            self.rx_broadcast.broadcast(message, self)
        else:
            self.log_message(message, Direction.RX)
        return message

    def log_message(self, message: "MerossMessage", direction: Direction, /):
        if self.isEnabledFor(logging.VERBOSE):
            self.log(
                logging.VERBOSE,
                "%s(%s) %s %s %s",
                direction,
                message.messageid,
                message.method,
                message.namespace,
                _message=message,
            )
        elif self.isEnabledFor(logging.DEBUG):
            self.log(
                logging.DEBUG,
                "%s(%s) %s %s",
                direction,
                message.messageid,
                message.method,
                message.namespace,
            )

    def on_device_add(self, device: "Device"):
        """Called when a client is being added to a device (add_client)."""
        try:
            # play it safe ...
            self.device.remove_client(self)
        except AttributeError:
            pass
        self.device = device  # type: ignore[assignment]
        self.logtag = self.TRANSPORT.upper()
        self.log(self.DEBUG, "Added client for %s", server=self.id)

    def on_device_remove(self, device: "Device"):
        """Called when a client is being removed from a device (remove_client)."""
        assert self.device is device, "Removing device that is not currently linked"
        del self.device  # type: ignore[assignment]
        self.log(self.DEBUG, "Removed client")
        self.configure_logger()

    @logging.abc.abstractmethod
    async def async_request_raw(
        self, request: MerossRequest, /, **kwargs: "Unpack[RequestRawArgs]"
    ) -> MerossResponse:
        """Low level request sending/receiving method to be implemented by
        transport-specific implementations."""
        ...

    async def async_request(
        self, *args: "Unpack[MerossRequestType]", **kwargs: "Unpack[RequestArgs]"
    ):
        return await self.async_request_raw(
            MerossRequest(
                *args,
                kwargs.pop("key", self.key),
                kwargs.pop("from_", self.from_),
                kwargs.pop("trigger_src", self.trigger_src),
            ),
            **kwargs,
        )

    async def async_request_ns_payload(
        self, ns: "Namespace", /, **kwargs: "Unpack[RequestArgs]"
    ) -> "Any":
        return (
            (await self.async_request(*ns.request_default, **kwargs))
            .check()
            .payload[ns.key]
        )

    async def async_request_multiple(
        self,
        requests: "Iterable[MerossRequestType]",
        /,
        **kwargs: "Unpack[RequestArgs]",
    ):
        """Send requests in a single NS_APPLIANCE_CONTROL_MULTIPLE message."""
        return await self.async_request(
            mn.Appliance_Control_Multiple,
            mc.METHOD_SET,
            {
                mn.Appliance_Control_Multiple.key: [
                    {
                        mc.KEY_HEADER: {
                            mc.KEY_MESSAGEID: MerossRequest.generate_id(),
                            mc.KEY_METHOD: request[1],
                            mc.KEY_NAMESPACE: request[0],
                        },
                        mc.KEY_PAYLOAD: request[2],
                    }
                    for request in requests
                ]
            },
            **kwargs,
        )

    async def async_identify(self, /, **kwargs: "Unpack[RequestArgs]"):
        return DeviceDescriptor(
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
            mn.Appliance_Config_WifiList, **kwargs
        )
        if sort_key:
            p_wifilist = sorted(p_wifilist, key=lambda x: x[sort_key], reverse=True)

        ssid_list: list[str] = []
        for wifi in p_wifilist:
            try:
                # It looks like some ssid b64 encodings are 'weird' and we're unable to decode them as UTF-8
                # strings
                ssid = a2b_base64(wifi[mc.KEY_SSID]).rstrip(b"\0").decode()
                if ssid not in ssid_list:
                    ssid_list.append(ssid)
            except:
                pass

        return ssid_list

    async def async_configure_mqtt(
        self,
        /,
        **kwargs: "Unpack[ConfigureMQTTArgs]",
    ):
        new_key = kwargs.pop("new_key", self.key)
        userid = kwargs.pop("userid", "0")
        try:
            host = kwargs.pop("host")
            port = kwargs.pop("port", mc.MQTT_DEFAULT_PORT)
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
                            mc.KEY_KEY: new_key,
                            mc.KEY_USERID: userid,
                        }
                    ),
                },
                **kwargs,
            )
        except KeyError:
            return await self.async_request(
                mn.Appliance_Config_Key,
                mc.METHOD_SET,
                {
                    mn.Appliance_Config_Key.key: (
                        {mc.KEY_KEY: new_key, mc.KEY_USERID: userid}
                    ),
                },
                **kwargs,
            )

    async def async_configure_wifi(
        self,
        /,
        **kwargs: "Unpack[ConfigureWifiArgs]",
    ):
        ns = mn.Appliance_Config_WifiX
        if (descriptor := self.descriptor) and (ns in descriptor.ability):
            password = compute_wifix_password(
                kwargs["password"],
                descriptor.type,
                descriptor.uuid,
                descriptor.macAddress,
            )
        else:
            ns = mn.Appliance_Config_Wifi
            password = b2a_base64(kwargs["password"].encode()).decode()

        return await self.async_request(
            ns,
            mc.METHOD_SET,
            {
                ns.key: {
                    mc.KEY_SSID: b2a_base64(kwargs["ssid"].encode()).decode(),
                    mc.KEY_PASSWORD: password,
                }
            },
            **kwargs,
        )

    async def async_configure(
        self,
        *,
        mqtt_host: str = "",
        mqtt_port: int = 8883,
        new_key: str = "",
        userid: str = "",
        wifi_ssid: str = "",
        wifi_password: str = "",
        **kwargs: "Unpack[RequestArgs]",
    ):
        # TODO: refine better. Also add configuration for time/timezone
        if wifi_ssid:
            assert wifi_password, "Wifi password is required if ssid is set"

        if mqtt_host or new_key or userid:
            await self.async_configure_mqtt(
                host=mqtt_host, port=mqtt_port, new_key=new_key, userid=userid, **kwargs
            )

        if wifi_ssid:
            await self.async_configure_wifi(
                ssid=wifi_ssid, password=wifi_password, **kwargs
            )
