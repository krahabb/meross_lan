from typing import TYPE_CHECKING

from .. import const as mlc
from ..merossclient.protocol import const as mc, namespaces as mn
from ..merossclient.protocol.message import check_message_strict

if TYPE_CHECKING:
    from typing import Any, Callable, Coroutine

    from . import Loggable
    from ..merossclient.protocol import types as mt
    from ..merossclient.protocol.message import MerossResponse
    from .device import AsyncRequestFunc, Device
    from .entity import MLEntity

    type PollingStrategyFunc = Callable[["NamespaceHandler"], Coroutine]
    type NamespaceConfigType = tuple[int, int, int, int, PollingStrategyFunc | None]


class EntityDisablerMixin:
    """
    Special 'disabler' mixin used when the device pushes a message for a 'not yet'
    known entity/channel. The namespace handler will then dynamically mixin this
    disabler into the entity instance class initialization
    """

    # HA core entity attributes:
    entity_registry_enabled_default = False


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
        channel: object
        subId: object

        namespace_handlers: set["NamespaceHandler"]

    # This set will be created x instance when linking the parser to the handler
    namespace_handlers = None  # type: ignore

    async def async_shutdown(self):
        if self.namespace_handlers:
            for handler in set(self.namespace_handlers):
                handler.unregister(self)

    def _parse(self, payload: dict, /):
        """Default payload message parser. This is invoked automatically
        when the parser is registered to a NamespaceHandler for a given namespace
        and no 'better' _parse_xxxx has been defined. See NamespaceHandler.register.
        At this root level, coming here is likely an error but this feature
        (default parser) is being leveraged to setup a quick parsing route for some
        specific class of entities instead of having to define a specific _parse_xxxx.
        This is useful for generalized sensor classes which are just mapped to a single
        namespace."""
        self.log(
            self.WARNING,
            "Parsing undefined for payload:(%s)",
            str(payload),
            timeout=14400,
        )

    def _handle(self, header, payload, /):
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
            str(payload),
            timeout=14400,
        )


