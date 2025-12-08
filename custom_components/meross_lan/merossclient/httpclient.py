"""
Implementation for an async (aiohttp.ClientSession) http client
for Meross devices.
"""

import asyncio
import logging
import socket
import sys
from typing import TYPE_CHECKING, override

import aiohttp
from yarl import URL

from . import MEROSSDEBUG, _BaseClient
from .protocol import AESCipher, MerossKeyError, const as mc
from .protocol.message import MerossMessage, MerossResponse

if TYPE_CHECKING:
    from typing import ClassVar, NotRequired, Unpack

    from .protocol.types import MerossHeaderType, MerossRequestType


class TerminatedException(Exception):
    pass


class MerossHttpClient(_BaseClient):
    if TYPE_CHECKING:

        class Args(_BaseClient.Args):
            session: NotRequired[aiohttp.ClientSession]

        class RequestArgs(_BaseClient.RequestArgs):
            pass

        SESSION_MAXIMUM_CONNECTIONS: ClassVar
        SESSION_MAXIMUM_CONNECTIONS_PER_HOST: ClassVar
        SESSION_TIMEOUT: ClassVar
        _SESSION: ClassVar[aiohttp.ClientSession | None]

        _encryption_cipher: AESCipher | None
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

    __slots__ = _BaseClient.__SLOTS__ + (
        "_host",
        "_requesturl",
        "_session",
        "_terminate",
        "_terminate_guard",
        "_encryption_cipher",
        "_key_header",
    )

    def __init__(self, host: str, **kwargs: "Unpack[Args]"):
        """
        host: the ip or hostname of the device
        kwargs:
        key: pass in the (str) device key used for signing or None to attempt 'key-hack'
        session: the shared session to use or None to use the library dedicated one
        logger: a shared logger to enable logging
        timeout: total request timeout
        """
        self._host = host
        self._requesturl = URL(f"http://{host}/config")
        self._session = (
            kwargs.pop("session", MerossHttpClient._SESSION)
            or MerossHttpClient._get_or_create_client_session()
        )
        self._terminate = False
        self._terminate_guard = 0
        self._encryption_cipher = None
        self._key_header = {}  # type: ignore
        _BaseClient.__init__(self, **kwargs)

    @property
    def host(self):
        return self._host

    @host.setter
    def host(self, value: str):
        self._host = value
        self._requesturl = URL(f"http://{value}/config")

    def set_encryption(self, encryption_key: bytes | None, /):
        self._encryption_cipher = AESCipher(encryption_key) if encryption_key else None

    def enable_encryption(self, uuid: str, key: str, mac: str, /):
        self._encryption_cipher = AESCipher(
            MerossResponse.compute_encryption_key(uuid, key, mac)
        )

    def disable_encryption(self):
        self._encryption_cipher = None

    def _check_terminated(self):
        if self._terminate:
            raise TerminatedException

    async def async_terminate(self):
        """
        Marks the client as 'terminating' and awaits for any pending request to finish
        """
        self._terminate = True
        while self._terminate_guard:
            await asyncio.sleep(0.5)

    @override
    async def async_request_raw(
        self, request: "MerossMessage", /, **kwargs: "Unpack[RequestArgs]"
    ) -> MerossResponse:
        self._check_terminated()
        logger = self.logger
        logid = None
        self._terminate_guard += 1
        try:
            if logger and logger.isEnabledFor(self.LOG_DUMP):
                # we catch the 'request' id before json dumping so
                # to reasonably set the context before any exception
                logid = f"MerossHttpClient({self._host}:{id(request)})"
                logger.log(logging.DEBUG, "%s: HTTP Request (%s)", logid, request)
            else:
                logger = None
            if MEROSSDEBUG:
                MEROSSDEBUG.http_random_timeout()

            if _cipher := self._encryption_cipher:
                data = _cipher.encript_text(request.json)
                headers = {
                    aiohttp.hdrs.CONTENT_TYPE: "application/octet-stream",
                }
            else:
                # no encryption: session defaults to json
                data = request.json
                headers = {
                    aiohttp.hdrs.CONTENT_TYPE: "application/json",
                }
            # since device HTTP service sometimes timeouts with no apparent
            # reason we're using an increasing timeout loop to try recover
            # when this timeout is transient. This will lead to a total timeout
            # (for the caller) exceeding the value(s) actually set in self.timeout
            _timeout = kwargs.get("timeout", self.timeout)
            _connect_timeout = 1
            while True:
                try:
                    response = await self._session.post(
                        url=self._requesturl,
                        data=data,
                        headers=headers,
                        timeout=aiohttp.ClientTimeout(
                            total=_timeout, connect=_connect_timeout
                        ),
                    )
                    break
                except aiohttp.ServerTimeoutError:
                    self._check_terminated()
                    if _connect_timeout < _timeout:
                        _connect_timeout = _connect_timeout * 2
                    else:
                        raise

            self._check_terminated()
            response.raise_for_status()
            response = await response.text()
            if _cipher:
                response = _cipher.decript_text(response)

            if logger:
                logger.log(self.LOG_DUMP, "%s: HTTP Response (%s)", logid, response)
            self._check_terminated()
            return MerossResponse(response)
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
            raise
        finally:
            self._terminate_guard -= 1

    async def async_request(
        self, *args: "Unpack[MerossRequestType]", **kwargs: "Unpack[RequestArgs]"
    ) -> MerossResponse:
        key = self.key
        request = (
            MerossMessage.build_keyhack(*args, self._key_header)
            if key is None
            else MerossMessage.build(*args, key)
        )
        response = await self.async_request_raw(request, **kwargs)
        if (
            response.get(mc.KEY_PAYLOAD, {}).get(mc.KEY_ERROR, {}).get(mc.KEY_CODE)
            == mc.ERROR_INVALIDKEY
        ):
            if key is not None:
                raise MerossKeyError(response)
            # sign error... hack and fool
            if self.logger:
                self.logger.log(
                    logging.WARNING,
                    "MerossHttpClient(%s): Key error on %s %s -> retrying with key-reply hack",
                    self._host,
                    args[1],
                    args[0],
                )
            req_header = request[mc.KEY_HEADER]
            resp_header = response[mc.KEY_HEADER]
            req_header[mc.KEY_MESSAGEID] = resp_header[mc.KEY_MESSAGEID]
            req_header[mc.KEY_TIMESTAMP] = resp_header[mc.KEY_TIMESTAMP]
            req_header[mc.KEY_SIGN] = resp_header[mc.KEY_SIGN]
            delattr(request, "json")  # force re-compute of json
            try:
                response = await self.async_request_raw(request, **kwargs)
            except TerminatedException:
                raise
            except Exception:
                # any error here is likely consequence of key-reply hack
                # so we'll rethrow that (see #83 lacking invalid key message when configuring)
                raise MerossKeyError(response)

        if key is None:
            self._key_header = response[mc.KEY_HEADER]
        return response
