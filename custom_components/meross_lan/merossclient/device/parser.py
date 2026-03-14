from functools import cached_property
from typing import TYPE_CHECKING, final, override

from .. import logging, merge_dicts
from ..protocol import const as mc, namespaces as mn

if TYPE_CHECKING:
    from typing import Any, ClassVar, Final, NotRequired, Unpack

    from . import Device, PhysicalDevice
    from ..protocol.types import (
        JsonDict,
        JsonList,
        JsonMapping,
        PayloadIndexType,
    )
    from .handler import NamespaceHandler


class NamespaceParser(logging.Loggable):
    """
    Represents the final 'parser' of a message after 'handling' in NamespaceHandler.
    In this model, NamespaceHandler is responsible for unpacking those messages
    who are intended to be delivered to different entities based off some indexing
    keys. These are typically: "channel", "Id", "subId" depending on the namespace itself.
    The protocol implementation needs to also expose a proper _parse_{key_namespace}
    (see NamespaceHandler.register_parser).
    """

    @final
    class Dispatcher:
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

        def __init__(self, *parsers: "NamespaceHandler.ParserFunc"):
            self.parsers = list(parsers)

        def __call__(self, payload: "JsonMapping", /):
            for parser in self.parsers:
                parser(payload)

    if TYPE_CHECKING:
        NS_CHANNELS: ClassVar[tuple[int, ...] | None]
        """
        This is related to NamespaceHandler registration. For parser classes where we know
        the ns exposes fixed channel layouts (i.e. PhysicalLock) which are not exposed in any digest key
        we can set this to (0,) or more funny presets so that namespace initialization will also
        automatically build the needed parsers.
        Setting to None means 'scan digests for channels'.
        This is actually not mandatory though since only used for NamespaceHandler.register_parser_class.
        in NamespaceParser.namespace_init
        """
        NS_CHANNELS_SINGLE: Final[tuple[int, ...]]
        """Preset singleton for parsers to be configured with a single channel in 0."""

        parent: Final[PhysicalDevice]  # type: ignore[override]
        init_ns: ClassVar[mn.Namespace]
        """Class default used to initialize the 'ns' instance attribute."""
        ns: mn.Namespace
        """The (primary) namespace this parser is associated with. This is used to issue requests."""
        channel: PayloadIndexType | None  # type: ignore[assignment]
        """The channel/id/subId key value according to the namespace (indexed or not).
        This is used by the NamespaceHandler to route messages to the correct parser.
        This is expected to be initialized by derived classes according to the namespace syntax."""
        ns_payload: JsonMapping  # type: ignore[assignment]
        """The last parsed payload."""
        handlers: Final[dict[mn.Namespace, NamespaceHandler]]
        """Set of NamespaceHandlers this parser is registered to. This is used to manage the link back
        to the handler for issuing requests and for cleanup on shutdown."""

        class Args(logging.Loggable.Args):
            ns: NotRequired[mn.Namespace]
            # channel: NotRequired[PayloadIndexType | None]

        def __init__(
            self,
            channel: PayloadIndexType | None,
            parent: PhysicalDevice,
            /,
            **kwargs: Unpack[Args],
        ): ...

    NS_CHANNELS = None  # scan digests for channels
    NS_CHANNELS_SINGLE = (0,)

    init_ns_payload = mn.EMPTY_DICT
    SLOTS_AUTO_INIT = (
        "ns",
        "ns_payload",
    )
    __SLOTS__ = ("channel", "handlers")

    def shutdown(self):
        super().shutdown()
        try:
            _dispatcher: "NamespaceParser.Dispatcher"
            for handler in self.handlers.values():
                _dispatcher = handler.parsers[self.channel]  # type: ignore[assignment]
                if type(_dispatcher) is NamespaceParser.Dispatcher:
                    # remove from dispatcher
                    _dispatcher.parsers.remove(
                        getattr(self, f"_parse_{handler.id.slug_end}", self._parse)
                    )
                    if not _dispatcher.parsers:
                        del handler.parsers[self.channel]
                else:
                    del handler.parsers[self.channel]
            del self.handlers  # type: ignore[assignment]
            del self.handler_ns
        except (TypeError, AttributeError):  # never registered
            pass

    def _namespace_registered(self, handler: "NamespaceHandler", /):
        """This is called by the NamespaceHandler when registering this parser to the handler.
        This is useful to setup the link back to the NamespaceHandler for issuing requests.
        """
        try:
            assert (
                handler.id not in self.handlers
            ), "NamespaceParser already registered to this NamespaceHandler"
            self.handlers[handler.id] = handler
        except AttributeError:
            self.handlers = {handler.id: handler}  # type: ignore[assignment]
            self.ns_payload = mn.EMPTY_DICT

    @cached_property
    def handler_ns(self):
        return self.parent.ns_handlers[self.ns]

    # TODO: rename to async_request
    async def async_request_payload(self, payload: "JsonDict", /):
        return await self.parent.async_request(
            *self.ns.request_set(payload, self.channel)
        )

    async def async_request_parse(self, payload: "JsonDict", /):
        response = await self.async_request_payload(payload)
        getattr(self, f"_parse_{self.ns.slug_end}", self._parse)(payload)
        return response

    async def async_request_parse_ex(self, payload: "JsonDict", /):
        response = await self.async_request_payload(payload)
        getattr(self, f"_parse_{self.ns.slug_end}", self._parse)(
            merge_dicts(dict(self.ns_payload), payload)
        )
        return response

    def _parse(self, payload: "JsonMapping", /):
        """Default payload message parser. This is invoked by the NamespaceHandler
        default routing mechanics when the parser is registered to a NamespaceHandler.
        """
        self.ns_payload = payload
        self.log(
            self.WARNING,
            "Parsing undefined for payload:(%s)",
            _payload=payload,
            timeout=14400,
        )

    @classmethod
    def digest_init(
        cls, device: "Device", digest: "JsonList", /
    ) -> "Device.DigestInitReturnType":
        """Helper to register and instantiate a specialized entity class to the proper namespace.
        This is going to be used on Device initialization for entities that maps to device
        digest payload. This kind of initialization is alternative to namespace_init and
        generally richer (not every namespace has 'digest' entities though - namespace_init is
        for that semantics)."""
        handler = device._create_handler(cls.init_ns)
        handler.register_parser_class(
            cls, (_digest[mc.KEY_CHANNEL] for _digest in digest)
        )
        return handler.parse_list, (handler,)

    @classmethod
    def namespace_init(cls, ns: mn.Namespace, device: "Device", /):
        """Helper to register a specialized entity class to the proper namespace.
        This is going to be used on Device initialization for various entities sharing
        common semantics in namespace parsing/handling."""
        assert ns is cls.init_ns
        device._create_handler(ns).register_parser_class(cls, cls.NS_CHANNELS)


