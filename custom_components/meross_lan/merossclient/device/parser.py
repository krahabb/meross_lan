from functools import cached_property
from typing import TYPE_CHECKING, final, override

from .. import logging, merge_dicts
from ..protocol import const as mc, namespaces as mn

if TYPE_CHECKING:
    from typing import Any, ClassVar, Final, NotRequired, Protocol, Unpack

    from . import Device, PhysicalDevice
    from ..protocol.types import JsonDict, JsonList, JsonMapping
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
            type ParsersContainer = list[
                "NamespaceHandler.ParserFunc | NamespaceParser"
            ]
            parsers: Final[ParsersContainer]

        __slots__ = ("parsers",)

        def __init__(self, *parsers: "NamespaceHandler.ParserFunc | NamespaceParser"):
            self.parsers = list(parsers)

        def __call__(self, payload: "JsonMapping", /):
            for parser in self.parsers:
                parser(payload)

    if TYPE_CHECKING:
        parent: Final[PhysicalDevice]  # type: ignore[override]
        init_ns: ClassVar[mn.Namespace]
        """Class default used to initialize the 'ns' instance attribute."""
        ns: mn.Namespace
        """The (primary) namespace this parser is associated with. This is used to issue requests."""
        index: Final[mn.IndexValue]  # type: ignore
        """The channel/id/subId key value according to the namespace (indexed or not).
        This is used by the NamespaceHandler to route messages to the correct parser."""
        ns_payload: JsonMapping  # type: ignore[assignment]
        """The last parsed payload."""
        _handler_registrations: Final[list[tuple[NamespaceHandler, mn.IndexValue]]]
        """Set of NamespaceHandlers this parser is registered to. This is used to manage the link back
        to the handler for issuing requests and for cleanup on shutdown."""

        class Args(logging.Loggable.Args):
            ns: NotRequired[mn.Namespace]
            index: NotRequired[mn.IndexValue]

        def __init__(self, id, parent: PhysicalDevice, /, **kwargs: Unpack[Args]): ...

    init_ns_payload = mn.EMPTY_DICT
    init_index = mn.IndexType.none()
    SLOTS_AUTO_INIT = (
        "ns",
        "ns_payload",
        "index",
    )
    __SLOTS__ = ("_handler_registrations",)

    def shutdown(self):
        super().shutdown()
        try:
            _dispatcher: "NamespaceParser.Dispatcher"
            for handler, index in self._handler_registrations:
                _dispatcher = handler.parsers[index]  # type: ignore[assignment]
                if type(_dispatcher) is NamespaceParser.Dispatcher:
                    # remove from dispatcher
                    _dispatcher.parsers.remove(
                        getattr(self, f"_parse_{handler.id.slug_end}", self)
                    )
                    if not _dispatcher.parsers:
                        del handler.parsers[index]
                else:
                    del handler.parsers[index]
            self._handler_registrations.clear()
        except AttributeError:  # never registered
            pass
        try:
            del self.__dict__["handler_ns"]  # type: ignore
        except KeyError:
            pass

    def _namespace_registered(
        self, handler_registration: tuple["NamespaceHandler", mn.IndexValue], /
    ):
        """This is called by the NamespaceHandler when registering this parser to the handler.
        This is useful to setup the link back to the NamespaceHandler for issuing requests.
        """
        try:
            self._handler_registrations.append(handler_registration)
        except AttributeError:
            self._handler_registrations = [handler_registration]  # type: ignore[assignment]

    @cached_property
    def handler_ns(self):
        return self.parent.ns_handlers[self.ns]

    @final
    async def async_request_payload(self, payload: "JsonDict", /):
        return await self.parent.async_request(
            *self.ns.request_set(self.index | payload)
        )

    @final
    async def async_request_parse(self, payload: "JsonDict", /):
        response = await self.parent.async_request(
            *self.ns.request_set(self.index | payload)
        )
        self(payload)
        return response

    @final
    async def async_request_parse_ex(self, payload: "JsonDict", /):
        response = await self.parent.async_request(
            *self.ns.request_set(self.index | payload)
        )
        self(merge_dicts(dict(self.ns_payload), payload))
        return response

    def __call__(self, payload: "JsonMapping", /):
        """Default payload message parser. This is invoked by the NamespaceHandler
        routing mechanics when the parser is registered as a sink and no specific
        _parse_{NamespaceHandler.id.slug_end} is available.
        As a convention this is also the 'official' parser method for self.ns related
        payloads and thus invoked as a callback when succesfully sending SET requests.
        """
        self.ns_payload = payload
        self.log(
            self.WARNING,
            "Parsing undefined for payload:(%s)",
            _payload=payload,
            timeout=14400,
        )

    @classmethod
    def namespace_init(cls, ns: mn.Namespace, device: "Device", /):
        """Helper to register a specialized entity class to the proper namespace.
        This is going to be used on Device initialization for various entities sharing
        common semantics in namespace parsing/handling."""
        device._create_handler(ns, parser_class=cls)


