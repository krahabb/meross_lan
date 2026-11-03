"""
Meross client library - exceptions
"""

from functools import cached_property
from typing import TYPE_CHECKING

import aiohttp

if TYPE_CHECKING:
    from typing import Final, Unpack

    from .client import AbstractClient
    from .logging import LoggerArgs
    from .protocol.message import MerossMessage


class MerossError(Exception):
    """Base class for any exception raised by the library."""

    def __str__(self):
        # This flexible dumper is useful when the exception args contain other exceptions
        # (like when chaining exceptions) and automatically extracts the original exception message.
        return ", ".join(str(a) or repr(a) for a in self.args)


class MerossTransportError(MerossError):
    """This is mainly used to carry exceptions informations raised in AbstractClients while trying to send
    requests to Meross devices and also provides an interface ('log') to uniformly log a message
    to the client logger pipe reporting some request context.
    It generally signals a transport error like:
    - connection error
    - unexpected disconnection.
    - inconsistent state or any other generic (and unexpected) error
    i.e. any reason that prevents the message to be delivered.
    The library code (AbstractClient and descendants) should try any effort to wrap any
    exception into this exception (for example those thrown by the aiohttp package),
    so that the user code can have a uniform way to handle transport errors.
    The original exception (if any) should be available as the __cause__ attribute of this exception.
    It also allows to wrap other MerossTransportError so to add chaining while traversing different
    clients (as for AbstractMQTTConnection.Client).
    When chaining MerossTransportError, the client pipe and request context eventually used to log
    is the outermost while the chain is only traversed to recover the original 'cause' error
    """

    def __init__(
        self,
        client: "AbstractClient",
        *args,
        request: MerossMessage | None = None,
        uuid: str | None = None,
    ):
        self.client = client
        self.request = request
        self.uuid = uuid
        super().__init__(*args)

    def _inspect_chaining(self):
        exc = self
        # we could likely recurse this but also likely it's no use
        # since we're not really reinspecting the same chain over and over
        # from different branches
        while cause := exc.__cause__:
            if not isinstance(cause, MerossTransportError):
                break
            exc = cause
        self.cause = cause
        self.inner_transport_error = exc

    @cached_property
    def cause(self) -> BaseException | None:
        self._inspect_chaining()
        return self.cause

    @cached_property
    def inner_transport_error(self) -> MerossTransportError:
        self._inspect_chaining()
        return self.inner_transport_error

    @cached_property
    def likely_remote_overflow(self):
        """This is an heuristic trying to detect if a transport error was originated
        because of a 'transient' buffer overflow in the device communication stack."""
        return isinstance(self.cause, aiohttp.ServerDisconnectedError) or isinstance(
            self.cause, TimeoutError
        )

    def log(self, level: int, message: str, /, **kwargs: "Unpack[LoggerArgs]"):
        inner = self.inner_transport_error
        if request := inner.request:
            self.client.log_exception(
                level,
                self.cause or inner,
                "%s (%s %s)",
                message,
                request.method,
                request.namespace,
                **kwargs,
            )
        else:
            self.client.log_exception(
                level,
                self.cause or inner,
                message,
                **kwargs,
            )

    def __str__(self):
        # recover original client/exception
        cause = self.cause
        inner = self.inner_transport_error
        error_message = (
            cause
            and (str(cause) or cause.__class__.__name__)
            or MerossError.__str__(inner)
        )
        if request := inner.request:
            return f"{error_message} ({request.method} {request.namespace})"
        else:
            return error_message


class MerossProtocolError(MerossError):
    """
    signal an application protocol error like:
    - missing header keys
    - application layer ERROR(s)

    - response is the full response payload
    - reason is an additional context error
    """

    def __init__(self, response, reason: object | None = None):
        self.response = response
        self.reason = reason
        super().__init__(reason)


class MerossKeyError(MerossProtocolError):
    """
    signal a protocol key error (wrong key)
    reported by device
    """

    def __init__(self, response: "MerossMessage"):
        super().__init__(response, "Invalid key")


class MerossSignatureError(MerossProtocolError):
    """
    signal a protocol signature error detected
    when validating the received header
    """

    def __init__(self, response: "MerossMessage"):
        super().__init__(response, "Signature error")
