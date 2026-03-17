from abc import abstractmethod
import asyncio
from datetime import UTC, tzinfo
from functools import cached_property
from typing import TYPE_CHECKING, override

from .. import (
    DeviceDescriptor,
    async_import_module,
    datetime_from_epoch,
    is_device_online,
    simple_slug,
    versiontuple,
)
from ..client import AbstractClient
from ..exceptions import MerossTransportError
from ..protocol import const as mc, namespaces as mn
from ..protocol.message import MerossMessage
from .handler import NamespaceHandler

if TYPE_CHECKING:
    from asyncio import Task
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

    from ..client.bluetooth import BluetoothClient
    from ..client.http import HttpClient
    from ..client.mqtt import AbstractMQTTConnection
    from ..cloudapi import DeviceInfoType, LatestVersionType
    from ..logging import LoggerType
    from ..protocol.message import MerossRequest, MerossResponse
    from ..protocol.types import (
        JsonDict,
        JsonList,
        JsonMapping,
        MerossMessageType,
        MerossPayloadType,
        MerossRequestType,
        VersionTupleType,
        config as mt_cf,
        control as mt_c,
        hub as mt_h,
        mcu as mt_m,
    )
    from .handler import NamespaceParser

Transport = AbstractClient.Transport


class PhysicalDevice(AbstractClient):
    """Common base for physical devices including Hub-paired (sub)devices.
    This is the common root of two main hierarchy branches defined in:
    - Device: any physical device with it's own network ip address
    - SubDevice: any physical device connected through an Hub (Device)

    This common root allows to abstract communication with namespaces
    which is actually carried/mediated by a proper Device.
    """

    if TYPE_CHECKING:
        descriptor: Final[DeviceDescriptor]  # type:ignore[override]
        latest_version: LatestVersionType  # lazy init

    __SLOTS__ = ("latest_version",)

    @override
    async def async_request_raw(
        self,
        request: "MerossRequest",
        /,
        **kwargs: "Unpack[AbstractClient.RequestRawArgs]",
    ) -> "MerossResponse":
        raise NotImplementedError(
            "async_request_raw is not implemented by design. Please use async_request instead"
        )

    @property
    @abstractmethod
    def firmware_version(self, /) -> str:
        raise NotImplementedError("firmware_version")

    @abstractmethod
    def get_upgrade_payload(self, /) -> "mt_c.Upgrade":
        """Builds and returns the correct upgrade payload if an upgrade is available, otherwise returns None/empty dict."""
        raise NotImplementedError("get_upgrade_payload")

    @abstractmethod
    def get_upgrade_info(self, /) -> tuple[str | None, ...]:
        """If an update is available returns a tuple of (installed_version, latest_version, release_summary)"""
        raise NotImplementedError("get_upgrade_info")

    # These methods are actually only relevant/implemented in Device child classes branch.
    # The Subdevice child branch will just link to the methods/attributes defined in the parent Device.
    @cached_property
    @abstractmethod
    def tz(self, /) -> tzinfo: ...

    @cached_property
    @abstractmethod
    def ns_handlers(self, /) -> "Mapping[str, NamespaceHandler]": ...

    @abstractmethod
    def _create_handler(
        self, ns: "mn.Namespace", /, **kwargs: "Unpack[NamespaceHandler.Args]"
    ) -> "NamespaceHandler": ...

    def get_handler(self, ns: "mn.Namespace", /):
        try:
            return self.ns_handlers[ns]
        except KeyError:
            return self._create_handler(ns)

    def register_parser_ex(
        self,
        parser: "NamespaceParser",
        *nss: "mn.Namespace",
    ):
        """Register a parser for multiple namespaces. Abilities are checked for namespaces availability."""
        ability = self.descriptor.ability
        for ns in (_ns for _ns in nss if _ns in ability):
            self.get_handler(ns).register_parser(parser)