class NamespaceHandler:
    """
    This is the root class for somewhat dynamic namespace handlers.
    Every device keeps its own list of method handlers indexed through
    the message namespace in order to speed up parsing/routing when receiving
    a message from the device see Device.namespace_handlers and
    Device._handle to get the basic behavior.

    - handler: specify a custom handler method for this namespace. By default
    it will be looked-up in the device definition (looking for _handle_xxxxxx)

    - entity_class: specify a MLEntity type (actually an implementation
    of Merossentity) to be instanced whenever a message for a particular channel
    is received and the channel has no parser associated (see _handle_list)

    """

    if TYPE_CHECKING:
        parsers: dict[object, Callable[[dict], None]]
        polling_strategy: PollingStrategyFunc | None
        polling_request_channels: list[dict[str, Any]]

    __slots__ = (
        "device",
        "ns",
        "handler",
        "parsers",
        "entity_class",
        "lastrequest",
        "lastresponse",
        "polling_epoch_next",
        "polling_strategy",
        "polling_period",
        "polling_period_cloud",
        "polling_response_base_size",
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
        handler: (
            "Callable[[mt.MerossHeaderType, mt.MerossPayloadType], None] | None"
        ) = None,
        config: "NamespaceConfigType | None" = None,
    ):
        namespace = ns.name
        assert namespace not in device.namespace_handlers, (
            "Namespace already registered",
            namespace,
        )
        self.device = device
        self.ns = ns
        self.lastresponse = self.lastrequest = self.polling_epoch_next = 0.0
        self.parsers = {}
        self.entity_class = None
        self.handler = handler or getattr(
            device, f"_handle_{namespace.replace('.', '_')}", self._handle_undefined
        )

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
        self.polling_request_channels = []
        self.polling_request_configure(None)
        device.namespace_handlers[namespace] = self

    def polling_request_configure(self, request_payload_type: mn.PayloadType | None, /):
        """The structure of the polling payload is usually 'fixed' in the namespace
        grammar (see merossclient.namespaces.Namespace) but we have some exceptions
        here and there (one example is Refoss EM06) where the 'standard' is not valid.
        This method allows to refine this namespace parser behavior based off current
        device configuration/type at runtime. Needs to be called early on before
        registering any parser.
        Passing None as request_payload_type configures the default for the namespace.
        """
        ns = self.ns
        request_payload_type = request_payload_type or ns.request_payload_type
        if request_payload_type is mn.PayloadType.LIST_C:
            self.polling_request = (
                ns.name,
                mc.METHOD_GET,
                {ns.key: self.polling_request_channels},
            )
        elif request_payload_type is ns.request_payload_type:
            # we'll reuse the default in the ns definition
            self.polling_request = ns.request_default
        else:
            self.polling_request = (
                ns.name,
                mc.METHOD_GET,
                {ns.key: request_payload_type.value},
            )

    def polling_request_add_channel(self, channel, extra: dict = {}, /):
        # Ensures the channel is set in polling request payload should
        # the ns need it. Also adjusts the estimated polling_response_size.
        polling_request_channels = self.polling_request_channels
        key_channel = self.ns.key_channel
        for channel_payload in polling_request_channels:
            if channel_payload[key_channel] == channel:
                # using log_exception to signal a suspicious situation in testing
                self.device.log_exception(
                    self.device.WARNING,
                    Exception(f"channel({channel}) already present"),
                    f"polling_request_add_channel(ns={self.ns.name})",
                )
                break
        else:
            polling_request_channels.append({key_channel: channel} | extra)
        self.polling_response_size = (
            self.polling_response_base_size
            + len(polling_request_channels) * self.polling_response_item_size
        )

    def polling_request_set(self, payload: list | dict, /):
        self.polling_request = (
            self.ns.name,
            mc.METHOD_GET,
            {self.ns.key: payload},
        )
        self.polling_response_size = (
            self.polling_response_base_size
            + self.polling_response_item_size
            * (len(payload) if type(payload) is list else 1)
        )

    def polling_response_size_adj(self, item_count: int, /):
        self.polling_response_size = (
            self.polling_response_base_size
            + item_count * self.polling_response_item_size
        )

    def polling_response_size_inc(self):
        self.polling_response_size += self.polling_response_item_size

    def register_entity_class(
        self,
        entity_class: type["MLEntity"],
        /,
        *,
        initially_disabled: bool = True,
        build_from_digest: bool = False,
    ):
        self.entity_class = (
            type(entity_class.__name__, (EntityDisablerMixin, entity_class), {})
            if initially_disabled
            else entity_class
        )
        self.handler = self._handle_list
        self.device.platforms.setdefault(entity_class.PLATFORM)
        if build_from_digest:
            channels = set()

            def _scan_digest(digest: dict):
                if mc.KEY_CHANNEL in digest:
                    channels.add(digest[mc.KEY_CHANNEL])
                else:
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
        channel = getattr(parser, ns.key_channel)
        assert channel not in self.parsers, "parser already registered"
        self.parsers[channel] = getattr(parser, f"_parse_{ns.slug}", parser._parse)
        if not parser.namespace_handlers:
            parser.namespace_handlers = set()
        parser.namespace_handlers.add(self)
        self.polling_request_add_channel(channel)
        self.handler = self._handle_list

    def unregister(self, parser: "NamespaceParser", /):
        if self.parsers.pop(getattr(parser, self.ns.key_channel), None):
            parser.namespace_handlers.remove(self)

    def handle_exception(self, exception: Exception, function_name: str, payload, /):
        device = self.device
        device.log_exception(
            device.WARNING,
            exception,
            "%s(%s).%s: payload=%s",
            self.__class__.__name__,
            self.ns.name,
            function_name,
            str(device.loggable_any(payload)),
            timeout=604800,
        )

    def _handle_list(self, header, payload, /):
        """
        splits and forwards the received NS payload to
        the registered entity(es).
        This handler si optimized for list payloads:
        "payload": { "key_namespace": [{"channel":...., ...}] }
        """
        try:
            for p_channel in payload[self.ns.key]:
                try:
                    _parse = self.parsers[p_channel[self.ns.key_channel]]
                except KeyError as key_error:
                    _parse = self._try_create_entity(key_error)
                _parse(p_channel)
        except TypeError:
            # this might be expected: the payload is not a list
            self.handler = self._handle_dict
            self._handle_dict(header, payload)

    def _handle_dict(self, header, payload, /):
        """
        splits and forwards the received NS payload to
        the registered entity(es).
        This handler si optimized for dict payloads:
        "payload": { "key_namespace": {"channel":...., ...} }
        """
        p_channel = payload[self.ns.key]
        try:
            _parse = self.parsers[p_channel.get(self.ns.key_channel)]
        except KeyError as key_error:
            _parse = self._try_create_entity(key_error)
        except AttributeError:
            # this might be expected: the payload is not a dict
            # final fallback to the safe _handle_generic
            self.handler = self._handle_generic
            self._handle_generic(header, payload)
            return
        _parse(p_channel)

    def _handle_generic(self, header, payload, /):
        """
        splits and forwards the received NS payload to
        the registered entity(es)
        This handler can manage both lists or dicts or even
        payloads without the "channel" key (see namespace Toggle)
        which will default forwarding to channel == None
        """
        p_channel = payload[self.ns.key]
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

    def _handle_undefined(
        self, header: "mt.MerossHeaderType", payload: "mt.MerossPayloadType", /
    ):
        device = self.device
        device.log(
            device.DEBUG,
            "Handler undefined for method:%s namespace:%s payload:%s",
            header[mc.KEY_METHOD],
            header[mc.KEY_NAMESPACE],
            str(device.loggable_dict(payload)),
            timeout=14400,
        )
        if device.create_diagnostic_entities:
            # since we're parsing an unknown namespace, our euristic about
            # the key_namespace might be wrong so we use another euristic
            ns = self.ns
            for _key, _payload in payload.items():
                # since the ns_key might be often the same across different namespaces
                # we add the last split of the namespace to the extracted payload key
                if type(_payload) is dict:
                    self._parse_undefined_dict(
                        f"{ns.slug}_{_key}", _payload, _payload.get(ns.key_channel)
                    )
                else:
                    _key = f"{ns.slug}_{_key}"
                    for __payload in _payload:
                        # not having a "channel" in the list payloads is unexpected so far
                        self._parse_undefined_dict(
                            _key, __payload, __payload[ns.key_channel]
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
            self.ns.name,
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
            self.entity_class(self.device, channel)
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

    async def async_poll_lazy(self):
        """
        This strategy is for those namespaces which might be skipped now and then
        if they don't fit in the current ns_multiple request. Their delaying
        would be no harm since they typically carry rather unchanging values
        or data which are not 'critical'. For those namespaces, polling_period
        is considered the maximum amount of time after which the poll 'has' to
        be done. If it hasn't elapsed then they're eventually packed
        with the outgoing ns_multiple
        """
        device = self.device
        if device._polling_epoch >= self.polling_epoch_next:
            await device.async_request_smartpoll(self)
        else:
            device.request_lazypoll(self)

    async def async_poll_smart(self):
        device = self.device
        if device._polling_epoch >= self.polling_epoch_next:
            if not await device.async_request_smartpoll(self):
                # if the cloud MQTT limit hitted try to compete for lazypolls
                device.request_lazypoll(self)

    async def async_poll_once(self):
        """
        This strategy is for 'constant' namespace data which do not change and only
        need to be requested once (after onlining that is). When polling use
        same queueing policy as async_poll_smart to don't overwhelm the cloud mqtt
        """
        if not self.polling_epoch_next:
            await self.device.async_request_smartpoll(self)

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
        if device._polling_epoch >= self.polling_epoch_next:
            await device.async_request_smartpoll(self)

    async def async_trace(self, async_request_func: "AsyncRequestFunc", /):
        """
        Used while tracing abilities. Depending on our 'knowledge' of this ns
        we're going a straigth route (when the ns is well-known) or experiment some
        euristics.
        """
        ns = self.ns
        if ns.grammar is mn.Grammar.STABLE:
            if (ns.has_get is False) and (ns.has_push_query is False):
                # corresponding to _ns_no_query definitions in merossclient.namespaces
                return
            if (ns.request_payload_type is mn.PayloadType.LIST_C) and (
                not self.polling_request_channels
            ):
                # when a 'LIST_C' namespace has no registered parsers, self.polling_request will fail
                # so we use the mocked default request
                await async_request_func(*ns.request_default)
                return
            await async_request_func(*self.polling_request)
            return

        ns_name = ns.name
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
                await async_request_func(
                    ns_name, mc.METHOD_PUSH, ns.DEFAULT_PUSH_PAYLOAD
                )

                if self.device.get_type() is mlc.DeviceType.DEVICE:
                    channels = self.parsers.keys() or (0,)
                else:  # it is an hub
                    channels = self.device.subdevices

                channels_count = len(channels)
                channels_payload = [{ns_key_channel: channel} for channel in channels]
                # We'll try then querying with those different payload structures as they're well known
                # for channelized devices, starting from the most complex (verbose) to the least one.
                # If any of these works it will candidate for this NamespaceHandler polling_request format.
                detected_request_payload_type: mn.PayloadType | None = None

                def _check_response(_response: "MerossResponse | None"):
                    if _response:
                        try:
                            _response = check_message_strict(_response)
                            payload = _response["payload"][ns_key]
                            if type(payload) is list:
                                return channels_count == len(payload)
                            else:  # dict
                                return channels_count == 1
                        except Exception:
                            pass
                    return False

                if _check_response(
                    await async_request_func(
                        ns_name, mc.METHOD_GET, {ns_key: channels_payload}
                    )
                ):
                    detected_request_payload_type = mn.PayloadType.LIST_C
                if _check_response(
                    await async_request_func(ns_name, mc.METHOD_GET, {ns_key: []})
                ):
                    detected_request_payload_type = mn.PayloadType.LIST
                if _check_response(
                    await async_request_func(ns_name, mc.METHOD_GET, {ns_key: {}})
                ):
                    detected_request_payload_type = mn.PayloadType.DICT

                if detected_request_payload_type:
                    # this will override the request_payload format from its default
                    self.polling_request_configure(detected_request_payload_type)
                    return

                # If our 'well-known' heuristics don't work, try these exotic queries:
                # looking for DICT_C request type
                for channel_payload in channels_payload:
                    # If this querying format works but none of the other does then we'll
                    # need to implement async_poll_digest in order to send the whole set of requests
                    # needed to poll the digest
                    await async_request_func(
                        ns_name, mc.METHOD_GET, {ns_key: channel_payload}
                    )
                # Also check if hub namespaces indexed by "subId" maybe also need a "channel"
                if ns_key_channel == mc.KEY_SUBID:
                    await async_request_func(
                        ns_name,
                        mc.METHOD_GET,
                        {
                            ns_key: [
                                {ns_key_channel: channel, mc.KEY_CHANNEL: 0}
                                for channel in channels
                            ]
                        },
                    )

            case mn.Grammar.UNKNOWN:
                # We don't know yet how to query this ns so we'll brute-force it
                if ns.has_push_query is not False:
                    response = await async_request_func(
                        ns_name, mc.METHOD_PUSH, ns.DEFAULT_PUSH_PAYLOAD
                    )
                    if response and (
                        response[mc.KEY_HEADER][mc.KEY_METHOD] == mc.METHOD_PUSH
                    ):
                        for key in response[mc.KEY_PAYLOAD]:
                            ns_key = key
                            break

                if ns.has_get is not False:

                    def _is_getack(_response: dict | None):
                        return _response and (
                            _response[mc.KEY_HEADER][mc.KEY_METHOD] == mc.METHOD_GETACK
                        )

                    response = await async_request_func(
                        ns_name, mc.METHOD_GET, {ns.key: []}
                    )
                    if _is_getack(response):
                        ns_key = ns.key
                    else:
                        # ns.key might be wrong or verb GET unsupported
                        if ns.key != ns_key:
                            # try the namespace key from PUSH attempt
                            response = await async_request_func(
                                ns_name, mc.METHOD_GET, {ns_key: []}
                            )
                        if (not _is_getack(response)) and ns.key.endswith("x"):
                            # euristic(!)
                            ns_key = ns.key[:-1]
                            response = await async_request_func(
                                ns_name, mc.METHOD_GET, {ns_key: []}
                            )
                        if not _is_getack(response):
                            # no chance
                            return

                    response_payload = response[mc.KEY_PAYLOAD].get(ns_key)  # type: ignore
                    if response_payload or (type(response_payload) is not list):
                        return
                    # the namespace might need a channel index in the request
                    if self.device.get_type() is mlc.DeviceType.DEVICE:
                        await async_request_func(
                            ns_name,
                            mc.METHOD_GET,
                            {ns_key: [{mc.KEY_CHANNEL: 0}]},
                        )
                    else:  # it is an hub
                        subdevices = self.device.subdevices
                        # typical 'legacy' devices are queried by "id"
                        response = await async_request_func(
                            ns_name,
                            mc.METHOD_GET,
                            {
                                ns_key: [
                                    {mc.KEY_ID: subdevice_id}
                                    for subdevice_id in subdevices
                                ]
                            },
                        )
                        if _is_getack(response):
                            response_payload = response[mc.KEY_PAYLOAD].get(ns_key)  # type: ignore
                            if response_payload:
                                return
                        # many other new ones (ms130 for example) need a "subId"
                        response = await async_request_func(
                            ns_name,
                            mc.METHOD_GET,
                            {
                                ns_key: [
                                    {mc.KEY_SUBID: subdevice_id}
                                    for subdevice_id in subdevices
                                ]
                            },
                        )
                        if _is_getack(response):
                            response_payload = response[mc.KEY_PAYLOAD].get(ns_key)  # type: ignore
                            if response_payload:
                                return
                        # finally: try also setting a "channel" (it is carried in messages from the subdevice)
                        await async_request_func(
                            ns_name,
                            mc.METHOD_GET,
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

    manager: "Device"

    async def async_added_to_hass(self):
        self.manager.get_handler(self.ns).polling_strategy = POLLING_STRATEGY_CONF[
            self.ns
        ][4]
        return await super().async_added_to_hass()

    async def async_will_remove_from_hass(self):
        self.manager.get_handler(self.ns).polling_strategy = None
        return await super().async_will_remove_from_hass()


class EntityNamespaceHandler(NamespaceHandler):
    """
    Utility class to manage namespaces which are mapped to a single entity.
    This will act as an helper in initialization
    """

    def __init__(self, entity: "EntityNamespaceMixin"):
        NamespaceHandler.__init__(
            self,
            entity.manager,
            entity.ns,
            handler=getattr(
                entity, f"_handle_{entity.ns.name.replace('.', '_')}", entity._handle
            ),
        )
        if not entity.hass_connected:
            # if initially disabled then uninstall default strategy
            # EntityNamespaceMixin will manage enabling/disabling
            self.polling_strategy = None


class VoidNamespaceHandler(NamespaceHandler):
    """Utility class to manage namespaces which should be 'ignored' i.e. we're aware
    of their existence but we don't process them at the device level. This class in turn
    just provides an empty handler and so suppresses any log too (for unknown namespaces)
    done by the base default handling."""

    def __init__(self, device: "Device", namespace: "mn.Namespace"):
        NamespaceHandler.__init__(self, device, namespace, handler=self._handle_void)

    def _handle_void(self, header, payload, /):
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
        NamespaceHandler.async_poll_lazy,
    ),
    mn.Appliance_System_Runtime: (
        mlc.PARAM_SENSOR_SLOW_UPDATE_PERIOD,
        mlc.PARAM_CLOUDMQTT_UPDATE_PERIOD,
        330,
        0,
        NamespaceHandler.async_poll_lazy,
    ),
    mn.Appliance_Config_OverTemp: (
        mlc.PARAM_CONFIG_UPDATE_PERIOD,
        mlc.PARAM_CLOUDMQTT_UPDATE_PERIOD,
        340,
        0,
        NamespaceHandler.async_poll_lazy,
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
        320,
        53,
        NamespaceHandler.async_poll_smart,
    ),
    mn.Appliance_Control_Diffuser_Sensor: (
        mlc.PARAM_SENSOR_SLOW_UPDATE_PERIOD,
        mlc.PARAM_SENSOR_SLOW_UPDATE_CLOUD_PERIOD,
        mlc.PARAM_HEADER_SIZE,
        100,
        NamespaceHandler.async_poll_lazy,
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
        NamespaceHandler.async_poll_lazy,
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
        NamespaceHandler.async_poll_lazy,
    ),
    mn.Appliance_Control_Presence_Config: (
        mlc.PARAM_CONFIG_UPDATE_PERIOD,
        mlc.PARAM_CLOUDMQTT_UPDATE_PERIOD,
        mlc.PARAM_HEADER_SIZE,
        260,
        NamespaceHandler.async_poll_lazy,
    ),
    mn.Appliance_Control_Screen_Brightness: (
        mlc.PARAM_CONFIG_UPDATE_PERIOD,
        mlc.PARAM_CLOUDMQTT_UPDATE_PERIOD,
        mlc.PARAM_HEADER_SIZE,
        70,
        NamespaceHandler.async_poll_lazy,
    ),
    mn.Appliance_Control_Sensor_Latest: (
        mlc.PARAM_SENSOR_SLOW_UPDATE_PERIOD,
        mlc.PARAM_SENSOR_SLOW_UPDATE_CLOUD_PERIOD,
        mlc.PARAM_HEADER_SIZE,
        80,
        NamespaceHandler.async_poll_lazy,
    ),
    mn.Appliance_Control_Sensor_LatestX: (
        mlc.PARAM_SENSOR_SLOW_UPDATE_PERIOD,
        mlc.PARAM_CLOUDMQTT_UPDATE_PERIOD,
        mlc.PARAM_HEADER_SIZE,
        220,
        NamespaceHandler.async_poll_lazy,
    ),
    mn.Appliance_GarageDoor_Config: (
        mlc.PARAM_CONFIG_UPDATE_PERIOD,
        mlc.PARAM_CLOUDMQTT_UPDATE_PERIOD,
        410,
        0,
        NamespaceHandler.async_poll_lazy,
    ),
    mn.Appliance_GarageDoor_MultipleConfig: (
        mlc.PARAM_CONFIG_UPDATE_PERIOD,
        mlc.PARAM_CLOUDMQTT_UPDATE_PERIOD,
        mlc.PARAM_HEADER_SIZE,
        140,
        NamespaceHandler.async_poll_lazy,
    ),
    mn.Appliance_RollerShutter_Adjust: (
        mlc.PARAM_CONFIG_UPDATE_PERIOD,
        mlc.PARAM_CLOUDMQTT_UPDATE_PERIOD,
        mlc.PARAM_HEADER_SIZE,
        35,
        NamespaceHandler.async_poll_lazy,
    ),
    mn.Appliance_RollerShutter_Config: (
        mlc.PARAM_CONFIG_UPDATE_PERIOD,
        mlc.PARAM_CLOUDMQTT_UPDATE_PERIOD,
        mlc.PARAM_HEADER_SIZE,
        70,
        NamespaceHandler.async_poll_lazy,
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
