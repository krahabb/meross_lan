import bisect
from functools import cached_property
from time import time
from typing import TYPE_CHECKING

from .. import const as mlc
from ..merossclient import merge_dicts
from ..merossclient.protocol import MerossProtocolError, const as mc, namespaces as mn

if TYPE_CHECKING:
    from typing import Any, Callable, Coroutine, Final, Iterable

    from . import Loggable
    from ..merossclient.protocol import types as mt
    from ..merossclient.protocol.message import MerossMessage, MerossResponse
    from ..merossclient.protocol.types import JsonDict, JsonMapping
    from .device import AsyncRequestFunc, Device
    from .entity import MLEntity

    type NamespaceHandlerFunc = Callable[[MerossMessage], None]
    type PollingStrategyFunc = Callable[["NamespaceHandler"], Coroutine]
    type NamespaceConfigType = tuple[int, int, int, int, PollingStrategyFunc | None]


class NamespaceParser(Loggable if TYPE_CHECKING else object):
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
        ns: mn.Namespace
        channel: int | str  # the channel/id/subId key value according to the namespace

        _payload_ns: JsonDict  # the last parsed payload
        _namespace_handlers: set[
            "NamespaceHandler"
        ]  # multiple ns could forward to this parser

    # using class-level defaults here until we build a proper hierarchy
    # with specialized __init__
    _payload_ns = mn.EMPTY_DICT  # class-level default
    _namespace_handlers = None  # type: ignore

    async def async_shutdown(self):
        try:
            for handler in self._namespace_handlers:
                del handler.parsers[self.channel]
            del self._namespace_handlers
        except TypeError:  # never registered
            assert self._namespace_handlers is None

    @cached_property
    def handler_ns(self) -> "NamespaceHandler":
        # TODO: define a more consistent interface
        # This is right now a brutal hack to automagically provide ns_handler property
        # to entities which might not need to be registered parsers but still need to access
        # the NamespaceHandler to issue device requests. Most of the times these are entities
        # where ns parsing is delegated to a container object/handler which is then dispatching
        # updates without using the NamespaceHandler inner mechanisms.
        return self.manager.ns_handlers[self.ns]  # type: ignore

    async def async_request_payload(self, payload: "JsonDict", /):
        return await self.handler_ns.device.async_request(
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

    def _parse(self, payload: dict, /):
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
            str(payload),
            timeout=14400,
        )

    def _handle(self, message: "MerossMessage", /):
        """
        Raw handler to be used as a direct callback for NamespaceHandler.
        Contrary to _parse which is invoked after splitting (x channel) the payload,
        this is intendend to be used as a direct handler for the full namespace
        message as an optimization in case the namespace is only mapped to a single
        entity/class instance (See DNDMode)
        """
        self.log(
            self.WARNING,
            "Handler undefined for payload:(%s)",
            str(message.payload),
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
        parsers: dict[object, Callable[[dict], None]]
        lastpush: JsonDict | None  # TODO: implement caching of all methods responses
        handler: NamespaceHandlerFunc
        polling_strategy: PollingStrategyFunc | None
        polling_request: mt.MerossRequestType
        polling_request_channels: list[dict[str, Any]]  # on demand instance

    __slots__ = (
        "device",
        "ns",
        "handler",
        "parsers",
        "entity_class",
        "lastrequest",
        "lastresponse",
        "lastpush",
        "polling_epoch_next",
        "polling_strategy",
        "polling_period",
        "polling_period_cloud",
        "polling_response_base_size",
        "polling_response_item_size",
        "polling_response_size",
        "polling_request",
        "polling_request_channels",
        "__weakref__",  # REMOVE
    )

    def __init__(
        self,
        device: "Device",
        ns: "mn.Namespace",
        /,
        *,
        handler: "NamespaceHandlerFunc | None" = None,
        config: "NamespaceConfigType | None" = None,
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
        self.entity_class = None
        self.lastresponse = self.lastrequest = self.polling_epoch_next = 0.0
        self.lastpush = None

        if _conf := config or POLLING_STRATEGY_CONF.get(ns):
            self.polling_period = _conf[0]
            self.polling_period_cloud = _conf[1]
            self.polling_response_base_size = _conf[2]
            self.polling_response_item_size = _conf[3]
            self.polling_strategy = _conf[4]
        else:
            # these in turn are defaults for dynamically parsed
            # namespaces managed when using create_diagnostic_entities
            self.polling_period = mlc.PARAM_DIAGNOSTIC_UPDATE_PERIOD
            self.polling_period_cloud = mlc.PARAM_CLOUDMQTT_UPDATE_PERIOD
            self.polling_response_base_size = mlc.PARAM_HEADER_SIZE
            self.polling_response_item_size = 0
            self.polling_strategy = None

        # by default we calculate 1 item/channel per payload but we should
        # refine this whenever needed
        self.polling_response_size = (
            self.polling_response_base_size + self.polling_response_item_size
        )
        self.polling_request_configure(
            mn.PayloadType.LIST_C_STRICT
            if self.polling_strategy is NamespaceHandler.async_poll_chunked
            else None
        )
        device.ns_handlers[ns] = self

    def shutdown(self):
        """Cleanup possible circular references."""
        self.device.objects.add(self)  # REMOVE
        del self.handler  # especially this one
        del self.polling_strategy
        del self.device
        assert not self.parsers, "parsers should have been cleared before shutdown"

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
        if (_payload_type is mn.PayloadType.LIST_C_STRICT) or (
            _payload_type is mn.PayloadType.LIST_C_DATA_STRICT
        ):
            self.polling_request_channels = []
            self.polling_request = (
                ns,
                mc.METHOD_GET,
                {ns.key: self.polling_request_channels},
            )
            return
        if (_payload_type is ns.payload_get) and ns.can_query:
            # we'll reuse the default in the ns definition
            self.polling_request = ns.request_default
            return
        match _payload_type:
            case mn.PayloadType.PUSH | mn.PayloadType.PUSH_QUERY:
                self.polling_request = (
                    ns,
                    mc.METHOD_PUSH,
                    mn.EMPTY_DICT,
                )
            case _:
                self.polling_request = _payload_type.build_get(ns)

    def polling_request_add_channel(
        self, channel, extra: "mt.MerossPayloadType" = {}, /
    ):
        # Ensures the channel is set in polling request payload should
        # the ns need it. Also adjusts the estimated polling_response_size.
        try:
            polling_request_channels = self.polling_request_channels
            key_channel = self.ns.key_channel
            for channel_payload in polling_request_channels:
                if channel_payload[key_channel] == channel:
                    break
            else:
                # this is just a shurtcut since 'subId' namespaces do not
                # still expose a channel different than 0. When that changes
                # it'll be a mess.
                channel_payload = (
                    {key_channel: channel, mc.KEY_CHANNEL: 0}
                    if key_channel == mc.KEY_SUBID
                    else {key_channel: channel}
                )
                polling_request_channels.append(channel_payload)

            if extra:
                channel_payload.update(extra)

            self.polling_response_size = (
                self.polling_response_base_size
                + len(polling_request_channels) * self.polling_response_item_size
            )
        except AttributeError:
            # polling_request_channels not used for this ns
            self.polling_response_size = (
                self.polling_response_base_size
                + len(self.parsers) * self.polling_response_item_size
            )

    def polling_response_size_adj(self, item_count: int, /):
        self.polling_response_size = (
            self.polling_response_base_size
            + item_count * self.polling_response_item_size
        )

    def polling_response_size_inc(self):
        self.polling_response_size += self.polling_response_item_size

    def channels_to_poll(self):
        # snapshot sequence of channels to query (likely needed with all these asyncs)
        return tuple(self.parsers.keys())

    def register_entity_class(
        self, entity_class: type["MLEntity"], channels: "Iterable[int] | None", /
    ):
        self.entity_class = entity_class
        self.handler = self._handle_list
        self.device.platforms.setdefault(entity_class.PLATFORM)
        if channels is None:
            channels = set()

            def _scan_digest(digest: dict):
                try:
                    channels.add(digest[mc.KEY_CHANNEL])
                except KeyError:
                    for value in digest.values():
                        if type(value) is dict:
                            _scan_digest(value)
                        elif type(value) is list:
                            for value_item in value:
                                if type(value_item) is dict:
                                    _scan_digest(value_item)

            _scan_digest(self.device.descriptor.digest)

        for channel in channels:
            entity_class(self.device, channel)

    def register_parser(self, parser: "NamespaceParser", /):
        # when setting up the entity-dispatching we'll substitute the legacy handler
        # (used to be a Device method with syntax like _handle_Appliance_xxx_xxx)
        # with our _handle_list, _handle_dict, _handle_generic. The 3 versions are meant
        # to be optimized against a well known type of payload. We're starting by guessing our
        # payload is a list but we'll dynamically adjust this whenever we find (in real world)
        # a different payload structure so we can adapt.
        # As an example of why this is needed, many modern payloads are just lists (
        # Thermostat payloads for instance) but many older ones are not, and still
        # either carry dict or, worse, could present themselves in both forms
        # (ToggleX is a well-known example)
        ns = self.ns
        channel = parser.channel
        assert channel not in self.parsers, "parser already registered"
        self.parsers[channel] = getattr(parser, f"_parse_{ns.slug_end}", parser._parse)
        if not parser._namespace_handlers:
            parser._namespace_handlers = set()
        parser._namespace_handlers.add(self)
        self.polling_request_add_channel(channel)
        self.handler = self._handle_list

    def handle_response(self, response: "MerossMessage", /):
        """Entry point for handling a received message for this namespace.
        This is invoked by Device._handle after routing the message to
        the proper NamespaceHandler based off the namespace in the header.
        """
        # TODO: save all of the last sent/received payloads for a ns_handler
        # for diagnostics (GET/ACK/SET/PUSH/DEL)
        self.lastresponse = self.device.lastresponse
        self.polling_epoch_next = self.lastresponse + self.polling_period
        try:
            self.handler(response)
        except Exception as exception:
            self.handle_exception(exception, self.handler.__name__, response.payload)

    def handle_exception(self, exception: Exception, function_name: str, payload, /):
        # TODO: migrate to Loggable so we have more flexibility in logging
        device = self.device
        device.log_exception(
            device.WARNING,
            exception,
            "%s(%s).%s: payload=%s",
            self.__class__.__name__,
            self.ns,
            function_name,
            str(device.loggable_any(payload)),
            timeout=604800,
        )

    def _handle_list(self, message: "MerossMessage", /):
        """
        splits and forwards the received NS payload to
        the registered entity(es).
        This handler si optimized for list payloads:
        "payload": { "key_namespace": [{"channel":...., ...}] }
        """
        try:
            for p_channel in message.payload[self.ns.key]:
                try:
                    _parse = self.parsers[p_channel[self.ns.key_channel]]
                except KeyError as key_error:
                    _parse = self._try_create_entity(key_error)
                _parse(p_channel)
        except TypeError:
            # this might be expected: the payload is not a list
            self.handler = self._handle_dict
            self._handle_dict(message)

    def _handle_dict(self, message: "MerossMessage", /):
        """
        splits and forwards the received NS payload to
        the registered entity(es).
        This handler si optimized for dict payloads:
        "payload": { "key_namespace": {"channel":...., ...} }
        """
        p_channel = message.payload[self.ns.key]
        try:
            _parse = self.parsers[p_channel.get(self.ns.key_channel)]
        except KeyError as key_error:
            _parse = self._try_create_entity(key_error)
        except AttributeError:
            # this might be expected: the payload is not a dict
            # final fallback to the safe _handle_generic
            self.handler = self._handle_generic
            self._handle_generic(message)
            return
        _parse(p_channel)

    def _handle_generic(self, message: "MerossMessage", /):
        """
        splits and forwards the received NS payload to
        the registered entity(es)
        This handler can manage both lists or dicts or even
        payloads without the "channel" key (see namespace Toggle)
        which will default forwarding to channel == None
        """
        p_channel = message.payload[self.ns.key]
        if type(p_channel) is dict:
            try:
                _parse = self.parsers[p_channel.get(self.ns.key_channel)]
            except KeyError as key_error:
                _parse = self._try_create_entity(key_error)
            _parse(p_channel)
        else:
            key_channel = self.ns.key_channel
            for p_channel in p_channel:
                try:
                    _parse = self.parsers[p_channel[key_channel]]
                except KeyError as key_error:
                    _parse = self._try_create_entity(key_error)
                _parse(p_channel)

    def _handle_undefined(self, message: "MerossMessage", /):
        device = self.device
        device.log(
            device.DEBUG,
            "Handler undefined for method:%s namespace:%s payload:%s",
            message.method,
            message.namespace,
            str(device.loggable_dict(message.payload)),
            timeout=14400,
        )
        if device.create_diagnostic_entities:
            # since we're parsing an unknown namespace, our euristic about
            # the key_namespace might be wrong so we use another euristic
            ns = self.ns
            for _key, _payload in message.payload.items():
                # since the ns_key might be often the same across different namespaces
                # we add the last split of the namespace to the extracted payload key
                if type(_payload) is dict:
                    self._parse_undefined_dict(
                        f"{ns.slug_end}_{_key}", _payload, _payload.get(ns.key_channel)
                    )
                else:
                    _key = f"{ns.slug_end}_{_key}"
                    for __payload in _payload:
                        # not having a "channel" in the list payloads is unexpected so far
                        self._parse_undefined_dict(
                            _key, __payload, __payload.get(ns.key_channel)
                        )

    def parse_list(self, digest: list, /):
        """twin method for _handle (same job - different context).
        Used when parsing digest(s) in NS_ALL"""
        try:
            key_channel = self.ns.key_channel
            for p_channel in digest:
                try:
                    _parse = self.parsers[p_channel[key_channel]]
                except KeyError as key_error:
                    _parse = self._try_create_entity(key_error)
                _parse(p_channel)
        except Exception as exception:
            self.handle_exception(exception, "_parse_list", digest)

    def parse_generic(self, digest: list | dict, /):
        """twin method for _handle (same job - different context).
        Used when parsing digest(s) in NS_ALL"""
        try:
            if type(digest) is dict:
                self.parsers[digest.get(self.ns.key_channel)](digest)
            else:
                key_channel = self.ns.key_channel
                for p_channel in digest:
                    try:
                        _parse = self.parsers[p_channel[key_channel]]
                    except KeyError as key_error:
                        _parse = self._try_create_entity(key_error)
                    _parse(p_channel)
        except Exception as exception:
            self.handle_exception(exception, "_parse_generic", digest)

    def _parse_undefined_dict(self, key: str, payload: dict, channel: object | None, /):
        device_entities = self.device.entities
        for subkey, subvalue in payload.items():
            if isinstance(subvalue, dict):
                self._parse_undefined_dict(f"{key}_{subkey}", subvalue, channel)
                continue
            if isinstance(subvalue, list):
                self._parse_undefined_list(f"{key}_{subkey}", subvalue, channel)
                continue
            if subkey in {
                mc.KEY_ID,
                mc.KEY_CHANNEL,
                mc.KEY_LMTIME,
                mc.KEY_LMTIME_,
                mc.KEY_SYNCEDTIME,
                mc.KEY_LATESTSAMPLETIME,
            }:
                continue
            try:
                device_entities[
                    (
                        f"{channel}_{key}_{subkey}"
                        if channel is not None
                        else f"{key}_{subkey}"
                    )
                ].update_native_value(subvalue)
            except KeyError:
                from ..sensor import MLDiagnosticSensor

                MLDiagnosticSensor(
                    self.device,
                    channel,
                    f"{key}_{subkey}",
                    native_value=subvalue,
                )
                if not self.polling_strategy:
                    self.polling_strategy = NamespaceHandler.async_poll_diagnostic

    def _parse_undefined_list(self, key: str, payload: list, channel, /):
        pass

    def _parse_stub(self, payload, /):
        device = self.device
        device.log(
            device.DEBUG,
            "Parser stub called on namespace:%s payload:%s",
            self.ns,
            str(device.loggable_dict(payload)),
            timeout=14400,
        )

    def _try_create_entity(self, key_error: KeyError, /):
        """
        Handler for when a payload points to a channel
        actually not registered for parsing.
        If an entity_class was registered then instantiate that else
        proceed with a 'stub' in order to just silence (from now on)
        the exception. This stub might be a dignostic entity if device
        configured so, or just an empty handler.
        """
        channel = key_error.args[0]
        if channel == self.ns.key_channel:
            # ensure key represents a channel and not the "channel" key
            # in the p_channel dict
            raise key_error

        if self.entity_class:
            self.entity_class(
                self.device, channel, entity_registry_enabled_default=True
            )
        elif self.device.create_diagnostic_entities:
            from ..sensor import MLDiagnosticSensor

            self.register_parser(
                MLDiagnosticSensor(
                    self.device,
                    channel,
                    self.ns.key,
                )
            )
        else:
            self.parsers[channel] = self._parse_stub

        return self.parsers[channel]

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
            self.handle_exception(e, "async_get", None)

    def schedule_get(
        self,
        *channels,
        task_name: str = "",
    ):
        """
        Helper to schedule a straigth query to get the whole namespace payload.
        This shouldnt be used for namespaces that don't support GET.
        """
        self.device.async_create_task(
            self.async_get_safe(*channels), task_name or self.ns, False
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
        assert ns.payload_set is mn.PayloadType.LIST_C, "Only LIST_C supported here"
        response = None
        try:
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
        except Exception as e:
            self.handle_exception(e, "async_set_c_ex", response)

    # Polling Strategies:
    # These are configured at initialization time by setting the 'polling_strategy' attribute
    # and invoked by the polling cycle.
    async def async_poll_all(self):
        """
        This is a special policy for NS_ALL.
        It is basically an 'async_poll_default' policy so it kicks-in whenever we poll
        the state in 'device._async_request_updates' but contrary to 'legacy' behavior
        where NS_ALL was always polled (unless mqtt active).
        This will alternate polling NS_ALL to the group of namespaces responsible for
        the state carried in 'digest'. This is an improvement since NS_ALL, even if carrying
        the whole state in one query, might be huge (because of the 'time' key) but also because
        most of its data are pretty static (never or seldom changing) info of the device.
        This new policy will interleave querying NS_ALL once in a while with smaller direct
        equivalent queries for the state carried in digest. (If the device doesn't support
        NS_MULTIPLE, it will likely do more queries though but this is unlikely)
        """
        device = self.device
        if device._mqtt_active:
            # on MQTT no need for updates since they're being PUSHed
            if not self.polling_epoch_next:
                # just when onlining...
                await device.async_request_poll(self)
            return

        # here we're missing PUSHed updates so we have to poll...
        if device._polling_epoch >= self.polling_epoch_next:
            # at start or periodically ask for NS_ALL..plain
            await device.async_request_poll(self)
            return

        # query specific namespaces instead of NS_ALL since we hope this is
        # better (less overhead/http sessions) together with ns_multiple packing
        for handler in device.digest_pollers:
            if handler.parsers:
                # don't query if digest key/namespace hasn't any entity registered
                # this also prevents querying a somewhat 'malformed' ToggleX reply
                # appearing in an mrs100 (#447)
                await handler.async_poll_digest()

    async def async_poll_digest(self):
        """This is the policy to be used when async_poll_all turns to requesting single
        namespaces as appearing in the digest key of ns_all. See async_poll_all."""
        await self.device.async_request_poll(self)

    async def async_poll_default(self):
        """
        This is a basic 'default' policy:
        - avoid the request when MQTT available (this is for general 'state' namespaces like NS_ALL) and
        we expect this namespace to be updated by PUSH(es)
        - unless the 'polling_epoch_next' is 0 which means we're re-onlining the device and so
        we like to re-query the full state (even on MQTT)
        """
        device = self.device
        if not (device._mqtt_active and self.polling_epoch_next):
            await device.async_request_poll(self)

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
        supports PUSH or we have received at least one PUSH for it (lastpush).
        """
        device = self.device
        """ TODO: re-enable this optimization after testing. It looks like our 'knowledge' of
        PUSHed namespaces is not perfect yet and we're skipping needed polls (#607 #609).
        if (
            device._mqtt_active
            and self.polling_epoch_next
            and (self.ns.payload_psh or self.lastpush)
        ):
            # on MQTT no need for updates since they're being PUSHed
            return
        """
        if device._polling_epoch >= self.polling_epoch_next:
            if await device.async_request_smartpoll(self):
                return

        # Insert into the lazypoll_requests ordering by least recently polled
        def _lazypoll_key(_handler: NamespaceHandler):
            return _handler.lastrequest - device._polling_epoch

        bisect.insort_right(device._lazypoll_requests, self, key=_lazypoll_key)

    async def async_poll_once(self):
        """
        This strategy is for 'constant' namespace data which do not change and only
        need to be requested once (after onlining that is). When polling use
        same queueing policy as async_poll_smart to don't overwhelm the cloud mqtt
        """
        if not self.polling_epoch_next:
            await self.device.async_request_smartpoll(self)

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
        if device._mqtt_active and (device._polling_epoch < self.polling_epoch_next):
            # this check is the same as async_poll_default where we expect this ns to be
            # PUSHed when on MQTT
            return

        size_available = (
            device.polling_response_size_available - self.polling_response_base_size
        )
        if size_available < self.polling_response_item_size:
            if device._multiple_requests:
                await device._async_poll_multiple_flush()
                size_available = (
                    device.polling_response_size_available
                    - self.polling_response_base_size
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

        if device.cloudpoll_requests:
            # We've already queued cloud polling requests for this cycle
            # TODO: re-implement this feature by leveraging MQTT rate-limiting state
            # i.e. if we're not rate-limited we could allow polling else defer.
            return  # defer to next (hopefully)

        # Previous implementation was just splitting-up the requests in fixed amounts
        # determined at design time.
        # New implementation tries to leverage the knowledge of allowed response buffers
        # in the device to fill up the most subdevices requests per message.
        channels = iter(self.channels_to_poll())
        channels_payload = self.polling_request_channels
        channels_payload.clear()
        self.polling_response_size = self.polling_response_base_size
        while True:
            if size_available > self.polling_response_item_size:
                try:
                    channels_payload.append({self.ns.key_channel: next(channels)})
                    size_available -= self.polling_response_item_size
                    self.polling_response_size += self.polling_response_item_size
                    continue
                except StopIteration:
                    if channels_payload:
                        await device.async_request_poll(self)
                    # no need to flush multiple since the polling loop
                    # will continue with standard handling
                    break

            if channels_payload:
                await device.async_request_poll(self)
            if device._multiple_requests:
                # ensure we (eventually) flush multiple requests
                await device._async_poll_multiple_flush()

            # reset for next chunk
            channels_payload.clear()
            self.polling_response_size = self.polling_response_base_size
            size_available = (
                device.polling_response_size_available - self.polling_response_base_size
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
        if (
            device._mqtt_active
            and self.polling_epoch_next
            and (self.ns.has_psh or self.lastpush)
        ):
            # on MQTT no need for updates since they're being PUSHed
            return

        if device._polling_epoch >= self.polling_epoch_next:
            await device.async_request_smartpoll(self)

    async def async_trace(self, async_request_func: "AsyncRequestFunc", /):
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
                        mn.PayloadType.LIST_C
                        | mn.PayloadType.LIST_C_STRICT
                        | mn.PayloadType.LIST_C_DATA_STRICT
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

                if ns.payload_get is not mn.PayloadType.EMPTY:
                    # Beside what is being stated by our grammar, it might be we've
                    # always probed this ns with the wrong GET payload. According
                    # to knowledge from Meross App analisys many if not all should
                    # instead work with a plain empty GET payload.
                    await _async_wrapped_get({})

            except Exception:
                # TODO: log exception?
                pass

            return

        ns_key = ns.key
        ns_key_channel = ns.key_channel
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

                if self.device.DEVICE_TYPE is mlc.DeviceType.DEVICE:
                    channels = self.parsers.keys() or (0,)
                else:  # it is an hub
                    channels = self.device.subdevices

                channels_count = len(channels)
                channels_payload = [{ns_key_channel: channel} for channel in channels]
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
                    detected_request_payload_type = mn.PayloadType.LIST_C_STRICT

                # check ordered from more to less 'data heavy' payloads
                # so that the last one working (less data) is the fallback
                for _payload_type in (
                    mn.PayloadType.DICT_C_65535,
                    mn.PayloadType.DICT_C_STRICT,
                    mn.PayloadType.LIST_C,
                    mn.PayloadType.DICT_C,
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
                    # If this querying format works but none of the other does then we'll
                    # need to implement async_poll_digest in order to send the whole set of requests
                    # needed to poll the digest
                    await _async_wrapped_get({ns_key: channel_payload})
                # Also check if hub namespaces indexed by "subId" maybe also need a "channel"
                if ns_key_channel == mc.KEY_SUBID:
                    await _async_wrapped_get(
                        {
                            ns_key: [
                                {ns_key_channel: channel, mc.KEY_CHANNEL: 0}
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
                    if self.device.DEVICE_TYPE is mlc.DeviceType.DEVICE:
                        await _async_wrapped_get({ns_key: [{mc.KEY_CHANNEL: 0}]})
                    else:  # it is an hub
                        subdevices = self.device.subdevices
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


class EntityNamespaceMixin(MLEntity if TYPE_CHECKING else object):
    """
    Special 'polling enabler/disabler' mixin used with entities which are
    'single instance' for a namespace handler and so they'll disable polling
    should they're disabled in HA.
    """

    if TYPE_CHECKING:
        manager: "Device"

    @classmethod
    def namespace_init(cls, device: "Device", ns: mn.Namespace, /):
        assert ns is cls.ns
        entity = cls(device, None)
        entity.handler_ns = NamespaceHandler(device, ns, handler=entity._handle)
        entity.handler_ns.polling_strategy = None
        return entity

    async def async_added_to_hass(self):
        self.handler_ns.polling_strategy = POLLING_STRATEGY_CONF[self.ns][4]
        return await super().async_added_to_hass()

    async def async_will_remove_from_hass(self):
        self.handler_ns.polling_strategy = None
        return await super().async_will_remove_from_hass()

    def _handle(self, message: "MerossMessage", /):
        self._parse(message.payload[self.ns.key])


class VoidNamespaceHandler(NamespaceHandler):
    """Utility class to manage namespaces which should be 'ignored' i.e. we're aware
    of their existence but we don't process them at the device level. This class in turn
    just provides an empty handler and so suppresses any log too (for unknown namespaces)
    done by the base default handling."""

    def __init__(self, device: "Device", ns: mn.Namespace, /):
        NamespaceHandler.__init__(self, device, ns, handler=self._handle_void)

    def _handle_void(self, message: "MerossMessage", /):
        pass


"""
Default timeouts and config parameters for polled namespaces.
The configuration is set in the tuple as:
(
    polling_period,
    polling_period_cloud,
    response_base_size,
    response_item_size,
    strategy
)
see the NamespaceHandler class for the meaning of these values
The 'response_size' is a conservative (in excess) estimate of the
expected response size for the whole message (header itself weights around 300 bytes).
Some payloads would depend on the number of channels/subdevices available
and the configured number would just be a base size (minimum) while
the 'response_item_size' value must be multiplied for the number of channels/subdevices
and will be used to adjust the actual 'response_size' at runtime in the relative strategy.
This parameter in turn will be used to split expected huge payload requests/responses
in Appliance.Control.Multiple since it appears the HTTP interface has an outbound
message size limit around 3000 chars/bytes (on a legacy mss310) and this would lead to a malformed (truncated)
response. This issue also appeared on hubs when querying for a big number of subdevices
as reported in #244 (here the buffer limit was around 4000 chars). From limited testing this 'kind of overflow' is not happening on MQTT
responses though
"""
POLLING_STRATEGY_CONF: dict[mn.Namespace, "NamespaceConfigType"] = {
    mn.Appliance_System_All: (
        mlc.PARAM_HEARTBEAT_PERIOD,
        0,
        1000,
        0,
        NamespaceHandler.async_poll_all,
    ),
    mn.Appliance_System_Debug: (0, 0, 1900, 0, None),
    mn.Appliance_System_DNDMode: (
        mlc.PARAM_CONFIG_UPDATE_PERIOD,
        mlc.PARAM_CLOUDMQTT_UPDATE_PERIOD,
        320,
        0,
        NamespaceHandler.async_poll_smart,
    ),
    mn.Appliance_System_Runtime: (
        mlc.PARAM_SENSOR_SLOW_UPDATE_PERIOD,
        mlc.PARAM_CLOUDMQTT_UPDATE_PERIOD,
        330,
        0,
        NamespaceHandler.async_poll_smart,
    ),
    mn.Appliance_Config_Sensor_Association: (
        mlc.PARAM_CONFIG_UPDATE_PERIOD,
        mlc.PARAM_CLOUDMQTT_UPDATE_PERIOD,
        mlc.PARAM_HEADER_SIZE,
        30,
        NamespaceHandler.async_poll_smart,
    ),
    mn.Appliance_Config_OverTemp: (
        mlc.PARAM_CONFIG_UPDATE_PERIOD,
        mlc.PARAM_CLOUDMQTT_UPDATE_PERIOD,
        340,
        0,
        NamespaceHandler.async_poll_smart,
    ),
    mn.Appliance_Control_ConsumptionH: (
        mlc.PARAM_ENERGY_UPDATE_PERIOD,
        mlc.PARAM_ENERGY_UPDATE_CLOUD_PERIOD,
        320,
        900,
        NamespaceHandler.async_poll_smart,
    ),
    mn.Appliance_Control_ConsumptionX: (
        mlc.PARAM_ENERGY_UPDATE_PERIOD,
        mlc.PARAM_ENERGY_UPDATE_CLOUD_PERIOD,
        1800,  # assume full 30 days of data
        0,  # single day roughly 53 bytes
        NamespaceHandler.async_poll_smart,
    ),
    mn.Appliance_Control_Diffuser_Sensor: (
        mlc.PARAM_SENSOR_SLOW_UPDATE_PERIOD,
        mlc.PARAM_SENSOR_SLOW_UPDATE_CLOUD_PERIOD,
        mlc.PARAM_HEADER_SIZE,
        100,
        NamespaceHandler.async_poll_smart,
    ),
    mn.Appliance_Control_Electricity: (
        mlc.PARAM_SENSOR_FAST_UPDATE_PERIOD,
        mlc.PARAM_SENSOR_FAST_UPDATE_CLOUD_PERIOD,
        430,
        0,
        NamespaceHandler.async_poll_smart,
    ),
    mn.Appliance_Control_ElectricityX: (
        mlc.PARAM_SENSOR_FAST_UPDATE_PERIOD,
        mlc.PARAM_SENSOR_FAST_UPDATE_CLOUD_PERIOD,
        mlc.PARAM_HEADER_SIZE,
        100,
        NamespaceHandler.async_poll_smart,
    ),
    mn.Appliance_Control_Fan: (
        0,
        mlc.PARAM_CLOUDMQTT_UPDATE_PERIOD,
        mlc.PARAM_HEADER_SIZE,
        20,
        None,
    ),
    mn.Appliance_Control_FilterMaintenance: (
        mlc.PARAM_CLOUDMQTT_UPDATE_PERIOD,
        mlc.PARAM_CLOUDMQTT_UPDATE_PERIOD,
        mlc.PARAM_HEADER_SIZE,
        35,
        NamespaceHandler.async_poll_smart,
    ),
    mn.Appliance_Control_Light_Effect: (
        mlc.PARAM_CONFIG_UPDATE_PERIOD,
        mlc.PARAM_CLOUDMQTT_UPDATE_PERIOD,
        1850,
        0,
        NamespaceHandler.async_poll_smart,
    ),
    mn.Appliance_Control_Mp3: (
        0,
        0,
        380,
        0,
        NamespaceHandler.async_poll_default,
    ),
    mn.Appliance_Control_PhysicalLock: (
        mlc.PARAM_CONFIG_UPDATE_PERIOD,
        mlc.PARAM_CLOUDMQTT_UPDATE_PERIOD,
        mlc.PARAM_HEADER_SIZE,
        35,
        NamespaceHandler.async_poll_smart,
    ),
    mn.Appliance_Control_Presence_Config: (
        mlc.PARAM_CONFIG_UPDATE_PERIOD,
        mlc.PARAM_CLOUDMQTT_UPDATE_PERIOD,
        mlc.PARAM_HEADER_SIZE,
        260,
        NamespaceHandler.async_poll_smart,
    ),
    mn.Appliance_Control_Screen_Brightness: (
        mlc.PARAM_CONFIG_UPDATE_PERIOD,
        mlc.PARAM_CLOUDMQTT_UPDATE_PERIOD,
        mlc.PARAM_HEADER_SIZE,
        70,
        NamespaceHandler.async_poll_smart,
    ),
    mn.Appliance_Control_Sensor_Latest: (
        mlc.PARAM_SENSOR_SLOW_UPDATE_PERIOD,
        mlc.PARAM_SENSOR_SLOW_UPDATE_CLOUD_PERIOD,
        mlc.PARAM_HEADER_SIZE,
        80,
        NamespaceHandler.async_poll_smart,
    ),
    mn.Appliance_Control_Sensor_LatestX: (
        mlc.PARAM_SENSOR_SLOW_UPDATE_PERIOD,
        mlc.PARAM_CLOUDMQTT_UPDATE_PERIOD,
        mlc.PARAM_HEADER_SIZE,
        220,
        NamespaceHandler.async_poll_smart,
    ),
    mn.Appliance_Control_Toggle: (
        0,
        0,
        mlc.PARAM_HEADER_SIZE,
        40,
        NamespaceHandler.async_poll_default,
    ),
    mn.Appliance_GarageDoor_Config: (
        mlc.PARAM_CONFIG_UPDATE_PERIOD,
        mlc.PARAM_CLOUDMQTT_UPDATE_PERIOD,
        410,
        0,
        NamespaceHandler.async_poll_smart,
    ),
    mn.Appliance_GarageDoor_MultipleConfig: (
        mlc.PARAM_CONFIG_UPDATE_PERIOD,
        mlc.PARAM_CLOUDMQTT_UPDATE_PERIOD,
        mlc.PARAM_HEADER_SIZE,
        140,
        NamespaceHandler.async_poll_smart,
    ),
    mn.Appliance_Mcu_Firmware: (
        0,
        0,
        mlc.PARAM_HEADER_SIZE,
        80,
        NamespaceHandler.async_poll_once,
    ),
    mn.Appliance_Mcu_Hp110_Firmware: (
        0,
        0,
        mlc.PARAM_HEADER_SIZE,
        80,
        NamespaceHandler.async_poll_once,
    ),
    mn.Appliance_RollerShutter_Adjust: (
        mlc.PARAM_CONFIG_UPDATE_PERIOD,
        mlc.PARAM_CLOUDMQTT_UPDATE_PERIOD,
        mlc.PARAM_HEADER_SIZE,
        35,
        NamespaceHandler.async_poll_smart,
    ),
    mn.Appliance_RollerShutter_Config: (
        mlc.PARAM_CONFIG_UPDATE_PERIOD,
        mlc.PARAM_CLOUDMQTT_UPDATE_PERIOD,
        mlc.PARAM_HEADER_SIZE,
        70,
        NamespaceHandler.async_poll_smart,
    ),
    mn.Appliance_RollerShutter_Position: (
        0,
        0,
        mlc.PARAM_HEADER_SIZE,
        50,
        NamespaceHandler.async_poll_default,
    ),
    mn.Appliance_RollerShutter_State: (
        0,
        0,
        mlc.PARAM_HEADER_SIZE,
        40,
        NamespaceHandler.async_poll_default,
    ),
}