class Device(PhysicalDevice):
    """Class to manage a Meross device. This is the main class of the library
    and provides the core functionalities to interact with the device (be it an Hub or a standard device),
    manage its state, and handle its namespaces."""

    if TYPE_CHECKING:

        type DigestParseFunc = Callable[[JsonDict], None] | Callable[[JsonList], None]
        type DigestInitReturnType = tuple[DigestParseFunc, Iterable[NamespaceHandler]]
        type DigestInitFunc = Callable[[Device, Any], DigestInitReturnType]
        type NamespaceInitFunc = Callable[[mn.Namespace, Device], None]

        class Args(AbstractClient.Args):
            descriptor: NotRequired[DeviceDescriptor]

        class ConnectArgs(AbstractClient.ConnectArgs):
            pass

        NAMESPACES: ClassVar[mn.NamespacesMapType]
        """Accesses the namespaces definitions for this Device. This could be overriden
        when needed to extend with other namespaces (this is actually true for Hub). This
        way, when we're working only with standard devices we don't need to import the namespaces
        only relevant to hubs."""
        DIGEST_INIT_PACKAGE: ClassVar[str]
        """package/module path where to look for digest initialization functions."""
        DIGEST_INIT: ClassVar[dict[str, Any]]
        """ Static dict of 'digest initialization function(s)'.
        This is built on demand during Device init whenever a new digest key
        is encountered. This static dict in turn is used to setup the Device instance
        'digest_handlers' dict which contains a lookup to the digest parsing function when
        an Appliance.System.All message is received/parsed.
        The 'digest initialization function' will (at device init time) parse the digest to
        setup the dedicated entities for the particular digest key.
        The definition of this init function is looked up at runtime by an algorithm that:
        - looks-up if the digest key is in DIGEST_INITIALIZERS where it'll find either the
        function or the (str) module coordinates of the init function for the digest key.
        - if not configured, the algorithm will try load the module in meross_lan/devices
        with the same name as the digest key.
        - if any is not found we'll set a 'digest_init_empty' function in order to not
        repeat the lookup process. That function will just pass so that the key
        init/parsing will just default to do nothing and not harm."""
        NAMESPACE_INIT_PACKAGE: ClassVar[str]
        """package/module path where to look for namespace initialization functions."""
        NAMESPACE_INIT: ClassVar[dict[mn.Namespace, Any]]
        """ Static dict of namespace initialization functions. This will be looked up
        and matched against the current device abilities (at device init time) and
        usually setups a dedicated namespace handler and/or a dedicated entity.
        As far as the initialization functions are looked up in related modules,
        they'll be cached in the dict.
        Namespace handlers will be initialized in the order as they appear in the dict
        and this could have consequences in the order of polls."""

        # Configuration
        preferred_transport: Transport
        polling_period: int

        # State
        transport: Final[Transport]
        """Currently active transport. This is a proxy for self.client.transport."""
        client: Final[AbstractClient | None]
        """Currently active client i.e. the client used by default for requests."""
        bluetooth: Final[BluetoothClient | None]
        http: Final[HttpClient | None]
        mqtt: Final[AbstractMQTTConnection.Client | None]
        mqtt_active: Final[bool]
        """MQTT application layer is fully connected i.e. we receive valid data from the remote end.
        This attribute is a proxy for the actual MQTT client state (is_connected) and
        need to be kept in sync (This is mostly accomplished in AbstractMQTTConnection.Client)."""
        _clients: Final[dict[Transport, AbstractClient]]
        _clients_connected: Final[dict[Transport, AbstractClient]]

        ns_handlers: Final[dict[str, NamespaceHandler]]
        digest_parsers: Final[dict[str, DigestParseFunc]]
        digest_pollers: Final[set[NamespaceHandler]]

        tz: tzinfo

        device_response_size_min: int
        device_response_size_max: int
        multiple_max: int
        _multiple_requests: list[NamespaceHandler]
        _multiple_response_size: int

        _polling_delay: int
        _polling_task: Task | None
        polling_lock: asyncio.Lock
        polling_epoch: Final[float]
        """Time of current/last polling cycle epoch."""
        _lazypoll_requests: list[NamespaceHandler]

    HEARTBEAT_TIMEOUT = 300
    TRANSPORT = Transport.AUTO  # type: ignore[override]
    NAMESPACES = mn.NAMESPACES

    @staticmethod
    def digest_parse_empty(digest: "JsonDict | JsonList"):
        pass

    @staticmethod
    def digest_init_empty(
        device: "Device", digest: "JsonDict | JsonList"
    ) -> "DigestInitReturnType":
        return Device.digest_parse_empty, ()

    @staticmethod
    def namespace_init_empty(ns: mn.Namespace, device: "Device", /):
        pass

    # TODO: define global symbol for merossclient package/library
    DIGEST_INIT_PACKAGE = "merossclient"
    DIGEST_INIT = {}
    NAMESPACE_INIT_PACKAGE = DIGEST_INIT_PACKAGE
    NAMESPACE_INIT = {}

    __SLOTS__ = (
        "preferred_transport",
        "polling_period",
        "transport",
        "client",
        "bluetooth",
        "http",
        "mqtt",
        "mqtt_active",
        "_clients",
        "_clients_connected",
        "ns_handlers",
        "digest_parsers",
        "digest_pollers",
        "tz",
        "device_response_size_min",
        "device_response_size_max",
        "multiple_max",
        "_multiple_requests",
        "_multiple_response_size",
        "_polling_delay",
        "_polling_task",
        "_polling_lock",
        "polling_epoch",
        "_lazypoll_requests",
    )

    def __init__(
        self, id, parent: "LoggerType | None" = None, **kwargs: "Unpack[Args]"
    ):
        super().__init__(id, parent, **kwargs)
        self.transport = self.preferred_transport = self.TRANSPORT
        self.client = None
        self.bluetooth = None
        self.http = None
        self.mqtt = None
        self.mqtt_active = False
        self._clients = {}
        self._clients_connected = {}
        self.ns_handlers = {}
        self.digest_parsers = {}
        self.digest_pollers = set()
        self.tz = UTC
        self.device_response_size_min = 1000
        self.device_response_size_max = (
            self.descriptor.ability.get(mn.Appliance_Control_Multiple, {}).get(
                "maxCmdNum", 0
            )
            * 800
        )
        if self.device_response_size_max < self.device_response_size_min:
            self.device_response_size_max = self.device_response_size_min
        self.multiple_max = 0
        self._multiple_requests = []
        self._multiple_response_size = NamespaceHandler.HEADER_AVG_SIZE
        self._lazypoll_requests = []
        self._polling_task = None
        self.polling_lock = asyncio.Lock()
        self.polling_epoch = self.time()

    async def async_init(self):
        digest_init_func: "Device.DigestInitFunc"
        ns_init_func: "Device.NamespaceInitFunc"

        await self._async_init_zoneinfo()

        descriptor = self.descriptor
        for key_digest, _digest in (
            descriptor.digest.items() or descriptor.control.items()
        ):
            try:
                try:
                    self.digest_parsers[key_digest], _digest_pollers = self.DIGEST_INIT[
                        key_digest
                    ](self, _digest)
                except (KeyError, TypeError):
                    # KeyError: key is unknown to our code (fallback to lookup ".devices.{key_digest}")
                    # TypeError: key is a string containing the module path
                    key_slug = simple_slug(key_digest)
                    _module_path = self.DIGEST_INIT.get(
                        key_digest, f".devices.{key_slug}"
                    )
                    if type(_module_path) is not str:
                        # This means we catched an error inside the digest init func
                        raise
                    try:
                        _module = await async_import_module(
                            _module_path, self.DIGEST_INIT_PACKAGE
                        )
                        # Lookup a class with the same name as the digest key.
                        # This is going to be the default parser class for the digest
                        # key and it has to implement a 'digest_init' classmethod with the proper signature.
                        try:
                            digest_init_func = getattr(
                                _module, key_slug.capitalize()
                            ).digest_init
                        except AttributeError:
                            # else fallback to a function with the same name as the digest key and the proper signature.
                            digest_init_func = getattr(
                                _module, f"digest_init_{key_slug}"
                            )
                    except Exception as exception:
                        self.log_exception(
                            self.WARNING,
                            exception,
                            "loading digest initializer for key '%s'",
                            key_digest,
                        )
                        digest_init_func = Device.digest_init_empty
                    self.DIGEST_INIT[key_digest] = digest_init_func
                    self.digest_parsers[key_digest], _digest_pollers = digest_init_func(
                        self, _digest
                    )
                self.digest_pollers.update(_digest_pollers)

            except Exception as exception:
                self.log_exception(
                    self.WARNING, exception, "initializing digest key '%s'", key_digest
                )
                self.digest_parsers[key_digest] = Device.digest_parse_empty

        ability = descriptor.ability
        for ns, ns_init_func in self.NAMESPACE_INIT.items():
            if ns not in ability:
                continue
            try:
                try:
                    ns_init_func(ns, self)
                except TypeError:
                    try:
                        ns_init_func = getattr(
                            await async_import_module(
                                ns_init_func[0], self.NAMESPACE_INIT_PACKAGE  # type: ignore
                            ),
                            ns_init_func[1],  # type: ignore
                        )
                    except Exception as exception:
                        self.log_exception(
                            self.WARNING,
                            exception,
                            "loading namespace initializer for %s",
                            ns,
                        )
                        self.NAMESPACE_INIT[ns] = Device.namespace_init_empty
                    else:
                        try:
                            ns_init_func = ns_init_func.namespace_init
                        except AttributeError:
                            pass
                        self.NAMESPACE_INIT[ns] = ns_init_func
                        ns_init_func(ns, self)

            except Exception as exception:
                self.log_exception(
                    self.WARNING, exception, "initializing namespace %s", ns
                )

    async def _async_init_zoneinfo(self, /):
        if tzname := self.descriptor.timezone:
            try:
                self.tz = await self.async_load_zoneinfo(tzname)
            except Exception:
                self.tz = UTC
        else:
            self.tz = UTC

    @override
    async def async_shutdown(self):
        self.polling_stop()
        for client in tuple(self._clients.values()):
            await client.async_shutdown()
        await super().async_shutdown()
        self.digest_parsers.clear()
        self.digest_pollers.clear()
        self._lazypoll_requests.clear()
        # This must be by design
        assert self.is_connected is False, "Device shutdown failed: still connected"
        assert not self.client, "Device shutdown failed: client still set"
        assert (
            not self.ns_handlers
        ), "Device shutdown failed: namespace handlers still set"

    # interface: AbstractClient
    @override
    async def async_connect(self, /, **kwargs: "Unpack[ConnectArgs]"):
        handler_all = self.get_handler(mn.Appliance_System_All)
        # use pre 3.13 compatible syntax/semantics
        for earliest_connect in asyncio.as_completed(
            {
                _client.create_task(
                    _client.async_request(*handler_all.polling_request),
                    f".async_connect_{_client.TRANSPORT}_task",
                    eager_start=True,
                )
                for _client in self._clients.values()
            },
            timeout=kwargs.get("timeout", self.timeout),
        ):
            try:
                response = await earliest_connect
                if not self.is_connected:
                    self.on_connect()
                handler_all.handle_response(response)
                handler_all.polling_response_size = len(response.json)
                return response
            except Exception:
                pass
        else:
            raise asyncio.TimeoutError("No transport available")

    @override
    async def async_disconnect(self, /):
        await asyncio.gather(
            *[
                _client.async_disconnect()
                for _client in self._clients_connected.values()
            ],
            return_exceptions=True,
        )
        assert (
            not self.is_connected
        ), "disconnect failed: still connected to some transports"

    @override
    def on_connect(self, /):
        super().on_connect()
        self._polling_delay = self.polling_period

    @override
    def on_disconnect(self, /):
        super().on_disconnect()
        self.client = None  # type: ignore[assignment]
        self.mqtt_active = False  # type: ignore[assignment]
        for handler in self.ns_handlers.values():
            handler.polling_epoch_next = 0.0

    @override
    async def async_request(
        self,
        *args: "Unpack[MerossRequestType]",
        **kwargs: "Unpack[AbstractClient.RequestArgs]",
    ):
        """
        route the request through available transports to the physical device according to
        current protocol. When switching transport the message is recomputed to
        avoid reusing the same (old) timestamps and messageids.
        """
        try:
            # We expect this to work most of the time, so we try it first and
            # catch any exception to trigger the fallback logic.
            return await self.client.async_request(*args, **kwargs)  # type: ignore[union-attr]
        except Exception as e:
            if self.client:
                if len(self._clients) < 2:
                    raise
                tryed_clients = {self.client}
            else:
                if not self._clients:
                    raise MerossTransportError(
                        self, "No transport available to send the request"
                    )
                tryed_clients = set()

        self.log(
            self.DEBUG,
            "Request failed on current transport (%s client:%s): trying fall-back",
            self.transport,
            self.client,
        )
        while True:
            for _client in self._clients_connected.values():
                if _client in tryed_clients:
                    continue
                try:
                    return await _client.async_request(*args, **kwargs)
                except Exception as e:
                    tryed_clients.add(_client)
                    last_exception = e
                    # We need to break here because _clients_connected might change
                    # This is expensive but we could only have max 2 connected clients at a time
                    break  # to outer (infinite) loop
            else:
                break

        while True:
            for _client in self._clients.values():
                if _client in tryed_clients:
                    continue
                try:
                    return await _client.async_request(*args, **kwargs)
                except Exception as e:
                    tryed_clients.add(_client)
                    last_exception = e
                    break  # to outer (infinite) loop
            else:
                break

        raise last_exception  # type: ignore[unbound-variable]

    # interface: PhysicalDevice
    @property
    @override
    def firmware_version(self, /) -> str:
        return self.descriptor.firmwareVersion

    @override
    def get_upgrade_payload(self, /) -> "mt_c.Upgrade":
        return self.descriptor.build_upgrade_payload(self.latest_version)

    @override
    def get_upgrade_info(self, /):
        # assert self.latest_version
        latest_version = self.latest_version
        try:
            descriptor = self.descriptor
            upgrade_payload = descriptor.build_upgrade_payload(latest_version)
            if upgrade_payload and mc.KEY_MCU in upgrade_payload:
                assert descriptor.mcu
                return (
                    descriptor.mcu[mc.KEY_VERSION],
                    latest_version[mc.KEY_MCU][0][mc.KEY_VERSION],
                    latest_version.get(mc.KEY_DESCRIPTION),
                )
            else:
                return (
                    descriptor.firmwareVersion,
                    latest_version[mc.KEY_VERSION],
                    latest_version.get(mc.KEY_DESCRIPTION),
                )
        except Exception as e:
            self.log_exception(
                self.WARNING,
                e,
                "get_upgrade_info (latest_version:%s mcu:%s)",
                str(latest_version),
                str(descriptor.mcu),
            )
            return None, None, None

    # interface: self
    @property
    def meross_binded(self):
        """
        Reports if the device own MQTT connection is active and likely Meross
        account binded.
        """
        if (mqtt := self.mqtt) and mqtt.is_connected:
            return mqtt.connection.is_cloud
        # if we're not connected (either reason) check the internal
        # device state connection
        if not is_device_online(self.descriptor.system):
            return False
        # the device is connected to its own broker..assume
        # it is a Meross cloud one
        return True

    def get_device_datetime(self, epoch, /):
        """
        given the epoch (utc timestamp) returns the datetime
        in device local timezone
        """
        return datetime_from_epoch(epoch, self.tz)

    def add_client(self, client: "AbstractClient", /):
        assert (
            getattr(self, client.TRANSPORT) is None
        ), f"{client.TRANSPORT} client already attached to {self}"
        client.on_device_add(self)
        setattr(self, client.TRANSPORT, client)
        self._clients[client.TRANSPORT] = client
        client.connect_broadcast.add(self.on_client_connect)
        client.disconnect_broadcast.add(self.on_client_disconnect)
        client.tx_broadcast.add(self.on_tx)
        client.rx_broadcast.add(self.on_rx)
        if client.is_connected:
            self.on_client_connect(client)

    def remove_client(self, client: "AbstractClient", /):
        assert (
            getattr(self, client.TRANSPORT) is client
        ), f"{client.TRANSPORT} client not attached to {self}"
        client.on_device_remove(self)
        del self._clients[client.TRANSPORT]
        setattr(self, client.TRANSPORT, None)
        if client.is_connected:
            self.on_client_disconnect(client)
        client.connect_broadcast.remove(self.on_client_connect)
        client.disconnect_broadcast.remove(self.on_client_disconnect)
        client.tx_broadcast.remove(self.on_tx)
        client.rx_broadcast.remove(self.on_rx)
        if self.client is client:
            self.client = None  # type: ignore[assignment]
            self.transport = self.TRANSPORT  # type: ignore[assignment]
            self.log(self.DEBUG, "Switching transport to %s", self.transport)

    def on_client_connect(self, client: "AbstractClient", /):
        self._clients_connected[client.TRANSPORT] = client

    def on_client_disconnect(self, client: "AbstractClient", /):
        del self._clients_connected[client.TRANSPORT]
        if self._clients_connected:
            if self.client is client:
                self._switch_client(next(iter(self._clients_connected.values())))
        elif self.is_connected:
            self.on_disconnect()

    def _switch_client(self, client: "AbstractClient"):
        self.client = client  # type: ignore[assignment]
        self.transport = client.TRANSPORT  # type: ignore[assignment]
        self.log(self.DEBUG, "Switching transport to %s", self.transport)

    @override
    def _create_handler(
        self, ns: "mn.Namespace", /, **kwargs: "Unpack[NamespaceHandler.Args]"
    ):
        """Called by the base device message parsing chain when a new
        NamespaceHandler need to be defined (This happens the first time
        the namespace enters the message handling flow)"""
        return NamespaceHandler(ns, self, **kwargs)

    def get_handler_by_name(self, namespace: str, /):
        try:
            return self.ns_handlers[namespace]
        except KeyError:
            return self._create_handler(self.NAMESPACES[namespace])

    @property
    def polling_response_size_available(self):
        """Returns the expected maximum allowed request response size in the current
        multiple request poll. If multiple polling is disabled this works too."""
        return (
            self.device_response_size_max - self._multiple_response_size
            if self.multiple_max
            else self.device_response_size_max
        )

    @property
    def should_limit_cloud_polling(self):
        """Returns True if we should limit cloud polling in order to avoid hitting the device rate-limiting.
        This is typically when we're using Meross cloud MQTT and we should avoid hitting the 200 messages x hour limit.
        """
        return (
            self.mqtt
            and (self.client is self.mqtt)  # active client is MQTT
            and (
                self.mqtt.connection.get_rl_safe_delay(self.id) > 20
            )  # 20 secs to wait before next MQTT request without hitting rate-limiting
        )

    def enable_multiple(self, enable: bool, /):
        self.multiple_max = (
            self.descriptor.ability.get(mn.Appliance_Control_Multiple, {}).get(
                "maxCmdNum", 0
            )
            if enable
            else 0
        )
        self._multiple_requests.clear()
        self._multiple_response_size = NamespaceHandler.HEADER_AVG_SIZE

    def polling_start(self):
        """Starts scheduling the polling task. This will be automatically
        re-scheduled until polling_stop is called which will cancel the schedule
        and any ongoing polling task.
        When called while a schedule is already in place, it'll be cancelled and re-started immediately.
        """
        self._polling_delay = self.polling_period
        self._polling()

    def polling_stop(self):
        """Stops the polling schedule and cancels any ongoing polling task."""
        self.cancel_callback(self._polling)
        if self._polling_task:
            self._polling_task.cancel("polling_stop")

    def _polling(self, /):
        self.schedule_callback(self._polling_delay, self._polling)
        self._polling_task = self.create_task(
            self.async_poll(), f"._polling", eager_start=False
        )

    async def async_poll(self, /):
        async with self.polling_lock:
            self.polling_epoch = epoch = self.time()  # type: ignore[assignment]
            self.log(self.DEBUG, "Polling begin")
            try:
                if self.is_connected:
                    # perform some heartbeats in case
                    if (
                        (http := self.http)
                        and (self.client is not http)
                        and (self.preferred_transport is Transport.HTTP)
                        and ((epoch - http.last_tx_epoch) > self.HEARTBEAT_TIMEOUT)
                    ):
                        try:
                            handler_all = self.get_handler(mn.Appliance_System_All)
                            handler_all.handle_response(
                                await http.async_request(*handler_all.polling_request)
                            )
                        except Exception:
                            pass

                    if (
                        (mqtt := self.mqtt)
                        and mqtt.connection.can_publish
                        and ((epoch - mqtt.last_rx_epoch) > self.HEARTBEAT_TIMEOUT)
                    ):
                        try:
                            handler_all = self.get_handler(mn.Appliance_System_All)
                            handler_all.handle_response(
                                await mqtt.async_request(*handler_all.polling_request)
                            )
                        except Exception:
                            pass

                else:  # offline
                    await self.async_connect()

                """
                When 'namespace' is not 'None' it represents the device coming online
                following a succesful received message. This is likely to be 'NS_ALL'.
                If we're connected to an MQTT broker anyway it could be any 'PUSH' message.
                We'll use _queued_smartpoll_requests to track how many polls went through
                over MQTT for this cycle in order to only send 1 for each if we're
                binded to a cloud MQTT broker (in order to reduce bursts).
                If a poll request is discarded because of this, it should go through
                on the next polling cycle. This will 'spread' smart requests over
                subsequent polls
                """
                self._lazypoll_requests.clear()
                # self.ns_handlers could change at any time due to async
                # message parsing (handlers might be dynamically created by then)
                for handler in [handler for handler in self.ns_handlers.values()]:
                    if handler.polling_strategy:
                        await handler.polling_strategy(handler)
                        if not self.is_connected:
                            break  # do not return: do the flush first!

                # needed even if offline: it takes care of resetting the ns_multiple state
                if self._multiple_requests:
                    await self.async_poll_flush()

            except asyncio.CancelledError:
                self.log(self.DEBUG, "Polling cancelled")
                raise
            except asyncio.TimeoutError:
                if self.is_connected:
                    self.on_disconnect()
                elif self._polling_delay < self.HEARTBEAT_TIMEOUT:
                    self._polling_delay += self.polling_period
                else:
                    self._polling_delay = self.HEARTBEAT_TIMEOUT
            except Exception as e:
                self.log_exception(self.WARNING, e, "async_poll")
            finally:
                self._polling_task = None
            self.log(self.DEBUG, "Polling end")

    async def async_poll_full(self):
        """Perform a 'full' namespaces poll like when onlining i.e. without any lazy optimization."""
        try:
            # eventually reschedule from now on
            self._timers[self._polling].cancel()
            self.schedule_callback(self._polling_delay, self._polling)
        except (AttributeError, KeyError):
            pass  # do not (re)schedule if we're not polling already
        for handler in self.ns_handlers.values():
            handler.polling_epoch_next = 0.0
        await self.async_poll()

    async def async_poll_flush(self):
        multiple_requests = self._multiple_requests
        multiple_response_size = self._multiple_response_size
        self._multiple_requests = []
        self._multiple_response_size = NamespaceHandler.HEADER_AVG_SIZE

        requests_len = len(multiple_requests)
        while self.is_connected and requests_len:
            lazypoll_requests = self._lazypoll_requests
            while (requests_len < self.multiple_max) and lazypoll_requests:
                # we have space available in current ns_multiple and lazy pollers are waiting
                for handler in lazypoll_requests:
                    # lazy pollers are ordered by 'oldest polled first' so
                    # the first is the one which hasn't been polled since longer
                    # we then decide to add to the current ns_multiple the first that would fit in
                    if (
                        handler.polling_response_size + multiple_response_size
                    ) < self.device_response_size_max:
                        handler.last_poll_epoch = self.polling_epoch
                        handler.polling_epoch_next = (
                            handler.last_poll_epoch + handler.polling_period
                        )
                        multiple_requests.append(handler)
                        lazypoll_requests.remove(handler)
                        multiple_response_size += handler.polling_response_size
                        requests_len += 1
                        # check if we can add more
                        break  # for
                else:
                    # no lazy_poller could match..break out of while
                    break  # while

            if requests_len == 1:
                await multiple_requests[0].async_get_safe()
                return

            try:
                response = await self.async_request_multiple(
                    (handler.polling_request for handler in multiple_requests),
                )
            except Exception as e:
                # the ns_multiple failed but the reason could be the device
                # did overflow somehow. I've seen 2 kind of errors so far on the
                # HTTP client: typically the device returns an incomplete json
                # and this is partly recovered in our http interface. One(old)
                # bulb (msl120) instead completely disconnects (ServerDisconnectedException
                # in http client) and so we get here with no response. The same
                # msl bulb timeouts completely on MQTT, so the response to our mqtt requests
                # is None again. At this point, if the device is still online we're
                # trying a last resort issue of single requests
                if self.is_connected:
                    self.log(
                        self.DEBUG,
                        "Appliance.Control.Multiple failed with '%s' (requests=%d expected size=%d)",
                        str(e) or e.__class__.__name__,
                        requests_len,
                        multiple_response_size,
                    )
                    # Here we reduce the device_response_size_max so that
                    # next ns_multiple will be less demanding. device_response_size_min
                    # is another dynamic param representing the biggest payload ever received
                    self.device_response_size_max = (
                        self.device_response_size_max + self.device_response_size_min
                    ) / 2  # type: ignore
                    self.log(
                        self.DEBUG,
                        "Updating device_response_size_max:%d",
                        self.device_response_size_max,
                    )
                    for handler in multiple_requests:
                        if not self.is_connected:  # TODO: remove these online checks
                            break
                        await handler.async_get_safe()
                return

            multiple_responses = response[mc.KEY_PAYLOAD][mc.KEY_MULTIPLE]
            if not multiple_responses:
                # no response at all..this is pathological but we have
                # examples (#526) of this so we'll just try issue single requests
                self.log(
                    self.WARNING,
                    "Appliance.Control.Multiple empty response (requests=%d expected size=%d)",
                    requests_len,
                    multiple_response_size,
                    timeout=14400,
                )
                for handler in multiple_requests:
                    if not self.is_connected:
                        break
                    await handler.async_get_safe()
                return

            responses_len = len(multiple_responses)
            if self.isEnabledFor(self.DEBUG):
                self.log(
                    self.DEBUG,
                    "Appliance.Control.Multiple requests=%d (responses=%d) expected size=%d (actual=%d)",
                    requests_len,
                    responses_len,
                    multiple_response_size,
                    len(response.json),
                )

            message: "MerossMessageType"
            for message in multiple_responses:
                _response = MerossMessage(message)
                for handler in multiple_requests:
                    if handler.id != _response.namespace:
                        continue
                    multiple_requests.remove(handler)
                    handler.handle_response(_response)
                    break
                else:
                    # not found..something is wrong!! TODO: log a DEBUG/WARNING here?
                    pass

            # and re-issue the missing ones
            requests_len = len(multiple_requests)
            multiple_response_size = -1  # logging purpose

    async def async_poll_request(self, handler: NamespaceHandler, /):
        handler.last_poll_epoch = self.polling_epoch
        handler.polling_epoch_next = handler.last_poll_epoch + handler.polling_period
        if (not self.multiple_max) or (
            handler.polling_response_size >= self.device_response_size_max
        ):
            # multiple requests are disabled
            # or this request alone would overflow the device response size limit
            await handler.async_get_safe()
            return
        # estimate the size of the multiple response
        multiple_response_size = (
            self._multiple_response_size + handler.polling_response_size
        )
        if multiple_response_size >= self.device_response_size_max:
            # this request (together with already previously packed)
            # would overflow the device response size limit
            if not self._multiple_requests:
                # again this request alone would overflow the device response size limit
                await handler.async_get_safe()
                return
            # flush the pending multiple requests
            await self.async_poll_flush()
            multiple_response_size = (
                self._multiple_response_size + handler.polling_response_size
            )
        self._multiple_requests.append(handler)
        self._multiple_response_size = multiple_response_size
        if len(self._multiple_requests) >= self.multiple_max:
            await self.async_poll_flush()

    async def async_poll_request_smart(self, handler: NamespaceHandler, /):
        if self.should_limit_cloud_polling and (
            (self.polling_epoch - handler.last_poll_epoch)
            < handler.polling_period_cloud
        ):
            assert self.mqtt
            self.log(
                self.DEBUG,
                "Skipping poll for %s to avoid mqtt rate-limiting (queue delay=%d s)",
                handler.id,
                self.mqtt.connection.get_rl_safe_delay(self.id),
            )
            return False
        await self.async_poll_request(handler)
        return True

    async def async_unbind(self):
        """
        WARNING!!!
        Hardware reset to factory default: the device will unpair itself from
        the (cloud) broker and then reboot, ready to be initialized/paired.
        This coroutine will likely raise an exception (server connection reset or timeout)
        """
        # in case we're connected to a broker we'll use that since
        # it appears the (cloud) broker session level will take care of also removing
        # the device from its list, thus totally cancelling it from the Meross account
        if self.mqtt and self.mqtt.is_connected:
            return await self.mqtt.async_request(
                *mn.Appliance_Control_Unbind.request_default
            )
        # else go with whatever transport: the device will reset it's configuration
        return await self.async_request(*mn.Appliance_Control_Unbind.request_default)


