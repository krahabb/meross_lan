"""
Meross client library - exceptions
"""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from typing import Final, Unpack

    from .client import AbstractClient
    from .protocol.message import MerossMessage


class MerossError(Exception):
    """Base class for any exception raised by the library."""

    pass


class MerossTransportError(MerossError):
    """Signal a transport error like:
    - connection error
    - timeout
    - unexpected disconnection
    """

    def __init__(self, client: "AbstractClient", *args):
        self.client = client
        super().__init__(*args)


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
