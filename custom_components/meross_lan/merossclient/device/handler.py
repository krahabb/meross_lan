from bisect import insort_right
from typing import TYPE_CHECKING, override

from .. import extract_dict_payloads, logging, merge_dicts
from ..protocol import const as mc, namespaces as mn
from ..protocol.message import MerossMessage
from .parser import NamespaceParser

if TYPE_CHECKING:
    from typing import (
        Any,
        Callable,
        ClassVar,
        Coroutine,
        Final,
        Iterable,
        NotRequired,
        Unpack,
    )

    from . import Device
    from ..protocol import types as mt


class NamespaceHandler(logging.Loggable):
    """
    This is the root class for somewhat dynamic namespace handlers.
    Every device keeps its own list of method handlers indexed through
    the message namespace in order to speed up parsing/routing when receiving
    a message from the device see Device.ns_handlers and
    Device._handle to get the basic behavior.

    - handler: specify a custom handler method for this namespace. By default
    it will be looked-up in the device definition (looking for _handle_xxxxxx)

    """

    if TYPE_CHECKING:
        type HandlerFunc = Callable[[MerossMessage], None]
        type ParserFunc = Callable[[mt.JsonMapping], None]
        # need to use Any because of covariance issues with NamespaceHandler
        type PollingStrategyFunc = Callable[[Any], Coroutine]
        type PollingConfigType = tuple[int, int, PollingStrategyFunc | None]
        """PollingConfigType is a tuple of (polling_period, polling_period_cloud, polling_strategy).
        This is used to configure the handler polling policy setting polling periods and strategy processor."""

        HEADER_AVG_SIZE: Final[int]
        """(rough) estimate of the header part of any response"""
        POLLING_CONFIG_DEFAULT: ClassVar[PollingConfigType]
        """(final) default polling configuration. This is used if no config is being passed
        at NamespaceHandler initialization time and no entry is found in POLLING_CONFIG_MAP."""
        POLLING_CONFIG_MAP: Final[dict[mn.Namespace, PollingConfigType]]
        """Centralized polling config parameters for namespaces. This is used if no config is being passed
        at NamespaceHandler initialization time."""

        id: Final[mn.Namespace]  # type: ignore[override]
        parent: Final[Device]  # type: ignore[override]

        handler: HandlerFunc
        parsers: Final[dict[Any, ParserFunc]]
        parser_class: type[NamespaceParser] | None
        digest: mt.JsonMapping | mt.JsonArray | None

        polling_strategy: PollingStrategyFunc | None
        polling_request: mt.MerossRequestType
        polling_request_channels: mt.JsonList  # on demand instance

        last_rx_push: mt.JsonMapping | None
        # TODO: implement caching of all methods responses

        class Args(logging.Loggable.Args):
            handler: NotRequired["NamespaceHandler.HandlerFunc"]
            config: NotRequired["NamespaceHandler.PollingConfigType"]
            parser_class: NotRequired[type[NamespaceParser]]
            channels: NotRequired[Iterable[int]]

    __SLOTS__ = (
        "handler",
        "parsers",
        "parser_class",
        "digest",
        "last_rx_epoch",
        "last_poll_epoch",
        "next_poll_epoch",
        "polling_strategy",
        "polling_period",
        "polling_period_cloud",
        "polling_response_size",
        "polling_request",
        "polling_request_channels",
        "last_rx_push",
    )

    def __init__(
        self,
        id: "mn.Namespace",
        parent: "Device",
        /,
        **kwargs: "Unpack[NamespaceHandler.Args]",
    ):
        assert id not in parent.ns_handlers, ("Namespace already registered", id)
        self.parsers = {}

        try:
            self.polling_period, self.polling_period_cloud, self.polling_strategy = (
                kwargs.pop("config")
            )
        except KeyError:
            self.polling_period, self.polling_period_cloud, self.polling_strategy = (
                self.POLLING_CONFIG_MAP.get(id, self.POLLING_CONFIG_DEFAULT)
            )

        if id.key_digest:
            try:
                # probe existence of digest for this namespace to speed up later checks when parsing messages
                self.digest = id.get_digest(
                    parent.descriptor.digest or parent.descriptor.control
                )
            except (KeyError, NotImplementedError) as e:
                self.digest = None
        else:
            self.digest = None

        try:
            self.parser_class = parser_class = kwargs.pop("parser_class")
        except KeyError:
            self.parser_class = None
            # by default we calculate 1 item/channel per payload but we should
            # refine this whenever needed
            self.polling_response_size = (
                NamespaceHandler.HEADER_AVG_SIZE + id.payload_item_size
            )
            try:
                self.handler = kwargs.pop("handler")
            except KeyError:
                self.handler = getattr(
                    parent, f"_handle_{id.replace('.', '_')}", self._handle
                )
        else:
            assert (
                "handler" not in kwargs
            ), "Cannot specify both handler and parser_class"
            # optimized register_parser_class and register_parser
            self.id = id  # preset self.id for parser._namespace_registered
            self.handler = self._handle_list
            for channel in kwargs.pop("channels", parent.descriptor.channels):
                parser = parser_class(channel, parent, ns=id)
                self.parsers[channel] = getattr(
                    parser, f"_parse_{id.slug_end}", parser._parse
                )
                parser._namespace_registered(self)
                # polling_request_channels will be eventually setup
                # by polling_request_configure later on
            self.polling_response_size = (
                NamespaceHandler.HEADER_AVG_SIZE
                + len(self.parsers) * id.payload_item_size
            )
        self.last_rx_epoch = self.last_poll_epoch = self.next_poll_epoch = 0.0
        self.last_rx_push = None

        super().__init__(id, parent, **kwargs)
        self.polling_request_configure(
            mn.PayloadType.LIST_IDX_STRICT
            if self.polling_strategy is NamespaceHandler.async_poll_chunked
            else None
        )
        parent.ns_handlers[id] = self
        parent.shutdown_broadcast.add(self.shutdown)

    def shutdown(self):
        super().shutdown()
        self.parent.shutdown_broadcast.remove(self.shutdown)
        del self.parent.ns_handlers[self.id]
        del self.handler  # especially this one
        assert not self.parsers, "parsers should have been cleared before shutdown"

    def register_parser(
        self,
        parser: NamespaceParser,
        /,
    ):
        """Installs a dedicated parser for the given channel payload.
        Calling this multiple times for the same channel is prohibited
        by design even though the dispatching model allows (_DispatcherParser)
        multiple recipients. Use register_parsers instead."""
        channel = parser.channel
        assert channel not in self.parsers, "Parser already registered for channel"
        self.parsers[channel] = getattr(
            parser, f"_parse_{self.id.slug_end}", parser._parse
        )
        parser._namespace_registered(self)
        self.handler = self._handle_list
        return self.polling_request_add_channel(channel)

    def register_parsers(self, *parsers: NamespaceParser):
        """Registers a whole set of parsers at once for the same channel payload.
        This will automatically install a dispatcher. This feature is useful to avoid having
        to define a dedicated parser class just to dispatch data to multiple entities.
        This will in turn remove the need for references that need to be maintained."""
        channel = parsers[0].channel
        assert channel not in self.parsers, "Parser already registered for channel"
        self.parsers[channel] = _dispatcher = NamespaceParser.Dispatcher()
        _parser_method_name = f"_parse_{self.id.slug_end}"
        for parser in parsers:
            assert parser.channel == channel, "All parsers must have the same channel"
            _dispatcher.parsers.append(
                getattr(parser, _parser_method_name, parser._parse)
            )
            parser._namespace_registered(self)
        self.handler = self._handle_list
        return self.polling_request_add_channel(channel)

    def swap_parsers(
        self, old: NamespaceParser, new: NamespaceParser, *extra: NamespaceParser
    ):
        assert old.channel == new.channel, "channel mismatch"
        del old.handlers[self.id]
        if extra:
            # install a dispatcher
            _parse_method_name = f"_parse_{self.id.slug_end}"
            self.parsers[new.channel] = _dispatcher = NamespaceParser.Dispatcher(
                getattr(new, _parse_method_name, new._parse)
            )
            new._namespace_registered(self)
            for parser in extra:
                assert (
                    parser.channel == new.channel
                ), "All parsers must have the same channel"
                _dispatcher.parsers.append(
                    getattr(parser, _parse_method_name, parser._parse)
                )
                parser._namespace_registered(self)
        else:
            self.parsers[new.channel] = getattr(
                new, f"_parse_{self.id.slug_end}", new._parse
            )
            new._namespace_registered(self)

    def handle_response(self, response: MerossMessage, /):
        """Entry point for handling a received message for this namespace.
        This is invoked by Device whenever a message for this ns is received.
        This method acts as a wrapper for the actual handler to catch and
        log any exception that might occur and to do some house-keeping.
        """
        # TODO: save all of the last sent/received payloads for a ns_handler
        # for diagnostics (GET/ACK/SET/PUSH/DEL)
        self.last_rx_epoch = self.parent.last_rx_epoch
        self.next_poll_epoch = self.last_rx_epoch + self.polling_period
        try:
            self.handler(response)
        except Exception as exception:
            self.log_exception(
                self.WARNING,
                exception,
                "handle_response (payload: %s)",
                _payload=response.payload,
            )

    def log_parser_exception(self, exception: Exception, payload, /):
        self.log_exception(
            self.WARNING,
            exception,
            "parser function '%s': payload=%s",
            self.parsers[payload[self.id.key_idx]].__name__,
            _any=payload,
            timeout=14400,
        )

    def _handle_list(self, message: MerossMessage, /):
        """
        splits and forwards the received NS payload to
        the registered entity(es).
        This handler si optimized for list payloads:
        "payload": { "{self.id.key}": [{"{self.id.key_idx}":...., ...}] }
        Under normal conditions the loop is optimized with direct parser lookup
        and invocation without caching any intermediate variable since this is the 99%
        expected pattern. The most-likely exceptions are when no parser is registered
        for the channel (KeyError) or when the payload is not a list (TypeError).
        These will be managed so that they'll don't recur anymore.
        """
        key_idx = self.id.key_idx
        for p_channel in message.payload[self.id.key]:
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
        payload = message.payload[self.id.key]
        try:
            self.parsers[payload[self.id.key_idx]](payload)
        except KeyError as ke:
            if ke.args[0] == self.id.key_idx:
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
        payload = message.payload[self.id.key]
        if type(payload) is dict:
            try:
                self.parsers[payload[self.id.key_idx]](payload)
            except KeyError as ke:
                if ke.args[0] == self.id.key_idx:
                    # might be expected for ns with no channels
                    # for example EntityNamespaceMixin
                    self.parsers[None](payload)
                else:
                    self._handle_missing_parser(payload, ke)
        else:
            key_idx = self.id.key_idx
            for p_channel in payload:
                try:
                    self.parsers[p_channel[key_idx]](p_channel)
                except KeyError as ke:
                    self._handle_missing_parser(p_channel, ke)
                except Exception as e:
                    self.log_parser_exception(e, p_channel)

    def _handle(self, msg: MerossMessage, /):
        """Default handler for a namespace message. This implementation works as a stub and is being invoked if no
        better handler is found. Handler functions can be installed per instance at construction or
        by overriding this method definition in custom NamespaceHandlers."""
        self.log(
            self.DEBUG, "Handler undefined (message:%s)", _message=msg, timeout=14400
        )

    def parse_digest(self, digest: "mt.JsonMapping | mt.JsonArray", /):
        """Used when parsing digest(s) in Appliance.System.All."""
        for p_channel in extract_dict_payloads(digest):
            try:
                self.parsers[p_channel[self.id.key_idx]](p_channel)
            except KeyError as ke:
                self._handle_missing_parser(p_channel, ke)
            except Exception as e:
                self.log_parser_exception(e, p_channel)

    def _parse_stub(self, payload, /):
        self.log(
            self.DEBUG,
            "Called parser stub (payload: %s)",
            _payload=payload,
            timeout=14400,
        )

    def _handle_missing_parser(self, p_channel: "mt.JsonMapping", ke: KeyError, /):
        """
        Smart handler for KeyError raised when dispatching
        a channel payload to a parser.
        # KeyError here might have been raised because:
        # - key_idx not in p_channel -> critical
        # - no parser registered for this channel -> create parser if possible
        # - KeyError in parser function
        """
        channel = p_channel[self.id.key_idx]
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
        response = await self.parent.async_request(
            *(
                self.id.payload_get.build_get(self.id, *channels)
                if channels
                else self.polling_request
            )
        )
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
            self.log_exception(self.WARNING, e, "async_get_safe")

    def schedule_get(
        self,
        *channels,
        task_name: str = "",
    ):
        """
        Helper to schedule a straigth query to get the whole namespace payload.
        This shouldnt be used for namespaces that don't support GET.
        """
        self.create_task(
            self.async_get(*channels), task_name or self.id, eager_start=True
        )

    async def async_set(self, payload: "mt.JsonMapping", /):
        """Helper to request method SET."""
        return await self.parent.async_request(*self.id.request_set(payload))

    async def async_set_parse(
        self,
        payload: "mt.JsonDict",
        parser: NamespaceParser,
        state: "mt.JsonMapping" = mn.EMPTY_DICT,
        /,
    ):
        """
        Helper to request method SET and eventually dispatch the response to the parser
        bypassing the Device and the NamespaceHandler message routing.
        the payload will be wrapped according to the namespace grammar.
        If parser is provided, it will be called back on its _parse method and
        the SET command payload will be automatically set to the parser's channel.
        """
        response = await self.parent.async_request(
            *self.id.request_set(payload, parser.channel)
        )
        # TODO: consider maybe a dedicated _parse_set_xxxx method?
        # also, most namespaces SETACK replies are empty dicts
        # so we just dispatch the request payload (which might be a
        # subset of the whole GET payload).
        # Some namespaces though might return different payloads on SETACK
        # GarageDoor.State or mts100.Temperature
        getattr(parser, f"_parse_{self.id.slug_end}", parser._parse)(
            merge_dicts(dict(state), payload) if state else payload
        )
        return response

    async def async_set_parse_ex(
        self,
        payload,
        parser: NamespaceParser,
        state: "mt.JsonMapping" = mn.EMPTY_DICT,
        /,
    ):
        """
        Helper to request method SET (only for LIST_C payload types)and eventually dispatch the response to the parser
        bypassing the Device and the NamespaceHandler message routing.
        Thsi is an extended version of async_set which allows to pass a 'state' dict
        which will be merged into the payload before sending.
        TODO: this is a temporary workaround for some namespaces.
        Examples are the Thermostat namespaces (see module devices.thermostat).
        But we could reorganize all together through implementation of a NamespaceHandler
        cache of the device state received through queries. This cache should be the ns_payload attribute.
        """
        ns = self.id
        assert ns.payload_set is mn.PayloadType.LIST_IDX, "Only LIST_C supported here"
        response = await self.parent.async_request(
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
        """
        ns = self.id
        _payload_type = payload_type or ns.payload_get
        if (_payload_type is mn.PayloadType.LIST_IDX_STRICT) or (
            _payload_type is mn.PayloadType.LIST_IDX_DATA_STRICT
        ):
            self.polling_request_channels = [
                {ns.key_idx: channel} for channel in self.parsers
            ]
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

    def polling_request_add_channel(self, channel, /):
        """Ensures the channel is set in polling request payload should the ns need it.
        Also adjusts the estimated polling_response_size.
        Returns the channel payload dict to be used for further updates if needed.
        Some ns grammar might not have a so called 'channel_payload'. For those ns
        the return value has no meaning and is an immutable empty dict.
        """
        try:
            polling_request_channels = self.polling_request_channels
            key_idx = self.id.key_idx
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
            self.polling_response_size = (
                NamespaceHandler.HEADER_AVG_SIZE
                + len(polling_request_channels) * self.id.payload_item_size
            )
            return channel_payload
        except AttributeError:
            # polling_request_channels not used for this ns
            self.polling_response_size = (
                NamespaceHandler.HEADER_AVG_SIZE
                + len(self.parsers) * self.id.payload_item_size
            )
            return mn.EMPTY_DICT

    def polling_response_size_adj(self, item_count: int, /):
        self.polling_response_size = (
            NamespaceHandler.HEADER_AVG_SIZE + item_count * self.id.payload_item_size
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
        - unless the 'next_poll_epoch' is 0 which means we're re-onlining the device and so
        we like to re-query the full state (even on MQTT)
        """
        device = self.parent
        if (device.mqtt_active and self.next_poll_epoch) or (
            device.polling_epoch < self.next_poll_epoch
        ):
            return
        await device.async_poll_request_rl(self)

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
        device = self.parent
        """ TODO: re-enable this optimization after testing. It looks like our 'knowledge' of
        PUSHed namespaces is not perfect yet and we're skipping needed polls (#607 #609).
        if (
            device._mqtt_active
            and self.next_poll_epoch
            and (self.id.payload_psh or self.last_rx_push)
        ):
            # on MQTT no need for updates since they're being PUSHed
            return
        """
        if device.polling_epoch >= self.next_poll_epoch:
            if await device.async_poll_request_rl(self):
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
        if not self.next_poll_epoch:
            await self.parent.async_poll_request_rl(self)

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
        device = self.parent
        if (device.mqtt_active and self.next_poll_epoch) or (
            device.polling_epoch < self.next_poll_epoch
        ):
            return

        payload_item_size = self.id.payload_item_size
        size_available = (
            device.polling_response_size_available - NamespaceHandler.HEADER_AVG_SIZE
        )
        if size_available < payload_item_size:
            if device._multiple_requests:
                await device.async_poll_flush()
                size_available = (
                    device.polling_response_size_available
                    - NamespaceHandler.HEADER_AVG_SIZE
                )
            else:
                self.log(
                    self.WARNING,
                    "async_poll_chunked: not enough space to add polling request (available:%s, device max:%s)",
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
        self.polling_response_size = NamespaceHandler.HEADER_AVG_SIZE
        while True:
            if size_available > payload_item_size:
                try:
                    channels_payload.append({self.id.key_idx: next(channels)})
                    size_available -= payload_item_size
                    self.polling_response_size += payload_item_size
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
            self.polling_response_size = NamespaceHandler.HEADER_AVG_SIZE
            size_available = (
                device.polling_response_size_available
                - NamespaceHandler.HEADER_AVG_SIZE
            )
            if size_available < payload_item_size:
                # This is pathological since we've just flushed everything
                self.log(
                    self.WARNING,
                    "async_poll_chunked: not enough space to add polling request (available:%s, device max:%s)",
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
        The strategy itself is the same as async_poll_smart.
        """
        device = self.parent
        if device.mqtt_active and self.next_poll_epoch and self.id.has_psh:
            # on MQTT no need for updates since they're being PUSHed
            return

        if device.polling_epoch >= self.next_poll_epoch:
            await device.async_poll_request_rl(self)

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

        ns = self.id

        async def _async_wrapped_get(payload: "mt.JsonDict"):
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

                channels = self.parsers.keys() or self.parent.descriptor.channels

                channels_count = len(channels)
                channels_payload = [{ns_key_index: channel} for channel in channels]
                # We'll try then querying with those different payload structures as they're well known
                # for channelized devices, starting from the most complex (verbose) to the least one.
                # If any of these works it will candidate for this NamespaceHandler polling_request format.
                detected_request_payload_type: mn.PayloadType | None = None

                async def _async_check(_payload: "mt.MerossPayloadType"):
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
                    subdevices = self.parent.descriptor.subdevices
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

    HEADER_AVG_SIZE = 300
    POLLING_CONFIG_DEFAULT = (0, 0, async_poll_default)
    POLLING_CONFIG_MAP = {}


class VoidNamespaceHandler(NamespaceHandler):
    """Utility class to manage namespaces which should be 'ignored' i.e. we're aware
    of their existence but we don't process them at the device level. This class in turn
    just provides an empty handler and so suppresses any log too (for unknown namespaces)
    done by the base default handling."""

    @override
    def _handle(self, message: "MerossMessage", /):
        pass
