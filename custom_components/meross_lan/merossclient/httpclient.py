"""
Implementation for an async (aiohttp.ClientSession) http client
for Meross devices.
"""

import asyncio
from base64 import b64decode, b64encode
import logging
import socket
import sys
from typing import TYPE_CHECKING
from uuid import uuid4

import aiohttp
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from yarl import URL

from . import JSON_ENCODER, MEROSSDEBUG
from .protocol import MerossKeyError, const as mc
from .protocol.message import (
    MerossResponse,
    build_message,
    build_message_keyhack,
)

if TYPE_CHECKING:
    from typing import ClassVar, Protocol

    from protocol.types import MerossHeaderType, MerossPayloadType

    class LoggerT(Protocol):
        def isEnabledFor(self, level: int) -> bool: ...
        def log(self, level: int, msg: str, *args, **kwargs) -> None: ...


class TerminatedException(Exception):
    pass


class MerossHttpClient:
    if TYPE_CHECKING:
        SESSION_MAXIMUM_CONNECTIONS: ClassVar
        SESSION_MAXIMUM_CONNECTIONS_PER_HOST: ClassVar
        SESSION_TIMEOUT: ClassVar
        _SESSION: ClassVar[aiohttp.ClientSession | None]

        _encryption_cipher: Cipher | None
        _key_header: MerossHeaderType

    SESSION_MAXIMUM_CONNECTIONS = 50
    SESSION_MAXIMUM_CONNECTIONS_PER_HOST = 1
    SESSION_TIMEOUT = aiohttp.ClientTimeout(total=10, connect=5)

    # Use an 'isolated' and dedicated client session to better manage
    # Meross http specifics following concern from @garysargentpersonal
    # about single device concurrency:
    # https://github.com/krahabb/meross_lan/issues/206#issuecomment-1999837054.
    # Setting SESSION_MAXIMUM_CONNECTIONS_PER_HOST == 1 should prevent
    # concurrent http sessions to the same device.
    _SESSION = None

    @staticmethod
    def _get_or_create_client_session():
        if not MerossHttpClient._SESSION:
            MerossHttpClient._SESSION = aiohttp.ClientSession(
                connector=aiohttp.TCPConnector(
                    family=socket.AF_INET,
                    limit=MerossHttpClient.SESSION_MAXIMUM_CONNECTIONS,
                    limit_per_host=MerossHttpClient.SESSION_MAXIMUM_CONNECTIONS_PER_HOST,
                    ssl=False,
                ),
                headers={
                    aiohttp.hdrs.USER_AGENT: "MerossLan aiohttp/{0} Python/{1[0]}.{1[1]}".format(
                        aiohttp.__version__, sys.version_info
                    ),
                },
                timeout=MerossHttpClient.SESSION_TIMEOUT,
            )
        return MerossHttpClient._SESSION

    @staticmethod
    async def async_shutdown_session():
        if MerossHttpClient._SESSION:
            await MerossHttpClient._SESSION.close()
            MerossHttpClient._SESSION = None

    __slots__ = (
        "_host",
        "_requesturl",
        "key",
        "timeout",
        "_session",
        "_logger",
        "_logid",
        "_log_level_dump",
        "_terminate",
        "_terminate_guard",
        "_encryption_cipher",
        "_key_header",
    )

    def __init__(
        self,
        host: str,
        key: str | None = None,
        *,
        session: aiohttp.ClientSession | None = None,
        logger: "LoggerT | None" = None,
        log_level_dump: int = logging.NOTSET,
    ):
        """
        host: the ip or hostname of the device
        key: pass in the (str) device key used for signing or None to attempt 'key-hack'
        session: the shared session to use or None to use the library dedicated one
        logger: a shared logger to enable logging
        log_level_dump: the logging level at which the full json payloads will be dumped (costly)
        """
        self._host = host
        self._requesturl = URL(f"http://{host}/config")
        self.key = key
        self.timeout = MerossHttpClient.SESSION_TIMEOUT
        self._session = session or MerossHttpClient._get_or_create_client_session()
        self._logger = logger
        self._logid = None
        self._log_level_dump = log_level_dump
        self._terminate = False
        self._terminate_guard = 0
        self._encryption_cipher = None
        self._key_header = {}  # type: ignore

    @property
    def host(self):
        return self._host

    @host.setter
    def host(self, value: str):
        self._host = value
        self._requesturl = URL(f"http://{value}/config")

    def set_encryption(self, encryption_key: bytes | None, /):
        if encryption_key:
            self._encryption_cipher = Cipher(
                algorithms.AES(encryption_key),
                modes.CBC("0000000000000000".encode("utf8")),
            )
        else:
            self._encryption_cipher = None

    def _check_terminated(self):
        if self._terminate:
            raise TerminatedException

    def terminate(self):
        """
        Marks the client as 'terminating' so that any pending request will abort
        and raise TerminateException. The client need to be rebuilt after this.
        """
        self._terminate = True

    async def async_terminate(self):
        """
        Marks the client as 'terminating' and awaits for any pending request to finish
        """
        self._terminate = True
        while self._terminate_guard:
            await asyncio.sleep(0.5)

    async def async_request_raw(self, request: str, /) -> MerossResponse:
        self._check_terminated()
        logger = self._logger
        logid = None
        self._terminate_guard += 1
        try:
            if logger and logger.isEnabledFor(self._log_level_dump):
                # we catch the 'request' id before json dumping so
                # to reasonably set the context before any exception
                logid = f"MerossHttpClient({self._host}:{id(request)})"
                logger.log(
                    self._log_level_dump, "%s: HTTP Request (%s)", logid, request
                )
            else:
                logger = None
            if MEROSSDEBUG:
                MEROSSDEBUG.http_random_timeout()

            if _cipher := self._encryption_cipher:
                request_bytes = request.encode("utf-8")
                request_bytes += bytes(16 - (len(request_bytes) % 16))
                encryptor = _cipher.encryptor()
                request = b64encode(
                    encryptor.update(request_bytes) + encryptor.finalize()
                ).decode("utf-8")
                headers = {
                    aiohttp.hdrs.CONTENT_TYPE: "application/octet-stream",
                }
            else:
                # no encryption: session defaults to json
                headers = {
                    aiohttp.hdrs.CONTENT_TYPE: "application/json",
                }
            # since device HTTP service sometimes timeouts with no apparent
            # reason we're using an increasing timeout loop to try recover
            # when this timeout is transient. This will lead to a total timeout
            # (for the caller) exceeding the value(s) actually set in self.timeout
            _connect_timeout_max = self.timeout.connect or self.timeout.total or 5
            _connect_timeout = 1
            while True:
                try:
                    response = await self._session.post(
                        url=self._requesturl,
                        data=request,
                        headers=headers,
                        timeout=aiohttp.ClientTimeout(
                            total=self.timeout.total, connect=_connect_timeout
                        ),
                    )
                    break
                except aiohttp.ServerTimeoutError as exception:
                    self._check_terminated()
                    if _connect_timeout < _connect_timeout_max:
                        _connect_timeout = _connect_timeout * 2
                    else:
                        raise exception

            self._check_terminated()
            response.raise_for_status()
            response = await response.text()
            if _cipher:
                decryptor = _cipher.decryptor()
                decrypted_bytes = decryptor.update(b64decode(response))
                decrypted_bytes += decryptor.finalize()
                response = decrypted_bytes.decode("utf8").rstrip("\0")

            if logger:
                logger.log(
                    self._log_level_dump, "%s: HTTP Response (%s)", logid, response
                )
            self._check_terminated()
            return MerossResponse(response)
        except TerminatedException as e:
            raise e
        except Exception as e:
            self._key_header = {}  # type: ignore
            if logger:
                logger.log(  # type: ignore
                    logging.DEBUG,
                    "%s: HTTP %s (%s)",
                    logid,
                    type(e).__name__,
                    str(e),
                )
            raise e
        finally:
            self._terminate_guard -= 1

    async def async_request(
        self, namespace: str, method: str, payload: "MerossPayloadType", /
    ) -> MerossResponse:
        key = self.key
        request = (
            build_message_keyhack(
                namespace,
                method,
                payload,
                self._key_header,
            )
            if key is None
            else build_message(
                namespace,
                method,
                payload,
                uuid4().hex,
                key,
            )
        )
        response = await self.async_request_raw(JSON_ENCODER.encode(request))
        if (
            response.get(mc.KEY_PAYLOAD, {}).get(mc.KEY_ERROR, {}).get(mc.KEY_CODE)
            == mc.ERROR_INVALIDKEY
        ):
            if key is not None:
                raise MerossKeyError(response)
            # sign error... hack and fool
            if self._logger:
                self._logger.log(
                    logging.WARNING,
                    "MerossHttpClient(%s): Key error on %s %s -> retrying with key-reply hack",
                    self._host,
                    method,
                    namespace,
                )
            req_header = request[mc.KEY_HEADER]
            resp_header = response[mc.KEY_HEADER]
            req_header[mc.KEY_MESSAGEID] = resp_header[mc.KEY_MESSAGEID]
            req_header[mc.KEY_TIMESTAMP] = resp_header[mc.KEY_TIMESTAMP]
            req_header[mc.KEY_SIGN] = resp_header[mc.KEY_SIGN]
            try:
                response = await self.async_request_raw(JSON_ENCODER.encode(request))
            except TerminatedException as e:
                raise e
            except Exception:
                # any error here is likely consequence of key-reply hack
                # so we'll rethrow that (see #83 lacking invalid key message when configuring)
                raise MerossKeyError(response)

        if key is None:
            self._key_header = response[mc.KEY_HEADER]
        return response
