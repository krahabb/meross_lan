"""
A collection of typing definitions for

"""
import enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from message import MerossResponse


#
# Custom Exceptions
#
class MerossProtocolError(Exception):
    """
    signal a protocol error like:
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

    def __init__(self, response: "MerossResponse"):
        super().__init__(response, "Invalid key")


class MerossSignatureError(MerossProtocolError):
    """
    signal a protocol signature error detected
    when validating the received header
    """

    def __init__(self, response: "MerossResponse"):
        super().__init__(response, "Signature error")