class NamespaceValue(NamespaceParser):
    """A specialization of NamespaceParser providing a simple interface to manage
    a single item value in the namespace payload."""

    if TYPE_CHECKING:

        init_key_value: ClassVar[str]
        key_value: str
        device_value: Any

        class Args(NamespaceParser.Args):
            key_value: NotRequired[str]
            device_value: NotRequired[Any]

    init_key_value = mc.KEY_VALUE

    SLOTS_AUTO_INIT = ("key_value", "device_value")

    def update_device_value(self, device_value, /) -> bool | None:
        # Called when the device value is being updated, either by parsing a new payload or by issuing a request.
        # This is intended as a placeholder to be overridden by derived classes to implement custom logic on device value update,
        # such as updating the entity state or triggering side effects. By default, it just updates the internal
        # device_value and returns True if the value has changed, False otherwise.
        if self.device_value != device_value:
            self.device_value = device_value
            return True

    async def async_request_value(self, device_value, /) -> None:
        """Issues a command SET to update the device and also updates
        the entity state if the command was acknowledged by the device.
        Raises exception on connection/protocol errors."""
        await self.async_request_payload({self.key_value: device_value})
        self.update_device_value(device_value)

    @override  # NamespaceParser
    def _parse(self, payload: "JsonMapping", /):
        self.ns_payload = payload
        self.update_device_value(payload[self.key_value])


class NamespaceBoolean(NamespaceValue):
    """A specialization of NamespaceValue to manage boolean values with custom on/off values in the device.
    By default it assumes that the device uses 1 for 'on' and 0 for 'off', but this can be customized by setting the
    'native_on' and 'native_off' class (or instance) attributes."""

    if TYPE_CHECKING:
        native_on: ClassVar[int] | int
        """The actual device value representing the 'on' state."""
        native_off: ClassVar[int] | int
        """The actual device value representing the 'off' state."""
        is_on: bool | None

        class Args(NamespaceValue.Args):
            is_on: NotRequired[bool]

    init_key_value = mc.KEY_ONOFF
    native_on = 1
    native_off = 0

    SLOTS_AUTO_INIT = ("is_on",)

    @override
    def update_device_value(self, device_value, /) -> bool | None:
        if self.device_value != device_value:
            self.device_value = device_value
            match device_value:
                case self.native_on:
                    self.is_on = True
                case self.native_off:
                    self.is_on = False
                case _:
                    self.is_on = None
            return True

    # interface compatibility with HA toggle entities, allowing to use this class as a
    # mixin with other NamespaceParser specializations
    async def async_turn_on(self, **kwargs):
        await self.async_request_value(self.native_on)

    async def async_turn_off(self, **kwargs):
        await self.async_request_value(self.native_off)


class NamespaceGroupValue(NamespaceValue):
    """
    Parser for payload values embedded in a(sub)dictionary in the namespace payload. The key of the
    dictionary is defined by the 'key_group' attribute and the value is defined by 'key_value'.
    This class could also be used as a mixin with other NamespaceParser specializations.
    """

    if TYPE_CHECKING:
        init_key_group: ClassVar[str]
        key_group: str

        class Args(NamespaceValue.Args):
            key_group: NotRequired[str]

    init_key_group = mc.KEY_VALUE

    SLOTS_AUTO_INIT = ("key_group",)

    @override
    async def async_request_value(self, device_value, /):
        await self.async_request_payload(
            {self.key_group: {self.key_value: device_value}}
        )
        self.update_device_value(device_value)

    @override
    def _parse(self, payload, /):
        self.ns_payload = payload
        self.update_device_value(payload[self.key_group][self.key_value])
