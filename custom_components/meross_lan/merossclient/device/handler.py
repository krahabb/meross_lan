from bisect import insort_right
from typing import TYPE_CHECKING, override

from .. import extract_dict_payloads, logging, merge_dicts
from ..protocol import const as mc, namespaces as mn
from ..protocol.message import MerossMessage
from .parser import NamespaceParser

KEY_CHANNEL = mc.KEY_CHANNEL
KEY_ID = mc.KEY_ID
KEY_SUBID = mc.KEY_SUBID


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
        """Default polling configuration. This is used if no config is being passed
        at NamespaceHandler initialization time and no entry is found in POLLING_CONFIG_MAP."""
        POLLING_CONFIG_NONE: ClassVar[PollingConfigType]
        """Polling configuration representing no polling. This is used to disable polling for a namespace."""
        POLLING_CONFIG_ONCE: Final[PollingConfigType]
        """Common polling configuration for namespaces carrying fixed info."""
        POLLING_CONFIG_MAP: Final[dict[mn.Namespace, PollingConfigType]]
        """Centralized polling config parameters for namespaces. This is used if no config is being passed
        at NamespaceHandler initialization time."""

        id: Final[mn.Namespace]  # type: ignore[override]
        parent: Final[Device]  # type: ignore[override]
        index: Final[mn.IndexType]  # shortcut to id.index

        handler: HandlerFunc
        parsers: Final[dict[mn.IndexValue, ParserFunc | NamespaceParser]]
        parser_class: type[NamespaceParser] | None
        digest: mt.JsonMapping | mt.JsonArray | None

        polling_strategy: PollingStrategyFunc | None
        polling_request: mt.MerossRequestType
        polling_request_payload: mt.JsonList  # on demand instance

        last_rx_push: mt.JsonMapping | None

        class Args(logging.Loggable.Args):
            handler: NotRequired["NamespaceHandler.HandlerFunc"]
            config: NotRequired["NamespaceHandler.PollingConfigType"]
            parser_class: NotRequired[type[NamespaceParser]]
            channels: NotRequired[Iterable[int]]

    __SLOTS__ = (
        "index",
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
        "polling_request_payload",
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
        if id.index is mn.IndexType.subId and not parent.descriptor.is_hub:
            # These namespaces, might be indexed by both 'subId' and/or 'channel'
            # but when used on non hub devices they're definitely using 'channel' index.
            # Our grammar doesn't cover this semantic but we can easily adapt to it
            # here by switching the index to channel for this case.
            self.index = mn.IndexType.channel
        else:
            self.index = id.index

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
                match self.index:
                    case mn.IndexType.id:
                        self.handler = self._handle_subdevice_id
                    case mn.IndexType.subId:
                        self.handler = self._handle_subid
                    case mn.IndexType.channel:
                        self.handler = self._handle_channel_list
                    case _:
                        self.handler = getattr(
                            parent, f"_handle_{id.replace('.', '_')}", self._handle
                        )
        else:
            assert (
                "handler" not in kwargs
            ), "Cannot specify both handler and parser_class"
            # optimized register_parser_class and register_parser
            self.id = id  # preset self.id for parser._namespace_registered
            match self.index:
                case mn.IndexType.channel:
                    self.handler = self._handle_channel_list
                case mn.IndexType.subId:
                    self.handler = self._handle_subid
                case _:
                    assert (
                        False
                    ), "parser_class only supported for 'channel' indexed namespaces"
            for channel in kwargs.pop("channels", parent.descriptor.channels):
                # TODO: place mn.IndexType.channel in the channels kwarg and in descriptor.channels
                index = mn.IndexType.channel(channel)
                self.parsers[index] = parser = parser_class(
                    channel, parent, ns=id, index=index
                )
                parser._namespace_registered((self, index))
                # polling_request_payload will be eventually setup
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
        for index in tuple(self.parsers):
            self.log(self.DEBUG, "Cleaning up dangling parser for index %s", index)
            del self.parsers[index]

    def register_parser(
        self,
        parser: NamespaceParser,
        /,
    ):
        # FIXME/TODO: add an index to the call to make it more flexible
        """Installs a dedicated parser for the given channel payload.
        Calling this multiple times for the same channel is prohibited
        by design even though the dispatching model allows (_DispatcherParser)
        multiple recipients. Use register_parsers instead."""
        index = parser.index
        if self.index is mn.IndexType.subId and index.type is mn.IndexType.id:
            # Temporary fix until better normalization:
            # This is the case of a subdevice registering to a 'subId' indexed namespace
            # We provide here a 'quick' workaround to automatically bind to channel == 0
            # since this seems pretty common.
            index = mn.IndexType.subId(index.value, 0, None)
        assert index.type is self.index, "index type mismatch"
        assert index not in self.parsers, "Parser already registered for index"
        self.parsers[index] = getattr(parser, f"_parse_{self.id.slug_end}", parser)
        parser._namespace_registered((self, index))
        self.polling_request_add_index(index)

    def register_parsers(self, *parsers: NamespaceParser):
        """Registers a whole set of parsers at once for the same channel payload.
        This will automatically install a dispatcher. This feature is useful to avoid having
        to define a dedicated parser class just to dispatch data to multiple entities.
        This will in turn remove the need for references that need to be maintained."""
        index = parsers[0].index
        assert index not in self.parsers, "Parser already registered for index"
        handler_registration = (self, index)
        self.parsers[index] = _dispatcher = NamespaceParser.Dispatcher()
        _parser_method_name = f"_parse_{self.id.slug_end}"
        for parser in parsers:
            assert parser.index is index, "All parsers must have the same index"
            _dispatcher.parsers.append(getattr(parser, _parser_method_name, parser))
            parser._namespace_registered(handler_registration)
        self.polling_request_add_index(index)

    def swap_parsers(
        self, old: NamespaceParser, new: NamespaceParser, *extra: NamespaceParser
    ):
        index = old.index
        assert index is new.index, "index mismatch"
        handler_registration = (self, index)
        old._handler_registrations.remove(handler_registration)
        if extra:
            # install a dispatcher
            _parse_method_name = f"_parse_{self.id.slug_end}"
            self.parsers[index] = _dispatcher = NamespaceParser.Dispatcher(
                getattr(new, _parse_method_name, new)
            )
            new._namespace_registered(handler_registration)
            for parser in extra:
                assert parser.index is index, "All parsers must have the same index"
                _dispatcher.parsers.append(getattr(parser, _parse_method_name, parser))
                parser._namespace_registered(handler_registration)
        else:
            self.parsers[index] = getattr(new, f"_parse_{self.id.slug_end}", new)
            new._namespace_registered(handler_registration)

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

    def parse_digest(self, digest: "mt.JsonMapping | mt.JsonArray", /):
        """Used when parsing digest(s) in Appliance.System.All."""
        for payload in extract_dict_payloads(digest):
            try:
                self.parsers[payload[KEY_CHANNEL]](payload)
            except KeyError as ke:
                self._handle_missing_channel(ke, payload)
            except Exception as e:
                self.log_parser_exception(e, payload)

    def log_handler_exception(self, exception: Exception, payload, /):
        """Logs an error raised inside a handler function. This is typically due
        to malformed payloads with respect to our expectancy. Typical example
        is missing the 'channel' key in a namespace with indexed payloads."""
        self.log_exception(
            self.WARNING,
            exception,
            "handler function '%s': payload=%s",
            self.handler.__name__,
            _any=payload,
            timeout=14400,
        )

    def log_parser_exception(self, exception: Exception, payload, /):
        """Logs an error raised inside a parser function (and not handled there ofc)."""
        self.log_exception(
            self.WARNING,
            exception,
            "parser function '%s': payload=%s",
            self.parsers[self.index.index(payload)].__name__,
            _any=payload,
            timeout=14400,
        )

    def _handle_subdevice_id(self, message: "MerossMessage"):
        """Generalized Hub namespace dispatcher to subdevices."""
        parsers = dict(self.parsers)
        for payload in message.payload[self.id.key]:
            try:
                parsers.pop(payload[KEY_ID])(payload)
            except KeyError as ke:
                if KEY_ID not in payload:
                    self.log_handler_exception(ke, payload)
                    continue
                self._handle_missing_subdevice(ke, payload, payload[KEY_ID])
            except Exception as e:
                self.log_parser_exception(e, payload)

    def _handle_subid(self, message: "MerossMessage"):
        """Handler for those ns which might either refer to a subdevice channel or
        to a device channel depending on the payload. These are typically based on the 'subId' key
        establishing a relationship with an hub subdevice or might skip the subid altogether and
        just refer to the device channel.
        Examples are Appliance.Config.DeviceCfg or Appliance.Control.Sensor.LatestX but there are many more.
        Here, self.parsers keys could be either (subdevice ids, channel) tuples or simple channels.
        """
        parsers = self.parsers
        for payload in message.payload[self.id.key]:
            try:
                subid = payload[KEY_SUBID]
                try:
                    channel = payload[KEY_CHANNEL]
                except KeyError:
                    channel = payload[mc.KEY_CHANNELS][0]
                    # WARNING receiving multiple channels in payload is not managed
                try:
                    parsers[(subid, channel)](payload)  # type: ignore
                except KeyError as ke:
                    self._handle_missing_subdevice(ke, payload, subid)
                except Exception as e:
                    self.log_parser_exception(e, payload)
            except KeyError:
                # message not related to a subdevice. Parse with plain 'channel' mechanics
                try:
                    parsers[payload[KEY_CHANNEL]](payload)
                except KeyError as ke:
                    self._handle_missing_channel(ke, payload)
                except Exception as e:
                    self.log_parser_exception(e, payload)

    def _handle_channel_list(self, message: MerossMessage, /):
        """
        This handler si optimized for list payloads:
        "payload": { "{self.id.key}": [{"channel":...., ...}] }
        Under normal conditions the loop is optimized with direct parser lookup
        and invocation without caching any intermediate variable since this is the 99%
        expected pattern. The most-likely exceptions are when no parser is registered
        for the channel (KeyError) or when the payload is not a list (TypeError).
        These will be managed so that they'll don't recur anymore.
        """
        for payload in message.payload[self.id.key]:
            try:
                self.parsers[payload[KEY_CHANNEL]](payload)
            except KeyError as ke:
                self._handle_missing_channel(ke, payload)
            except Exception as e:
                # this might be expected: the key payload is not a list
                if type(payload) is str:  # enumerating dict keys
                    self.handler = self._handle_channel_dict
                    self._handle_channel_dict(message)
                    return
                else:
                    self.log_parser_exception(e, payload)

    def _handle_channel_dict(self, message: MerossMessage, /):
        """
        This handler si optimized for dict payloads:
        "payload": { "key_namespace": {"channel":...., ...} }
        """
        payload = message.payload[self.id.key]
        try:
            self.parsers[payload[KEY_CHANNEL]](payload)
        except KeyError as ke:
            self._handle_missing_channel(ke, payload)
        except Exception as e:
            # this might be expected: the payload is not a dict
            # final fallback to the safe _handle_generic
            if type(payload) is not dict:
                self.handler = self._handle_channel
                self._handle_channel(message)
            else:
                self.log_parser_exception(e, payload)

    def _handle_channel(self, message: MerossMessage, /):
        """
        This handler can manage both lists or dicts of 'channel' payloads.
        """
        for payload in extract_dict_payloads(message.payload[self.id.key]):
            try:
                self.parsers[payload[KEY_CHANNEL]](payload)
            except KeyError as ke:
                self._handle_missing_channel(ke, payload)
            except Exception as e:
                self.log_parser_exception(e, payload)

    def _handle(self, msg: MerossMessage, /):
        """Default handler for a namespace message. This implementation works as a stub and is being invoked if no
        better handler has been installed. Handler functions can be installed per instance at construction or
        by overriding this method definition in custom NamespaceHandlers."""
        self.log(
            self.DEBUG, "Handler undefined (message:%s)", _message=msg, timeout=14400
        )

    def _parse(self, payload, /):
        """Default ParserFunc automatically installed when parsing a message for which no indexed parser is registered.
        The payload is typically an 'indexed' item payload scanned by handlers like _handle_channel_list or _handle_subid.
        This is a fallback for unexpected channels/subdevices and is useful for logging purposes.
        """
        self.log(
            self.DEBUG,
            "Parser undefined (payload: %s)",
            _payload=payload,
            timeout=14400,
        )

    def _handle_missing_channel(self, ke: KeyError, payload: "mt.JsonMapping", /):
        """
        Smart handler for KeyError raised when dispatching
        a channel payload to a parser.
        # KeyError here might have been raised because:
        # - key_idx not in payload -> critical
        # - no parser registered for this channel -> create parser if possible
        # - KeyError in parser function
        """
        try:
            index = mn.IndexType.channel(payload[KEY_CHANNEL])
        except KeyError as ke:
            self.log_handler_exception(ke, payload)
            return

        if index in self.parsers:
            self.log_parser_exception(ke, payload)
            return

        self.parent._handle_missing_parser(self, index, payload)

    def _handle_missing_subdevice(
        self, ke: KeyError, payload: "mt.JsonMapping", subdevice_id: str, /
    ):
        """Handler for KeyError raised when dispatching a payload to an hub subdevice parser."""
        index = self.index.index(payload)
        if index in self.parsers:
            # index for the received payload is present so this is likely an error
            # in the parser method.
            if ke.args[0] == subdevice_id:
                # This could only come when this method is being called by _handle_subdevice_id
                # since that handler is checking for duplicates by eating up indexes from parsers copy.
                self.parent.subdevices[subdevice_id].log_duplicated()
            else:
                self.log_parser_exception(ke, payload)
            return
        try:
            self.parent.subdevices[subdevice_id]._handle_missing_parser(
                self, index, payload
            )
        except KeyError as ke:
            if ke.args[0] == subdevice_id:
                # this is a new subdevice for which we dont have a parser yet and we
                # didnt know it existed so we need to do a digest rescan to discover it
                # WARNING/TODO: this might cause a storm of rescans if the device is sending
                # a lot of messages for the same unknown subdevice before we discover it.
                # We should implement a temporary blocklist of unknown subdevices to avoid this.
                # or maybe setup a stub parser for this subdevice that will log and ignore
                # messages until we discover it.
                self.parent.handler_all.next_poll_epoch = 0.0
                return
            raise

    async def async_get(self, *indexes: mn.IndexValue):
        """
        Helper to execute a straigth query to get the whole namespace payload
        or a single item/channel and dispatch the response to the internal
        handler bypassing the Device message routing.
        if channel is None the whole namespace is requested.
        """
        response = await self.parent.async_request(
            *(
                self.id.payload_get.build_get(self.id, *indexes)
                if indexes
                else self.polling_request
            )
        )
        self.handle_response(response)
        return response

    async def async_get_safe(self, *indexes: mn.IndexValue):
        """
        Helper to execute a straigth query to get the whole namespace payload
        or a single item/channel and dispatch the response to the internal
        handler bypassing the Device message routing.
        if channel is None the whole namespace is requested.
        """
        try:
            return await self.async_get(*indexes)
        except Exception as e:
            self.log_exception(self.WARNING, e, "async_get_safe")

    def schedule_get(
        self,
        *indexes: mn.IndexValue,
        task_name: str = "",
    ):
        """
        Helper to schedule a straigth query to get the whole namespace payload.
        This shouldnt be used for namespaces that don't support GET.
        """
        self.create_task(
            self.async_get(*indexes), task_name or self.id, eager_start=True
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
        The parser will be called back on its _parse_xxx method (or __call__ as fallback) and
        the SET command payload will be automatically set to the parser's channel.
        """
        response = await self.parent.async_request(
            *self.id.request_set(parser.index | payload)
        )
        getattr(parser, f"_parse_{self.id.slug_end}", parser)(
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
        response = await self.parent.async_request(
            *ns.request_set(parser.index | payload)
        )
        try:
            payload = response.payload[ns.key][0]
        except (KeyError, IndexError):
            # optimistic update
            if state:
                payload = merge_dicts(dict(state), payload)
        getattr(parser, f"_parse_{ns.slug_end}", parser)(payload)
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
            self.polling_request_payload = [*self.parsers]
            self.polling_request = (
                ns,
                mc.METHOD_GET,
                {ns.key: self.polling_request_payload},
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

    def polling_request_add_index(self, index: mn.IndexValue, /):
        """Ensures the channel is set in polling request payload should the ns need it.
        Also adjusts the estimated polling_response_size.
        Returns the channel payload dict to be used for further updates if needed.
        Some ns grammar might not have a so called 'channel_payload'. For those ns
        the return value has no meaning and is an immutable empty dict.
        """
        try:
            polling_request_payload = self.polling_request_payload
            for payload in polling_request_payload:
                if index.matches(payload):
                    break
            else:
                # this is just a shurtcut since 'subId' namespaces do not
                # still expose a channel different than 0. When that changes
                # it'll be a mess.
                polling_request_payload.append(index)
            self.polling_response_size = (
                NamespaceHandler.HEADER_AVG_SIZE
                + len(polling_request_payload) * self.id.payload_item_size
            )
        except AttributeError:
            # polling_request_payload not used for this ns
            self.polling_response_size = (
                NamespaceHandler.HEADER_AVG_SIZE
                + len(self.parsers) * self.id.payload_item_size
            )

    def polling_response_size_adj(self, item_count: int, /):
        self.polling_response_size = (
            NamespaceHandler.HEADER_AVG_SIZE + item_count * self.id.payload_item_size
        )

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
        indexes = iter(self.parsers.keys())
        polling_request_payload = self.polling_request_payload
        polling_request_payload.clear()
        self.polling_response_size = NamespaceHandler.HEADER_AVG_SIZE
        while True:
            if size_available > payload_item_size:
                try:
                    polling_request_payload.append(next(indexes))
                    size_available -= payload_item_size
                    self.polling_response_size += payload_item_size
                    continue
                except StopIteration:
                    if polling_request_payload:
                        await device.async_poll_request(self)
                    # no need to flush multiple since the polling loop
                    # will continue with standard handling
                    break

            if polling_request_payload:
                await device.async_poll_request(self)
            if device._multiple_requests:
                # ensure we (eventually) flush multiple requests
                await device.async_poll_flush()

            # reset for next chunk
            polling_request_payload.clear()
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
                            if self.polling_request_payload:
                                await async_request_func(*self.polling_request)
                            else:
                                # when a 'LIST_C_STRICT' namespace has no registered parsers, self.polling_request will fail
                                # so we use the mocked default request
                                await async_request_func(*ns.request_default)
                        except AttributeError as ae:
                            if ae.name == "polling_request_payload":
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

                # FIXME
                channels_payload = [*self.parsers]
                if not channels_payload:
                    channels = self.parent.descriptor.channels or (0,)
                    match self.index:
                        case mn.IndexType.channel:
                            channels_payload = [
                                {KEY_CHANNEL: channel} for channel in channels
                            ]
                        case mn.IndexType.id:
                            channels_payload = [
                                {KEY_ID: subdevice_id}
                                for subdevice_id in self.parent.subdevices
                            ]
                        case mn.IndexType.subId:
                            channels_payload = [
                                {KEY_SUBID: subdevice_id, KEY_CHANNEL: 0}
                                for subdevice_id in self.parent.subdevices
                            ]
                            channels_payload.append({KEY_CHANNEL: 0})
                        case _:
                            channels_payload = [
                                {KEY_CHANNEL: channel} for channel in channels
                            ]

                channels_count = len(channels_payload)

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
                # looking for DICT_IDX_STRICT request type
                for payload in channels_payload:
                    await _async_wrapped_get({ns_key: payload})

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
                    # the namespace might need an index in the request
                    # 'channel' index might be used in any kind of device (also hubs)
                    await _async_wrapped_get({ns_key: [{KEY_CHANNEL: 0}]})
                    subdevices = self.parent.descriptor.subdevices
                    if subdevices:
                        # typical 'legacy' devices are queried by "id"
                        if response := await _async_wrapped_get(
                            {
                                ns_key: [
                                    {KEY_ID: subdevice[KEY_ID]}
                                    for subdevice in subdevices
                                ]
                            },
                        ):
                            response_payload = response.payload.get(ns_key)
                            if response_payload:
                                return
                        # many other new ones (ms130 for example) need a "subId/channel" pair
                        await _async_wrapped_get(
                            {
                                ns_key: [
                                    {KEY_SUBID: subdevice[KEY_ID], KEY_CHANNEL: 0}
                                    for subdevice in subdevices
                                ]
                            },
                        )

    HEADER_AVG_SIZE = 300
    POLLING_CONFIG_DEFAULT = (0, 0, async_poll_default)
    POLLING_CONFIG_NONE = (0, 0, None)
    POLLING_CONFIG_ONCE = (0, 0, async_poll_once)
    POLLING_CONFIG_MAP = {}


class VoidNamespaceHandler(NamespaceHandler):
    """Utility class to manage namespaces which should be 'ignored' i.e. we're aware
    of their existence but we don't process them at the device level. This class in turn
    just provides an empty handler and so suppresses any log too (for unknown namespaces)
    done by the base default handling."""

    @override
    def _handle(self, message: "MerossMessage", /):
        pass
