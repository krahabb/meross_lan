"""
Meross protocol core types and helpers

"""

from binascii import a2b_base64, b2a_base64
from hashlib import md5
from typing import TYPE_CHECKING

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

if TYPE_CHECKING:
    from typing import Final, Unpack

    from message import MerossMessage


#
# Custom Exceptions
#
class MerossError(Exception):
    """Base class for any exception raised by the library."""

    pass


class MerossTransportError(MerossError):
    """Signal a transport error like:
    - connection error
    - timeout
    - unexpected disconnection
    """

    pass


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


def md5hexdigest(*args: "Unpack[tuple[str, ...]]"):
    """Returns the MD5 digest of the joined args as an hex string."""
    return md5("".join(args).encode(), usedforsecurity=False).hexdigest()


def compute_wifix_password(password: str, type: str, uuid: str, mac: str, /):
    return AESCipher(md5hexdigest(type, uuid, mac).encode()).encript_text(password)


class AESCipher(Cipher):

    if TYPE_CHECKING:
        IV: Final[bytes]

    IV = "0000000000000000".encode()

    def __init__(self, encryption_key: bytes):
        super().__init__(algorithms.AES(encryption_key), modes.CBC(self.IV))

    def encript_text(self, text: str):
        buffer = text.encode()
        buffer += bytes(16 - (len(buffer) % 16))
        encryptor = self.encryptor()
        return b2a_base64(encryptor.update(buffer) + encryptor.finalize()).decode()

    def decript(self, data: bytes | bytearray):
        decryptor = self.decryptor()
        return (decryptor.update(a2b_base64(data)) + decryptor.finalize()).rstrip(
            bytes(1)
        )

    def decript_text(self, text: str):
        return self.decript(text.encode()).decode()
