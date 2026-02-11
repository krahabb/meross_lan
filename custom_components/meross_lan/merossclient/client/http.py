"""
Implementation for an async (aiohttp.ClientSession) http client
for Meross devices.
"""

import asyncio
import socket
import sys
from typing import TYPE_CHECKING, override

import aiohttp
from yarl import URL

from . import AbstractClient
from .. import MEROSSDEBUG, logging
from ..protocol import AESCipher, MerossKeyError, const as mc, md5hexdigest
from ..protocol.message import MerossMessage, MerossResponse

if TYPE_CHECKING:
    from typing import ClassVar, NotRequired, Unpack

    from ..logging import LoggerType
    from ..protocol.types import MerossHeaderType, MerossRequestType


class TerminatedException(Exception):
    pass


class HttpClient(AbstractClient):

    class Cipher(AESCipher):
        def __init__(self, uuid: str, key: str, mac: str, /):
            AESCipher.__init__(
                self, md5hexdigest(uuid[3:22], key[1:9], mac, key[10:28]).encode()
            )

    if TYPE_CHECKING:

        class Args(AbstractClient.Args):
            session: NotRequired[aiohttp.ClientSession]

        class RequestArgs(AbstractClient.RequestArgs):
            pass

        SESSION_MAXIMUM_CONNECTIONS: ClassVar
        SESSION_MAXIMUM_CONNECTIONS_PER_HOST: ClassVar
        SESSION_TIMEOUT: ClassVar
        _SESSION: ClassVar[aiohttp.ClientSession | None]

        _cipher: Cipher | None
        _key_header: MerossHeaderType

    TRANSPORT = AbstractClient.Transport.HTTP  # type: ignore[override]

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
        if not HttpClient._SESSION:
            HttpClient._SESSION = aiohttp.ClientSession(
                connector=aiohttp.TCPConnector(
                    family=socket.AF_INET,
                    limit=HttpClient.SESSION_MAXIMUM_CONNECTIONS,
                    limit_per_host=HttpClient.SESSION_MAXIMUM_CONNECTIONS_PER_HOST,
                    ssl=False,
                ),
                headers={
                    aiohttp.hdrs.USER_AGENT: "MerossLan aiohttp/{0} Python/{1[0]}.{1[1]}".format(
                        aiohttp.__version__, sys.version_info
                    ),
                },
                timeout=HttpClient.SESSION_TIMEOUT,
            )
        return HttpClient._SESSION

    @staticmethod
    async def async_shutdown_session():
        if HttpClient._SESSION:
            await HttpClient._SESSION.close()
            HttpClient._SESSION = None

    __slots__ = AbstractClient._calc_slots(
        "_host",
        "_requesturl",
        "_session",
        "_terminate",
        "_terminate_guard",
        "_cipher",
        "_key_header",
    )

    def __init__(
        self, host: str, parent: "LoggerType | None" = None, /, **kwargs: "Unpack[Args]"
    ):
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
            kwargs.pop("session", HttpClient._SESSION)
            or HttpClient._get_or_create_client_session()
        )
        self._terminate = False
        self._terminate_guard = 0
        self._cipher = None
        self._key_header = {}  # type: ignore
        super().__init__(host, parent, **kwargs)

    @property
    def host(self):
        return self._host

    @host.setter
    def host(self, value: str):
        self._host = value
        self._requesturl = URL(f"http://{value}/config")

    def enable_encryption(self, uuid: str, key: str, mac: str, /):
        self._cipher = HttpClient.Cipher(uuid, key, mac)

    def disable_encryption(self):
        self._cipher = None

    def _check_terminated(self):
        if self._terminate:
            raise TerminatedException

    @override
    async def async_shutdown(self):
        await super().async_shutdown()
        self._terminate = True
        while self._terminate_guard:
            await asyncio.sleep(0.5)

    @override
    async def async_request_raw(
        self, request: "MerossMessage", /, **kwargs: "Unpack[RequestArgs]"
    ) -> MerossResponse:
        self._check_terminated()
        self._terminate_guard += 1
        try:
            if self.isEnabledFor(logging.VERBOSE):
                # we catch the 'request' id before json dumping so
                # to reasonably set the context before any exception
                logid = f"{self.__class__.__name__}({self._host}:{id(request)})"
                logger = self.parent
                logger.log(logging.DEBUG, "%s: HTTP Request (%s)", logid, request)
            else:
                logid = logger = None

            if MEROSSDEBUG:
                MEROSSDEBUG.http_random_timeout()

            if _cipher := self._cipher:
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
                logger.log(logging.VERBOSE, "%s: HTTP Response (%s)", logid, response)
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

    @override
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
        try:
            if response.payload[mc.KEY_ERROR][mc.KEY_CODE] == mc.ERROR_INVALIDKEY:
                if key is not None:
                    raise MerossKeyError(response)
                # sign error... hack and fool
                self.log(
                    logging.WARNING,
                    "Key error on %s %s -> retrying with key-reply hack",
                    args[1],
                    args[0],
                )
                req_header = request.header
                resp_header = response.header
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

        except KeyError:
            pass

        if key is None:
            self._key_header = response.header
        return response
