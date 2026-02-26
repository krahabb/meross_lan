from abc import abstractmethod
import asyncio
from bisect import insort_right
from datetime import UTC, tzinfo
from functools import cached_property
from typing import TYPE_CHECKING, override

from . import DeviceDescriptor, logging, merge_dicts, versiontuple
from .client import AbstractClient
from .protocol import (
    MerossError,
    const as mc,
    namespaces as mn,
)
from .protocol.message import MerossMessage

if TYPE_CHECKING:
    from asyncio import Task, TimerHandle
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

    from .client.bluetooth import BluetoothClient
    from .client.http import HttpClient
    from .client.mqtt import AbstractMQTTConnection
    from .cloudapi import DeviceInfoType, LatestVersionType
    from .logging import LoggerType
    from .protocol.message import MerossRequest, MerossResponse
    from .protocol.types import (
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

Transport = AbstractClient.Transport
T_AUTO = Transport.AUTO
T_BLUETOOTH = Transport.BLUETOOTH
T_HTTP = Transport.HTTP
T_MQTT = Transport.MQTT


class NamespaceParser(logging.Loggable):
    """
    Represents the final 'parser' of a message after 'handling' in NamespaceHandler.
    In this model, NamespaceHandler is responsible for unpacking those messages
    who are intended to be delivered to different entities based off some indexing
    keys. These are typically: "channel", "Id", "subId" depending on the namespace itself.
    The class implementing the NamespaceParser protocol needs to expose that key value as a
    property with the same name. 99% of the time the class is a MLEntity with its "channel"
    property but the implementation allows more versatility.
    The protocol implementation needs to also expose a proper _parse_{key_namespace}
    (see NamespaceHandler.register_parser).
    """

    if TYPE_CHECKING:
        # These properties must be implemented in derived classes according to the
        # namespace payload syntax. NamespaceHandler will lookup any of these when
        # establishing the link between the handler and the parser
        manager: "Device"  # used for async_request and ns_handlers access
        ns: mn.Namespace  # same (only MLEntity for now)
        channel: int | str  # the channel/id/subId key value according to the namespace

        _payload_ns: JsonDict  # the last parsed payload
        _namespace_handlers: set[
            "NamespaceHandler"
        ]  # multiple ns could forward to this parser

    # using class-level defaults here until we build a proper hierarchy
    # with specialized __init__
    _payload_ns = mn.EMPTY_DICT  # class-level default
    _namespace_handlers = None  # type: ignore

    __SLOTS__ = ()

    async def async_shutdown(self):
        await super().async_shutdown()
        try:
            for handler in self._namespace_handlers:
                _dispatcher: NamespaceHandler._DispatcherParser = handler.parsers[self.channel]  # type: ignore
                if type(_dispatcher) is NamespaceHandler._DispatcherParser:
                    # remove from dispatcher
                    _dispatcher.parsers.remove(
                        getattr(self, f"_parse_{handler.ns.slug_end}", self._parse)
                    )
                    if not _dispatcher.parsers:
                        del handler.parsers[self.channel]
                else:
                    del handler.parsers[self.channel]
            self._namespace_handlers = None  # type: ignore
            del self.handler_ns
        except (TypeError, AttributeError):  # never registered
            pass
        assert self._namespace_handlers is None

    @cached_property
    def handler_ns(self):
        # TODO: define a more consistent interface
        # This is right now a brutal hack to automagically provide ns_handler property
        # to entities which might not need to be registered parsers but still need to access
        # the NamespaceHandler to issue device requests. Most of the times these are entities
        # where ns parsing is delegated to a container object/handler which is then dispatching
        # updates without using the NamespaceHandler inner mechanisms.
        return self.manager.ns_handlers[self.ns]

    async def async_request_payload(self, payload: "JsonDict", /):
        return await self.manager.async_request(
            *self.ns.request_set(payload, self.channel)
        )

    async def async_request_parse(self, payload: "JsonDict", /):
        response = await self.async_request_payload(payload)
        # TODO: consider maybe a dedicated _parse_set_xxxx method?
        # also, most namespaces SETACK replies are empty dicts
        # so we just dispatch the request payload (which might be a
        # subset of the whole GET payload).
        # Some namespaces though might return different payloads on SETACK
        # GarageDoor.State or mts100.Temperature
        getattr(self, f"_parse_{self.ns.slug_end}", self._parse)(payload)
        return response

    async def async_request_parse_ex(self, payload: "JsonDict", /):
        response = await self.async_request_payload(payload)
        getattr(self, f"_parse_{self.ns.slug_end}", self._parse)(
            merge_dicts(dict(self._payload_ns), payload)
        )
        return response

    def _parse(self, payload: "JsonMapping", /):
        """Default payload message parser. This is invoked automatically
        when the parser is registered to a NamespaceHandler for a given namespace
        and no 'better' _parse_xxxx has been defined. See NamespaceHandler.register.
        At this root level, coming here is likely an error but this feature
        (default parser) is being leveraged to setup a default parsing route for some
        specific class of entities instead of having to define a specific _parse_xxxx.
        This is useful for generalized sensor classes which are just mapped to a single
        namespace."""
        self.log(
            self.WARNING,
            "Parsing undefined for payload:(%s)",
            _payload=payload,
            timeout=14400,
        )


class NamespaceHandler:
    """
    This is the root class for somewhat dynamic namespace handlers.
    Every device keeps its own list of method handlers indexed through
    the message namespace in order to speed up parsing/routing when receiving
    a message from the device see Device.ns_handlers and
    Device._handle to get the basic behavior.

    - handler: specify a custom handler method for this namespace. By default
    it will be looked-up in the device definition (looking for _handle_xxxxxx)

    - entity_class: specify a MLEntity type (actually an implementation
    of Merossentity) to be instanced whenever a message for a particular channel
    is received and the channel has no parser associated (see _handle_list)

    """

    if TYPE_CHECKING:

        type HandlerFunc = Callable[[MerossMessage], None]
        type ParserFunc = Callable[[JsonMapping], None]
        type PollingStrategyFunc = Callable[[Self], Coroutine]
        type ConfigType = tuple[int, int, int, PollingStrategyFunc | None]

        DEFAULT_CONFIG: ClassVar[ConfigType]
        HEADER_AVG_SIZE: Final[int]
        """(rough) estimate of the header part of any response"""

        parsers: Final[dict[object, ParserFunc]]
        handler: HandlerFunc

        polling_strategy: PollingStrategyFunc | None
        polling_request: MerossRequestType
        polling_request_channels: list[dict[str, Any]]  # on demand instance

    class _DispatcherParser:
        """Small helper class to implement dispatching the same payload to
        multiple registered NamespaceParsers.
        By default (and historically), only a single parser is registered to
        receive a (channel) payload when dispatching message data for a namespace.
        When needed though, we might want to dispatch the same payload to multiple
        Parsers/Entities. This is typically needed when we have multiple data field in a payload
        each one binded or needed to be forwarded to a different entity.
        The single parser model overcomes this by installing a parser that subsequently
        dispatches the data to the multiple entities. This helper class simplifies
        and generalizes this pattern by automatically creating the 'dispatcher parser'
        responsible to deliver data to multiple entities."""

        if TYPE_CHECKING:
            type ParsersContainer = list["NamespaceHandler.ParserFunc"]
            parsers: Final[ParsersContainer]

        __slots__ = ("parsers",)

        def __init__(
            self, parsers: "Iterable[NamespaceHandler.ParserFunc] | None" = None, /
        ):
            self.parsers = list(parsers) if parsers is not None else []

        def __call__(self, payload: "JsonMapping", /):
            for parser in self.parsers:
                parser(payload)

    DEFAULT_CONFIG = (
        0,
        0,
        50,
        None,
    )

    HEADER_AVG_SIZE = 300

    __slots__ = (
        "device",
        "ns",
        "handler",
        "parsers",
        "last_rx_epoch",
        "last_poll_epoch",
        "polling_epoch_next",
        "polling_strategy",
        "polling_period",
        "polling_period_cloud",
        "polling_response_item_size",
        "polling_response_size",
        "polling_request",
        "polling_request_channels",
    )

    def __init__(
        self,
        device: "Device",
        ns: "mn.Namespace",
        /,
        *,
        handler: "HandlerFunc | None" = None,
        config: "ConfigType | None" = None,
    ):
        assert ns not in device.ns_handlers, (
            "Namespace already registered",
            ns,
        )
        self.device = device
        self.ns = ns
        self.handler = handler or getattr(
            device, f"_handle_{ns.replace('.', '_')}", self._handle_undefined
        )
        self.parsers = {}
        self.last_rx_epoch = self.last_poll_epoch = self.polling_epoch_next = 0.0
        config = config or self.DEFAULT_CONFIG
        self.polling_period = config[0]
        self.polling_period_cloud = config[1]
        self.polling_response_item_size = config[2]
        self.polling_strategy = config[3]
        # by default we calculate 1 item/channel per payload but we should
        # refine this whenever needed
        self.polling_response_size = (
            self.HEADER_AVG_SIZE + self.polling_response_item_size
        )
        self.polling_request_configure(
            mn.PayloadType.LIST_IDX_STRICT
            if self.polling_strategy is NamespaceHandler.async_poll_chunked
            else None
        )
        device.ns_handlers[ns] = self

    def shutdown(self):
        """Cleanup possible circular references."""
        del self.handler  # especially this one
        del self.device
        assert not self.parsers, "parsers should have been cleared before shutdown"

    def register_parser(
        self,
        parser: "NamespaceParser",
        extra: "MerossPayloadType" = mn.EMPTY_DICT,
        /,
    ):
        """Installs a dedicated parser for the given channel payload.
        Calling this multiple times for the same channel is prohibited
        by design even though the dispatching model allows (_DispatcherParser)
        multiple recipients. Use register_parsers instead."""
        channel = parser.channel
        assert channel not in self.parsers, "Parser already registered for channel"
        self.parsers[channel] = getattr(
            parser, f"_parse_{self.ns.slug_end}", parser._parse
        )

        if not parser._namespace_handlers:
            parser._namespace_handlers = set()
        parser._namespace_handlers.add(self)
        self.polling_request_add_channel(channel, extra)
        self.handler = self._handle_list

    def register_parsers(self, *parsers: "NamespaceParser"):
        """Registers a whole set of parsers at once for the same channel payload.
        This will automatically install a dispatcher. This feature is useful to avoid having
        to define a dedicated parser class just to dispatch data to multiple entities.
        This will in turn remove the need for references that need to be maintained."""
        channel = parsers[0].channel
        assert channel not in self.parsers, "Parser already registered for channel"
        self.parsers[channel] = _dispatcher = NamespaceHandler._DispatcherParser()
        _parser_method_name = f"_parse_{self.ns.slug_end}"
        for parser in parsers:
            assert parser.channel == channel, "All parsers must have the same channel"
            if not parser._namespace_handlers:
                parser._namespace_handlers = set()
            parser._namespace_handlers.add(self)
            _dispatcher.parsers.append(
                getattr(parser, _parser_method_name, parser._parse)
            )
        self.polling_request_add_channel(channel)
        self.handler = self._handle_list

    def swap_parsers(self, old: "NamespaceParser", *parsers: "NamespaceParser"):
        if len(parsers) == 1:
            parser = parsers[0]
            assert old.channel == parser.channel, "channel mismatch"
            old._namespace_handlers.remove(self)
            self.parsers[parser.channel] = getattr(
                parser, f"_parse_{self.ns.slug_end}", parser._parse
            )
            if not parser._namespace_handlers:
                parser._namespace_handlers = set()
            parser._namespace_handlers.add(self)
        else:
            # install a dispatcher
            old._namespace_handlers.remove(self)
            self.parsers[old.channel] = _dispatcher = (
                NamespaceHandler._DispatcherParser()
            )
            _parser_method_name = f"_parse_{self.ns.slug_end}"
            for parser in parsers:
                assert (
                    parser.channel == old.channel
                ), "All parsers must have the same channel"
                if not parser._namespace_handlers:
                    parser._namespace_handlers = set()
                parser._namespace_handlers.add(self)
                _dispatcher.parsers.append(
                    getattr(parser, _parser_method_name, parser._parse)
                )

    def handle_response(self, response: MerossMessage, /):
        """Entry point for handling a received message for this namespace.
        This is invoked by Device._handle after routing the message to
        the proper NamespaceHandler based off the namespace in the header.
        """
        # TODO: save all of the last sent/received payloads for a ns_handler
        # for diagnostics (GET/ACK/SET/PUSH/DEL)
        self.last_rx_epoch = self.device.last_rx_epoch
        self.polling_epoch_next = self.last_rx_epoch + self.polling_period
        try:
            self.handler(response)
        except Exception as exception:
            self.log_exception(exception, self.handler.__name__, response.payload)

    def log_exception(self, exception: Exception, function_name: str, payload, /):
        # TODO: migrate to Loggable so we have more flexibility in logging
        device = self.device
        device.log_exception(
            device.WARNING,
            exception,
            "%s(%s).%s: payload=%s",
            self.__class__.__name__,
            self.ns,
            function_name,
            _any=payload,
            timeout=604800,
        )

    def log_parser_exception(self, exception: Exception, payload, /):
        device = self.device
        device.log_exception(
            device.WARNING,
            exception,
            "%s(%s).%s: payload=%s",
            self.__class__.__name__,
            self.ns,
            self.parsers[payload[self.ns.key_idx]].__name__,
            _any=payload,
            timeout=14400,
        )

    def _handle_list(self, message: MerossMessage, /):
        """
        splits and forwards the received NS payload to
        the registered entity(es).
        This handler si optimized for list payloads:
        "payload": { "{self.ns.key}": [{"{self.ns.key_idx}":...., ...}] }
        Under normal conditions the loop is optimized with direct parser lookup
        and invocation without caching any intermediate variable since this is the 99%
        expected pattern. The most-likely exceptions are when no parser is registered
        for the channel (KeyError) or when the payload is not a list (TypeError).
        These will be managed so that they'll don't recur anymore.
        """
        key_idx = self.ns.key_idx
        for p_channel in message.payload[self.ns.key]:
            try:
                self.parsers[p_channel[key_idx]](p_channel)
            except KeyError as ke:
                self._handle_missing_parser(p_channel, ke)
            except Exception as e:
                # this might be expected: the key payload is not a list
                if type(p_channel) is str:  # enumerating dict keys
                    self.handler = self._handle_dict
                    self._handle_dict(message)
                    return
                else:
                    self.log_parser_exception(e, p_channel)

    def _handle_dict(self, message: MerossMessage, /):
        """
        splits and forwards the received NS payload to
        the registered entity(es).
        This handler si optimized for dict payloads:
        "payload": { "key_namespace": {"channel":...., ...} }
        """
        payload = message.payload[self.ns.key]
        try:
            self.parsers[payload[self.ns.key_idx]](payload)
        except KeyError as ke:
            if ke.args[0] == self.ns.key_idx:
                # might be expected for ns with no channels
                # for example EntityNamespaceMixin
                self.parsers[None](payload)
            else:
                self._handle_missing_parser(payload, ke)
        except Exception as e:
            # this might be expected: the payload is not a dict
            # final fallback to the safe _handle_generic
            if type(payload) is not dict:
                self.handler = self._handle_generic
                self._handle_generic(message)
            else:
                self.log_parser_exception(e, payload)

    def _handle_generic(self, message: MerossMessage, /):
        """
        splits and forwards the received NS payload to
        the registered entity(es)
        This handler can manage both lists or dicts or even
        payloads without the "channel" key (see namespace Toggle)
        which will default forwarding to channel == None
        """
        payload = message.payload[self.ns.key]
        if type(payload) is dict:
            try:
                self.parsers[payload[self.ns.key_idx]](payload)
            except KeyError as ke:
                if ke.args[0] == self.ns.key_idx:
                    # might be expected for ns with no channels
                    # for example EntityNamespaceMixin
                    self.parsers[None](payload)
                else:
                    self._handle_missing_parser(payload, ke)
        else:
            key_idx = self.ns.key_idx
            for p_channel in payload:
                try:
                    self.parsers[p_channel[key_idx]](p_channel)
                except KeyError as ke:
                    self._handle_missing_parser(p_channel, ke)
                except Exception as e:
                    self.log_parser_exception(e, p_channel)

    def _handle_undefined(self, message: MerossMessage, /):
        self.device.log(
            self.device.DEBUG,
            "Handler undefined for method:%s namespace:%s payload:%s",
            message.method,
            message.namespace,
            _payload=message.payload,
            timeout=14400,
        )

    def parse_list(self, digest: list, /):
        """twin method for _handle_list (same job - different context).
        Used when parsing digest(s) in NS_ALL"""
        key_idx = self.ns.key_idx
        for p_channel in digest:
            try:
                self.parsers[p_channel[key_idx]](p_channel)
            except KeyError as ke:
                self._handle_missing_parser(p_channel, ke)
            except Exception as e:
                self.log_parser_exception(e, p_channel)

    def parse_dict(self, digest: dict, /):
        """twin method for _handle_dict (same job - different context).
        Used when parsing digest(s) in NS_ALL"""
        self.parsers[digest[self.ns.key_idx]](digest)

    def _parse_stub(self, payload, /):
        device = self.device
        device.log(
            device.DEBUG,
            "Parser stub called on namespace:%s payload:%s",
            self.ns,
            _payload=payload,
            timeout=14400,
        )

    def _handle_missing_parser(self, p_channel: dict, ke: KeyError, /):
        """
        Smart handler for KeyError raised when dispatching
        a channel payload to a parser.
        # KeyError here might have been raised because:
        # - key_idx not in p_channel -> critical
        # - no parser registered for this channel -> create parser if possible
        # - KeyError in parser function
        """
        channel = p_channel[self.ns.key_idx]
        if channel in self.parsers:
            self.log_parser_exception(ke, p_channel)
            return

        self.parsers[channel] = self._parse_stub
        self.parsers[channel](p_channel)

    async def async_get(self, *channels):
        """
        Helper to execute a straigth query to get the whole namespace payload
        or a single item/channel and dispatch the response to the internal
        handler bypassing the Device message routing.
        if channel is None the whole namespace is requested.
        """
        if channels:
            ns = self.ns
            response = await self.device.async_request(
                *ns.payload_get.build_get(ns, *channels)
            )
        else:
            response = await self.device.async_request(*self.polling_request)

        self.handle_response(response)
        return response

    async def async_get_safe(self, *channels):
        """
        Helper to execute a straigth query to get the whole namespace payload
        or a single item/channel and dispatch the response to the internal
        handler bypassing the Device message routing.
        if channel is None the whole namespace is requested.
        """
        try:
            return await self.async_get(*channels)
        except Exception as e:
            self.log_exception(e, "async_get", None)

    def schedule_get(
        self,
        *channels,
        task_name: str = "",
    ):
        """
        Helper to schedule a straigth query to get the whole namespace payload.
        This shouldnt be used for namespaces that don't support GET.
        """
        self.device.create_task(
            self.async_get_safe(*channels), task_name or self.ns, eager_start=True
        )

    async def async_set(
        self,
        payload: "JsonDict",
        parser: NamespaceParser | None = None,
        state: "JsonMapping" = mn.EMPTY_DICT,
        /,
    ):
        """
        Helper to request method SET and eventually dispatch the response to the parser
        bypassing the Device and the NamespaceHandler message routing.
        the payload will be wrapped according to the namespace grammar.
        If parser is provided, it will be called back on its _parse method and
        the SET command payload will be automatically set to the parser's channel.
        """
        ns = self.ns
        response = await self.device.async_request(
            *ns.request_set(payload, parser.channel if parser else None)
        )
        if parser:
            # TODO: consider maybe a dedicated _parse_set_xxxx method?
            # also, most namespaces SETACK replies are empty dicts
            # so we just dispatch the request payload (which might be a
            # subset of the whole GET payload).
            # Some namespaces though might return different payloads on SETACK
            # GarageDoor.State or mts100.Temperature
            getattr(parser, f"_parse_{ns.slug_end}", parser._parse)(
                merge_dicts(dict(state), payload) if state else payload
            )
        return response

    async def async_set_c_ex(
        self, payload, parser: NamespaceParser, state: "JsonMapping" = mn.EMPTY_DICT, /
    ):
        """
        Helper to request method SET (only for LIST_C payload types)and eventually dispatch the response to the parser
        bypassing the Device and the NamespaceHandler message routing.
        Thsi is an extended version of async_set which allows to pass a 'state' dict
        which will be merged into the payload before sending.
        TODO: this is a temporary workaround for some namespaces.
        Examples are the Thermostat namespaces (see module devices.thermostat).
        But we could reorganize all together through implementation of a NamespaceHandler
        cache of the device state received through queries. This cache is now implemented
        'per entity' everywhere needed but there are a lot of entities needing it.
        (MLLight._light for instance or various thermostats)
        An idea would be to put a 'generic JSONDict' attribute in NamespaceParser so that
        it would be easy to update it (when invoking NamespaceHandler.handler) in get requests
        and use it when issuing set requests.
        """
        ns = self.ns
        assert ns.payload_set is mn.PayloadType.LIST_IDX, "Only LIST_C supported here"
        response = await self.device.async_request(
            *ns.request_set(payload, parser.channel)
        )
        try:
            payload = response.payload[ns.key][0]
        except (KeyError, IndexError):
            # optimistic update
            if state:
                payload = merge_dicts(dict(state), payload)
        getattr(parser, f"_parse_{ns.slug_end}", parser._parse)(payload)
        return response

    def polling_request_configure(self, payload_type: mn.PayloadType | None, /):
        """The structure of the polling payload is usually 'fixed' in the namespace
        grammar (see merossclient.namespaces.Namespace) but we have some exceptions
        here and there (one example is Refoss EM06) where the 'standard' is not valid.
        This method allows to refine this namespace parser behavior based off current
        device configuration/type at runtime. Needs to be called early on before
        registering any parser.
        Passing None as payload_type configures the default for the namespace.
        TODO: this need further attention when used after the handler initialization (
        in async_trace for example) because the polling_request_channels might have
        already been set and this method would override them losing channels.
        """
        ns = self.ns
        _payload_type = payload_type or ns.payload_get
        if (_payload_type is mn.PayloadType.LIST_IDX_STRICT) or (
            _payload_type is mn.PayloadType.LIST_IDX_DATA_STRICT
        ):
            self.polling_request_channels = []
            self.polling_request = (
                ns,
                mc.METHOD_GET,
                {ns.key: self.polling_request_channels},
            )
            return
        if _payload_type is ns.payload_get:
            # we'll reuse the default in the ns definition
            if ns.can_query:
                self.polling_request = ns.request_default
            return
        match _payload_type:
            case mn.PayloadType.PUSH | mn.PayloadType.PUSH_QUERY:
                self.polling_request = (
                    ns,
                    mc.METHOD_PUSH,
                    mn.EMPTY_DICT,
                )
            case mn.PayloadType.UNSUPPORTED:
                pass  # do nothing
            case _:
                self.polling_request = _payload_type.build_get(ns)

    def polling_request_add_channel(
        self, channel, extra: "MerossPayloadType" = mn.EMPTY_DICT, /
    ):
        # Ensures the channel is set in polling request payload should
        # the ns need it. Also adjusts the estimated polling_response_size.
        try:
            polling_request_channels = self.polling_request_channels
            key_idx = self.ns.key_idx
            for channel_payload in polling_request_channels:
                if channel_payload[key_idx] == channel:
                    break
            else:
                # this is just a shurtcut since 'subId' namespaces do not
                # still expose a channel different than 0. When that changes
                # it'll be a mess.
                channel_payload = (
                    {key_idx: channel, mc.KEY_CHANNEL: 0}
                    if key_idx == mc.KEY_SUBID
                    else {key_idx: channel}
                )
                polling_request_channels.append(channel_payload)

            if extra:
                channel_payload.update(extra)

            self.polling_response_size = (
                self.HEADER_AVG_SIZE
                + len(polling_request_channels) * self.polling_response_item_size
            )
        except AttributeError:
            # polling_request_channels not used for this ns
            self.polling_response_size = (
                self.HEADER_AVG_SIZE
                + len(self.parsers) * self.polling_response_item_size
            )

    def polling_response_size_adj(self, item_count: int, /):
        self.polling_response_size = (
            self.HEADER_AVG_SIZE + item_count * self.polling_response_item_size
        )

    def channels_to_poll(self):
        # snapshot sequence of channels to query (likely needed with all these asyncs)
        return tuple(self.parsers.keys())

    # Polling Strategies:
    # These are configured at initialization time by setting the 'polling_strategy' attribute
    # and invoked by the polling cycle.
    async def async_poll_default(self):
        """
        This is a basic 'default' policy:
        - avoid the request when MQTT available (this is for general 'state' namespaces like NS_ALL) and
        we expect this namespace to be updated by PUSH(es)
        - unless the 'polling_epoch_next' is 0 which means we're re-onlining the device and so
        we like to re-query the full state (even on MQTT)
        """
        device = self.device
        if not (device.mqtt_active and self.polling_epoch_next):
            await device.async_poll_request(self)

    async def async_poll_smart(self):
        """
        This strategy is for those namespaces which might be skipped now and then
        if they don't fit in the current ns_multiple request. Their delaying
        would be no harm since they typically carry rather unchanging values
        or data which are not 'critical'. For those namespaces, polling_period
        is considered the maximum amount of time after which the poll 'has' to
        be done. If it hasn't elapsed then they're eventually packed
        with the outgoing ns_multiple (lazy polling).
        This strategy should also avoid polling when MQTT is active if the namespace
        supports PUSH or we have received at least one PUSH for it (last_rx_push).
        """
        device = self.device
        """ TODO: re-enable this optimization after testing. It looks like our 'knowledge' of
        PUSHed namespaces is not perfect yet and we're skipping needed polls (#607 #609).
        if (
            device._mqtt_active
            and self.polling_epoch_next
            and (self.ns.payload_psh or self.last_rx_push)
        ):
            # on MQTT no need for updates since they're being PUSHed
            return
        """
        if device.polling_epoch >= self.polling_epoch_next:
            if await device.async_poll_request_smart(self):
                return

        # Insert into the lazypoll_requests ordering by least recently polled
        def _lazypoll_key(_handler: NamespaceHandler):
            return _handler.last_poll_epoch - device.polling_epoch

        insort_right(device._lazypoll_requests, self, key=_lazypoll_key)

    async def async_poll_once(self):
        """
        This strategy is for 'constant' namespace data which do not change and only
        need to be requested once (after onlining that is). When polling use
        same queueing policy as async_poll_smart to don't overwhelm the cloud mqtt
        """
        if not self.polling_epoch_next:
            await self.device.async_poll_request_smart(self)

    async def async_poll_chunked(self):
        """
        This strategy allows splitting the request (which might lead to a huge response payload)
        into smaller chunks in order to fit into the device response buffer.
        This was historically developed for Hub devices where the number of subdevices might grow huge
        and the response to a full GET request might exceed the device capabilities (see #244 for insights).
        TODO: we might want to dynamically 'swap' this strategy with async_poll_default
        whenever the number of registered parsers is small enough to fit into the device
        response buffer in one go and avoid all of this mess.
        """
        device = self.device
        if device.mqtt_active and (device.polling_epoch < self.polling_epoch_next):
            # this check is the same as async_poll_default where we expect this ns to be
            # PUSHed when on MQTT
            return

        size_available = device.polling_response_size_available - self.HEADER_AVG_SIZE
        if size_available < self.polling_response_item_size:
            if device._multiple_requests:
                await device.async_poll_flush()
                size_available = (
                    device.polling_response_size_available - self.HEADER_AVG_SIZE
                )
            else:
                device.log(
                    device.WARNING,
                    "%s(%s).async_poll_chunked: not enough space to add polling request (available:%s, device max:%s)",
                    self.__class__.__name__,
                    self.ns,
                    size_available,
                    device.device_response_size_max,
                    timeout=14400,
                )
                return  # defer to next (hopefully)

        if device.should_limit_cloud_polling:
            return  # defer to next (hopefully)

        # Previous implementation was just splitting-up the requests in fixed amounts
        # determined at design time.
        # New implementation tries to leverage the knowledge of allowed response buffers
        # in the device to fill up the most subdevices requests per message.
        channels = iter(self.channels_to_poll())
        channels_payload = self.polling_request_channels
        channels_payload.clear()
        self.polling_response_size = self.HEADER_AVG_SIZE
        while True:
            if size_available > self.polling_response_item_size:
                try:
                    channels_payload.append({self.ns.key_idx: next(channels)})
                    size_available -= self.polling_response_item_size
                    self.polling_response_size += self.polling_response_item_size
                    continue
                except StopIteration:
                    if channels_payload:
                        await device.async_poll_request(self)
                    # no need to flush multiple since the polling loop
                    # will continue with standard handling
                    break

            if channels_payload:
                await device.async_poll_request(self)
            if device._multiple_requests:
                # ensure we (eventually) flush multiple requests
                await device.async_poll_flush()

            # reset for next chunk
            channels_payload.clear()
            self.polling_response_size = self.HEADER_AVG_SIZE
            size_available = (
                device.polling_response_size_available - self.HEADER_AVG_SIZE
            )
            if size_available < self.polling_response_item_size:
                # This is pathological since we've just flushed everything
                device.log(
                    device.WARNING,
                    "%s(%s).async_poll_chunked: not enough space to add polling request (available:%s, device max:%s)",
                    self.__class__.__name__,
                    self.ns,
                    size_available,
                    device.device_response_size_max,
                    timeout=14400,
                )
                break

    async def async_poll_diagnostic(self):
        """
        This strategy is for namespace polling when diagnostics sensors are detected and
        installed due to any unknown namespace parsing (see self._parse_undefined_dict).
        This in turn needs to be removed from polling when diagnostic sensors are disabled.
        The strategy itself is the same as async_poll_smart; the polling settings
        (period, payload size, etc) has been defaulted in self.__init__ when the definition
        for the namespace polling has not been found in POLLING_STRATEGY_CONF
        """
        device = self.device
        if device.mqtt_active and self.polling_epoch_next and self.ns.has_psh:
            # on MQTT no need for updates since they're being PUSHed
            return

        if device.polling_epoch >= self.polling_epoch_next:
            await device.async_poll_request_smart(self)

    async def async_trace(self, async_request_func: "Device.AsyncRequestFunc", /):
        """
        Used while tracing abilities. Depending on our 'knowledge' of this ns
        we're going a straigth route (when the ns is well-known) or experiment some
        euristics.
        TODO: try to refine our knowledge by seeing if our PayloadType.LIST_C_STRICT GET
        really needs to be that verbose: it looks like Meross app often uses 'plain empty' GET
        (much like namespaces where we found PUSH working as a query method).
        In general, current Namespace class restructure (dec 2025) tried to mantain our knowledge from
        the 'field' but that might likely be too much for many of these namespaces.
        """

        ns = self.ns

        async def _async_wrapped_get(payload: "JsonDict"):
            try:
                return await async_request_func(ns, mc.METHOD_GET, payload)
            except Exception:
                # Right now we just keep on going..
                # It would be better to detect if the device is offline and leave the whole
                # tracing at that point (maybe).
                return None

        async def _async_wrapped_push():
            try:
                return await async_request_func(ns, mc.METHOD_PUSH, mn.EMPTY_DICT)
            except Exception:
                # Right now we just keep on going..
                # It would be better to detect if the device is offline and leave the whole
                # tracing at that point (maybe).
                return None

        if ns.grammar is mn.Grammar.STABLE:
            try:
                match ns.payload_get:
                    case (
                        mn.PayloadType.LIST_IDX
                        | mn.PayloadType.LIST_IDX_STRICT
                        | mn.PayloadType.LIST_IDX_DATA_STRICT
                    ):
                        try:
                            if self.polling_request_channels:
                                await async_request_func(*self.polling_request)
                            else:
                                # when a 'LIST_C_STRICT' namespace has no registered parsers, self.polling_request will fail
                                # so we use the mocked default request
                                await async_request_func(*ns.request_default)
                        except AttributeError as ae:
                            if ae.name == "polling_request_channels":
                                # might be if payload_type is LIST_C though...
                                # so we use the mocked default request
                                await async_request_func(*ns.request_default)
                    case mn.PayloadType.UNSUPPORTED:
                        if ns.has_psq:
                            await _async_wrapped_push()
                    case _:
                        await async_request_func(*self.polling_request)

            except Exception:
                # TODO: log exception?
                pass

            return

        ns_key = ns.key
        ns_key_index = ns.key_idx
        match ns.grammar:
            case mn.Grammar.EXPERIMENTAL:
                # These are typically known in their structure and likely to be channelized
                # supporting at least GET. We'll check if the 'channelization' works and how
                # This is inspired by 'lacking of state polling (#538)' issue and was initially
                # specifically implemented for GarageDoor.State. Other issues that might be due
                # to the same 'structural querying format error' are #517 and others involving
                # the namespaces marked as EXPERIMENTAL in our mn.grammar
                await _async_wrapped_push()
                await _async_wrapped_get({})

                channels = self.parsers.keys() or self.device.descriptor.channels

                channels_count = len(channels)
                channels_payload = [{ns_key_index: channel} for channel in channels]
                # We'll try then querying with those different payload structures as they're well known
                # for channelized devices, starting from the most complex (verbose) to the least one.
                # If any of these works it will candidate for this NamespaceHandler polling_request format.
                detected_request_payload_type: mn.PayloadType | None = None

                async def _async_check(_payload: "MerossPayloadType"):
                    try:
                        _response = await async_request_func(
                            ns, mc.METHOD_GET, _payload
                        )
                        payload = _response.payload[ns_key]
                        if type(payload) is list:
                            return channels_count == len(payload)
                        else:  # dict
                            return channels_count == 1
                    except Exception:
                        return False

                if await _async_check({ns_key: channels_payload}):
                    detected_request_payload_type = mn.PayloadType.LIST_IDX_STRICT

                # check ordered from more to less 'data heavy' payloads
                # so that the last one working (less data) is the fallback
                for _payload_type in (
                    mn.PayloadType.DICT_IDX_65535,
                    mn.PayloadType.DICT_IDX_STRICT,
                    mn.PayloadType.LIST_IDX,
                    mn.PayloadType.DICT_IDX,
                ):
                    if await _async_check(_payload_type.build(ns)):
                        detected_request_payload_type = _payload_type

                if detected_request_payload_type:
                    # this will override the request_payload format from its default
                    self.polling_request_configure(detected_request_payload_type)
                    return

                # If our 'well-known' heuristics don't work, try these exotic queries:
                # looking for DICT_C_STRICT request type
                for channel_payload in channels_payload:
                    await _async_wrapped_get({ns_key: channel_payload})
                # Also check if hub namespaces indexed by "subId" maybe also need a "channel"
                if ns_key_index == mc.KEY_SUBID:
                    await _async_wrapped_get(
                        {
                            ns_key: [
                                {ns_key_index: channel, mc.KEY_CHANNEL: 0}
                                for channel in channels
                            ]
                        }
                    )

            case mn.Grammar.UNKNOWN:
                # We don't know yet how to query this ns so we'll brute-force it
                if ns.has_psh:
                    if response := await _async_wrapped_push():
                        ns_key = mn.Namespace.infer_key(ns, response.payload)

                if ns.has_get:
                    if response := await _async_wrapped_get({}):
                        ns_key = mn.Namespace.infer_key(ns, response.payload)
                    elif response := await _async_wrapped_get({ns.key: []}):
                        ns_key = ns.key
                    else:
                        # ns.key might be wrong or verb GET unsupported
                        if ns_key and (ns_key != ns.key):
                            # try the namespace key from PUSH attempt
                            response = await _async_wrapped_get({ns_key: []})
                        if (not response) and ns.key.endswith("x"):
                            # euristic(!)
                            ns_key = ns.key[:-1]
                            response = await _async_wrapped_get({ns_key: []})
                        if not response:
                            # no chance
                            return

                    response_payload = response.payload.get(ns_key)  # type: ignore
                    if response_payload or (type(response_payload) is not list):
                        return
                    # the namespace might need a channel index in the request
                    subdevices = self.device.descriptor.subdevices
                    if subdevices is None:  # it is not a hub
                        await _async_wrapped_get({ns_key: [{mc.KEY_CHANNEL: 0}]})
                    else:  # it is an hub
                        subdevices = [subdevice[mc.KEY_ID] for subdevice in subdevices]
                        # typical 'legacy' devices are queried by "id"
                        if response := await _async_wrapped_get(
                            {
                                ns_key: [
                                    {mc.KEY_ID: subdevice_id}
                                    for subdevice_id in subdevices
                                ]
                            },
                        ):
                            response_payload = response.payload.get(ns_key)
                            if response_payload:
                                return
                        # many other new ones (ms130 for example) need a "subId"
                        if response := await _async_wrapped_get(
                            {
                                ns_key: [
                                    {mc.KEY_SUBID: subdevice_id}
                                    for subdevice_id in subdevices
                                ]
                            },
                        ):
                            response_payload = response.payload.get(ns_key)
                            if response_payload:
                                return
                        # finally: try also setting a "channel" (it is carried in messages from the subdevice)
                        await _async_wrapped_get(
                            {
                                ns_key: [
                                    {mc.KEY_SUBID: subdevice_id, mc.KEY_CHANNEL: 0}
                                    for subdevice_id in subdevices
                                ]
                            },
                        )


class PhysicalDevice(AbstractClient):
    """Common base for physical devices including Hub-paired (sub)devices."""

    if TYPE_CHECKING:
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

    @cached_property
    @abstractmethod
    def tz(self, /) -> tzinfo:
        raise NotImplementedError("tz")

    @cached_property
    @abstractmethod
    def ns_handlers(self, /) -> "Mapping[str, NamespaceHandler]":
        raise NotImplementedError("ns_handlers")


class Device(PhysicalDevice):
    """Class to manage a Meross device. This is the main class of the library
    and provides the core functionalities to interact with the device (be it an Hub or a standard device),
    manage its state, and handle its namespaces."""

    if TYPE_CHECKING:

        class Args(AbstractClient.Args):
            descriptor: NotRequired[DeviceDescriptor]

        class ConnectArgs(AbstractClient.ConnectArgs):
            pass

        descriptor: Final[DeviceDescriptor]  # type:ignore[override]

        NAMESPACES: ClassVar[mn.NamespacesMapType]
        """Accesses the namespaces definitions for this Device. This could be overriden
        when needed to extend with other namespaces (this is actually true for Hub). This
        way, when we're working only with standard devices we don't need to import the namespaces
        only relevant to hubs."""

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
        This attribute works as a proxy for the actual MQTT client connection state (is_connected) and
        need to be kept in sync (This is mostly accomplished in AbstractMQTTConnection.Client)."""
        _clients: Final[dict[Transport, AbstractClient]]
        _clients_connected: Final[dict[Transport, AbstractClient]]

        ns_handlers: Final[dict[str, NamespaceHandler]]
        handler_all: Final[NamespaceHandler]

        tz: tzinfo

        device_response_size_min: int
        device_response_size_max: int
        multiple_max: int
        _multiple_requests: list[NamespaceHandler]
        _multiple_response_size: int

        _polling_delay: int
        _polling_unsub: TimerHandle | None
        _polling_task: Task | None
        polling_epoch: Final[float]
        """Time of current/last polling cycle epoch."""
        _lazypoll_requests: list[NamespaceHandler]

    HEARTBEAT_TIMEOUT = 300
    TRANSPORT = T_AUTO  # type: ignore[override]
    NAMESPACES = mn.NAMESPACES

    __SLOTS__ = (
        "preferred_transport",
        "polling_period" "transport",
        "client",
        "bluetooth",
        "http",
        "mqtt",
        "mqtt_active",
        "_clients",
        "_clients_connected",
        "ns_handlers",
        "handler_all",
        "tz",
        "device_response_size_min",
        "device_response_size_max",
        "multiple_max",
        "_multiple_requests",
        "_multiple_response_size",
        "_polling_delay",
        "_polling_unsub",
        "_polling_task",
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
        self.handler_all = self._create_handler(mn.Appliance_System_All)
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
        self._polling_unsub = None
        self._polling_task = None
        self.polling_epoch = self.time()

    @override
    async def async_shutdown(self):
        await self.async_poll_stop()
        await super().async_shutdown()
        # Clients will be forcibly disconnected/shutdown at this point since the base class shutdown
        # will disconnect the device and so trigger the clients disconnect logic.
        # In order to leave the client 'alive' call remove_client before shutting down the device.
        for client in tuple(self._clients.values()):
            await client.async_shutdown()
        for handler in self.ns_handlers.values():
            handler.shutdown()
        self.ns_handlers.clear()
        del self.handler_all  # type: ignore
        self._lazypoll_requests.clear()

    # interface: AbstractClient
    @override
    async def async_connect(self, /, **kwargs: "Unpack[ConnectArgs]"):
        handler_all = self.handler_all
        # use pre 3.13 compatible syntax/semantics
        for earliest_connect in asyncio.as_completed(
            {
                self.create_task(
                    _client.async_request(*handler_all.polling_request),
                    f".async_poll_{_client.TRANSPORT}_task",
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
                    raise MerossError("No transport available to send the request")
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
        self._clients.pop(client.TRANSPORT)
        setattr(self, client.TRANSPORT, None)
        if client.is_connected:
            self.on_client_disconnect(client)
        client.connect_broadcast.remove(self.on_client_connect)
        client.disconnect_broadcast.remove(self.on_client_disconnect)
        client.tx_broadcast.remove(self.on_tx)
        client.rx_broadcast.remove(self.on_rx)

    def on_client_connect(self, client: "AbstractClient", /):
        self._clients_connected[client.TRANSPORT] = client

    def on_client_disconnect(self, client: "AbstractClient", /):
        self._clients_connected.pop(client.TRANSPORT)
        if self._clients_connected:
            if self.client is client:
                self._switch_client(next(iter(self._clients_connected.values())))
        elif self.is_connected:
            self.on_disconnect()

    def _switch_client(self, client: "AbstractClient"):
        self.client = client  # type: ignore[assignment]
        self.transport = client.TRANSPORT  # type: ignore[assignment]
        self.log(self.DEBUG, "Switching transport to %s", self.transport)

    def _create_handler(self, ns: "mn.Namespace", /):
        """Called by the base device message parsing chain when a new
        NamespaceHandler need to be defined (This happens the first time
        the namespace enters the message handling flow)"""
        return NamespaceHandler(self, ns)

    def get_handler(self, ns: "mn.Namespace", /):
        try:
            return self.ns_handlers[ns]
        except KeyError:
            return self._create_handler(ns)

    def get_handler_by_name(self, namespace: str, /):
        try:
            return self.ns_handlers[namespace]
        except KeyError:
            return self._create_handler(self.NAMESPACES[namespace])

    def register_parser(self, parser: "NamespaceParser", ns: "mn.Namespace", /):
        self.get_handler(ns).register_parser(parser)

    def register_parser_ex(
        self,
        parser: "NamespaceParser",
        *nss: "mn.Namespace",
    ):
        """Register a parser for multiple namespaces. Abilities are checked for namespaces availability."""
        ability = self.descriptor.ability
        for ns in (_ns for _ns in nss if _ns in ability):
            self.get_handler(ns).register_parser(parser)

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

    def _poll(self, namespace: str | None = None):
        self._polling_unsub = None
        self._polling_task = task = self.create_task(
            self._async_poll(namespace), f"._poll({namespace})", eager_start=False
        )
        return task

    async def _async_poll(self, namespace: str | None):
        self.polling_epoch = epoch = self.time()  # type: ignore[assignment]
        self.log(self.DEBUG, "Polling begin")
        try:
            if self.is_connected and (
                (self.last_rx_epoch > self.last_tx_epoch)
                or ((epoch - self.last_tx_epoch) < (self.polling_period - 2))
            ):
                # perform some heartbeats in case
                if (
                    (http := self.http)
                    and (self.client is not http)
                    and (self.preferred_transport is T_HTTP)
                    and ((epoch - http.last_tx_epoch) > self.HEARTBEAT_TIMEOUT)
                ):
                    try:
                        self.handler_all.handle_response(
                            await http.async_request(*self.handler_all.polling_request)
                        )
                        namespace = self.handler_all.ns
                    except Exception:
                        pass

                if (
                    (mqtt := self.mqtt)
                    and mqtt.connection.can_publish
                    and ((epoch - mqtt.last_rx_epoch) > self.HEARTBEAT_TIMEOUT)
                ):
                    try:
                        self.handler_all.handle_response(
                            await mqtt.async_request(*self.handler_all.polling_request)
                        )
                        namespace = self.handler_all.ns
                    except Exception:
                        pass

            else:  # offline or 'likely' offline (failed last request)
                namespace = (await self.async_connect()).namespace

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
            for handler in [
                handler
                for handler in self.ns_handlers.values()
                if (handler.ns != namespace)
            ]:
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
            self.log_exception(self.WARNING, e, "_async_poll")
        finally:
            self._polling_task = None

        self._polling_unsub = self.schedule_callback(
            self._polling_delay, self._poll, None
        )
        self.log(self.DEBUG, "Polling end")

    async def async_poll_stop(self):
        """Ensure we're not polling nor any schedule is in place."""
        if self._polling_unsub:
            self._polling_unsub.cancel()
            self._polling_unsub = None
        elif self._polling_task:
            self._polling_task.cancel("async_poll_stop")
            try:
                await self._polling_task
            except asyncio.CancelledError:
                pass

    async def async_poll_full(self):
        """Stops an ongoing poll if any and executes a full poll (like when onlining)."""
        await self.async_poll_stop()
        # BEWARE/TODO: this might overlap with async_shutdown and is not protected.
        for handler in self.ns_handlers.values():
            handler.polling_epoch_next = 0.0
        # this will also restart/schedule the cycle
        await self._poll()

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
                    if handler.ns != _response.namespace:
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
                handler.ns,
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
    )

    def __init__(
        self, id: str, parent: "Device", **kwargs: "Unpack[PhysicalDevice.Args]"
    ):
        self.async_request = parent.async_request
        self.ns_handlers = parent.ns_handlers
        kwargs["key"] = parent.key
        kwargs["from_"] = parent.from_
        kwargs["trigger_src"] = parent.trigger_src
        kwargs["descriptor"] = parent.descriptor
        kwargs["timeout"] = parent.timeout
        kwargs["loop"] = parent.loop
        super().__init__(id, parent, **kwargs)

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
    def tz(self):
        return self.parent.tz

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