class NamespaceValue(NamespaceParser):
    """A specialization of NamespaceParser providing a simple interface to manage
    a single item value in the namespace payload."""

    class _KeyValueDescriptor(str):
        """Descriptor class to define how to extract the value from the payload and how to format it for requests.
        In general, most of the device_value data are stored in the first level key of a dictionary payload, but in
        some cases they are stored in nested dictionaries. This descriptor allows to abstract this logic and provide
        a consistent interface for both cases."""

        if TYPE_CHECKING:
            # Using language specials to implement our custom methods
            # in order to make the code less readable ;)
            def __call__(self, value) -> "JsonDict": ...

            """Returns a dict with the key(s) defined in this descriptor and the value provided as argument."""

            def __getitem__(self, payload: "JsonMapping"): ...

            """Extracts the value from the payload using the key(s) defined in this descriptor."""

    class SimpleKeyValue(_KeyValueDescriptor):
        """Descriptor for the simple case where the value is stored in the first level key of the payload."""

        def __call__(self, value):
            return {self: value}

        def __getitem__(self, payload: "JsonMapping"):
            return payload[self]

    class NestedKeyValue(_KeyValueDescriptor):

        if TYPE_CHECKING:
            keys: tuple[str, ...]

        __slots__ = ("keys",)

        def __new__(cls, *keys: str):
            return super().__new__(cls, "_".join(keys))

        def __init__(self, *keys: str):
            self.keys = keys

        def __call__(self, value):
            for key in reversed(self.keys):
                value = {key: value}
            return value

        def __getitem__(self, payload: "JsonMapping"):
            for key in self.keys:
                payload = payload[key]
            # TODO: test this generator expression for performance and readability against the more straightforward loop --- IGNORE ---
            # (payload := payload[key] for key in self.keys)
            return payload

    if TYPE_CHECKING:

        init_key_value: ClassVar[_KeyValueDescriptor]
        key_value: _KeyValueDescriptor
        device_value: Any

        class Args(NamespaceParser.Args):
            key_value: NotRequired[NamespaceValue._KeyValueDescriptor]
            device_value: NotRequired[Any]

        def __init__(self, id, parent: PhysicalDevice, /, **kwargs: Unpack[Args]): ...

    init_key_value = SimpleKeyValue(mc.KEY_VALUE)

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
        # await self.async_request_payload({self.key_value: device_value})
        await self.parent.async_request(
            *self.ns.request_set(self.index | self.key_value(device_value))
        )
        self.update_device_value(device_value)

    @override  # NamespaceParser
    def __call__(self, payload: "JsonMapping", /):
        self.ns_payload = payload
        self.update_device_value(self.key_value[payload])


class NamespaceBoolean(NamespaceValue):
    """A specialization of NamespaceValue to manage boolean values with custom on/off values in the device.
    By default it assumes that the device uses 1 for 'on' and 0 for 'off', but this can be customized by setting the
    'value_on' and 'value_off' attributes."""

    if TYPE_CHECKING:
        value_on: int
        """The actual device value representing the 'on' state."""
        value_off: int
        """The actual device value representing the 'off' state."""
        is_on: bool | None

        class Args(NamespaceValue.Args):
            value_on: NotRequired[int]
            value_off: NotRequired[int]
            is_on: NotRequired[bool]

    init_key_value = NamespaceValue.SimpleKeyValue(mc.KEY_ONOFF)
    init_value_on = 1
    init_value_off = 0

    SLOTS_AUTO_INIT = ("value_on", "value_off", "is_on")

    @override
    def update_device_value(self, device_value, /) -> bool | None:
        if self.device_value != device_value:
            self.device_value = device_value
            match device_value:
                case self.value_on:
                    self.is_on = True
                case self.value_off:
                    self.is_on = False
                case _:
                    self.is_on = None
            return True

    # interface compatibility with HA toggle entities, allowing to use this class as a
    # mixin with other NamespaceParser specializations
    async def async_turn_on(self, **kwargs):
        await self.async_request_value(self.value_on)

    async def async_turn_off(self, **kwargs):
        await self.async_request_value(self.value_off)