class SubDevice(PhysicalDevice):
    """Common base for hub-paired subdevices."""

    if TYPE_CHECKING:
        parent: Final[Device]  # type: ignore[override]

    __SLOTS__ = (
        "async_request",
        "ns_handlers",
        "_create_handler",
    )

    def __init__(
        self, id: str, parent: "Device", **kwargs: "Unpack[PhysicalDevice.Args]"
    ):
        self.async_request = parent.async_request
        self.ns_handlers = parent.ns_handlers
        self._create_handler = parent._create_handler
        kwargs["key"] = parent.key
        kwargs["from_"] = parent.from_
        kwargs["trigger_src"] = parent.trigger_src
        kwargs["descriptor"] = parent.descriptor
        kwargs["timeout"] = parent.timeout
        kwargs["loop"] = parent.loop
        super().__init__(id, parent, **kwargs)

    async def async_shutdown(self):
        await super().async_shutdown()
        del self.async_request
        del self.ns_handlers
        del self._create_handler

    # interface: AbstractClient
    @override
    async def async_connect(self, /, **kwargs):
        pass

    @override
    async def async_disconnect(self, /):
        pass

    # interface: PhysicalDevice
    @property
    @override
    # Use a property since parent.tz might change so we can't cache it
    def tz(self):
        return self.parent.tz

    @override
    # parent._create_handler is being cached in self._create_handler
    def _create_handler(
        self, ns: "mn.Namespace", /, **kwargs: "Unpack[NamespaceHandler.Args]"
    ) -> "NamespaceHandler": ...

    # TODO: implement maybe something for firmware_version
    @override
    def get_upgrade_payload(self, /) -> "mt_c.Upgrade":
        # start from hub upgrade payload (eventually)
        upgrade_payload = self.parent.get_upgrade_payload()
        latest_version = self.latest_version
        if versiontuple(latest_version[mc.KEY_VERSION]) > versiontuple(
            self.firmware_version
        ):
            upgrade_payload["subdev"] = [
                {
                    "devid": self.id,
                    mc.KEY_URL: latest_version[mc.KEY_URL],
                    mc.KEY_MD5: latest_version[mc.KEY_MD5],
                }
            ]
        return upgrade_payload

    @override
    def get_upgrade_info(self, /):
        return (
            self.firmware_version,
            self.latest_version.get(mc.KEY_VERSION),
            self.latest_version.get(mc.KEY_DESCRIPTION),
        )
