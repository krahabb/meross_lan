"""
Meross protocol core types and helpers

"""

from binascii import a2b_base64, b2a_base64
from hashlib import md5
from typing import TYPE_CHECKING

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

if TYPE_CHECKING:
    from typing import Final, Unpack


def md5hexdigest(*args: "Unpack[tuple[str, ...]]"):
    """Returns the MD5 digest of the joined args as an hex string."""
    return md5("".join(args).encode(), usedforsecurity=False).hexdigest()


def compute_wifix_password(password: str, type: str, uuid: str, mac: str, /):
    return AESCipher(md5hexdigest(type, uuid, mac).encode()).encrypt_text(password)


class AESCipher(Cipher):

    if TYPE_CHECKING:
        IV: Final[bytes]

    IV = b"0000000000000000"

    def __init__(self, encryption_key: bytes):
        super().__init__(algorithms.AES(encryption_key), modes.CBC(self.IV))

    def encrypt_text(self, text: str):
        buffer = text.encode()
        buffer += bytes(16 - (len(buffer) % 16))
        encryptor = self.encryptor()
        return b2a_base64(encryptor.update(buffer) + encryptor.finalize()).decode()

    def decrypt(self, data: bytes | bytearray):
        decryptor = self.decryptor()
        return (decryptor.update(a2b_base64(data)) + decryptor.finalize()).rstrip(
            bytes(1)
        )

    def decrypt_text(self, text: str):
        return self.decrypt(text.encode()).decode()
