from functools import cached_property
from typing import TYPE_CHECKING, final, override

from .. import logging, merge_dicts
from ..protocol import const as mc, namespaces as mn

if TYPE_CHECKING:
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
    The class implementing the NamespaceParser protocol needs to expose that key value as a
    property with the same name. 99% of the time the class is a MLEntity with its "channel"
    property but the implementation allows more versatility.
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

        def __init__(
            self, parsers: "Iterable[NamespaceHandler.ParserFunc] | None" = None, /
        ):
            self.parsers = list(parsers) if parsers is not None else []

        def __call__(self, payload: "JsonMapping", /):
            for parser in self.parsers:
                parser(payload)

    if TYPE_CHECKING:
        # These properties must be implemented in derived classes according to the
        # namespace payload syntax. NamespaceHandler will lookup any of these when
        # establishing the link between the handler and the parser
        parent: Final[PhysicalDevice]  # type: ignore[override]
        ns: mn.Namespace  # same (only MLEntity for now)
        # TODO/BEWARE: these are not yet initialized here and they
        # are expected to be set by the derived class
        channel: PayloadIndexType | None  # TODO: rename to 'index'
        """The channel/id/subId key value according to the namespace (indexed or not)."""
        ns_payload: JsonMapping  # type: ignore[assignment]
        """The last parsed payload."""
        _ns_handlers: set[NamespaceHandler]
        """Set of NamespaceHandlers this parser is registered to. This is used to manage the link back
        to the handler for issuing requests and for cleanup on shutdown."""

    __SLOTS__ = ("channel", "ns_payload", "_ns_handlers")

    def shutdown(self):
        super().shutdown()
        try:
            for handler in self._ns_handlers:
                _dispatcher: NamespaceParser.Dispatcher = handler.parsers[self.channel]  # type: ignore
                if type(_dispatcher) is NamespaceParser.Dispatcher:
                    # remove from dispatcher
                    _dispatcher.parsers.remove(
                        getattr(self, f"_parse_{handler.id.slug_end}", self._parse)
                    )
                    if not _dispatcher.parsers:
                        del handler.parsers[self.channel]
                else:
                    del handler.parsers[self.channel]
            del self._ns_handlers
            del self.handler_ns
        except (TypeError, AttributeError):  # never registered
            pass

    def _namespace_registered(self, handler: "NamespaceHandler", /):
        """This is called by the NamespaceHandler when registering this parser to the handler.
        This is useful to setup the link back to the NamespaceHandler for issuing requests.
        """
        try:
            self._ns_handlers.add(handler)
        except AttributeError:
            self._ns_handlers = {handler}

    @cached_property
    def ns_payload(self) -> "JsonMapping":
        """The last parsed payload. This is set by the default _parse method but it can be
        used by derived classes to store the last parsed payload for later use, such as
        when issuing a request to update a value in the device and needing to merge the
        request payload with the last known state of the whole namespace."""
        return mn.EMPTY_DICT

    @cached_property
    def handler_ns(self):
        # TODO: define a more consistent interface
        # This is right now a brutal hack to automagically provide ns_handler property
        # to entities which might not need to be registered parsers but still need to access
        # the NamespaceHandler to issue device requests. Most of the times these are entities
        # where ns parsing is delegated to a container object/handler which is then dispatching
        # updates without using the NamespaceHandler inner mechanisms.
        return self.parent.ns_handlers[self.ns]

    # TODO: rename to async_request
    async def async_request_payload(self, payload: "JsonDict", /):
        return await self.parent.async_request(
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


class NamespaceValue(NamespaceParser):
    """A specialization of NamespaceParser providing a simple interface to manage
    a single item value in the namespace payload."""

    if TYPE_CHECKING:
        key_value: ClassVar[str] | str
        device_value: Any  # type: ignore[assignment]

    key_value = mc.KEY_VALUE

    __SLOTS__ = ("device_value",)

    @cached_property
    def device_value(self) -> "Any":
        return None

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

    key_value = mc.KEY_ONOFF
    native_on = 1
    native_off = 0

    __SLOTS__ = ("is_on",)

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
    @cached_property
    def is_on(self) -> bool | None:
        """Return True if entity is on."""
        return None

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
        key_group: ClassVar[str] | str
        key_value: ClassVar[str] | str

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
