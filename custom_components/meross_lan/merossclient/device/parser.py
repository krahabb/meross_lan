from functools import cached_property
from typing import TYPE_CHECKING, final

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

    def shutdown(self):
        super().shutdown()
        try:
            for handler in self._namespace_handlers:
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
        return self.parent.ns_handlers[self.ns]

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
