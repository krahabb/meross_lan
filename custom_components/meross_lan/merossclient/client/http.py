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
from .. import MEROSSDEBUG
from ..exceptions import MerossKeyError, MerossTransportError
from ..protocol import AESCipher, const as mc, md5hexdigest
from ..protocol.message import MerossMessage

if TYPE_CHECKING:
    from typing import ClassVar, NotRequired, Unpack

    from ..logging import LoggerType
    from ..protocol.types import MerossHeaderType, MerossRequestType


class HttpClient(AbstractClient):

    class Cipher(AESCipher):
        def __init__(self, uuid: str, key: str, mac: str, /):
            AESCipher.__init__(
                self, md5hexdigest(uuid[3:22], key[1:9], mac, key[10:28]).encode()
            )

    if TYPE_CHECKING:

        class Args(AbstractClient.Args):
            session: NotRequired[aiohttp.ClientSession]

        class RequestRawArgs(AbstractClient.RequestRawArgs):
            pass

        class RequestArgs(AbstractClient.RequestArgs):
            pass

        SESSION_MAXIMUM_CONNECTIONS: ClassVar
        SESSION_MAXIMUM_CONNECTIONS_PER_HOST: ClassVar
        SESSION_TIMEOUT: ClassVar
        _SESSION: ClassVar[aiohttp.ClientSession | None]

        _requests: set[asyncio.Task]
        _cipher: Cipher | None

    TRANSPORT = AbstractClient.Transport.HTTP  # type: ignore[override]

    SESSION_MAXIMUM_CONNECTIONS = 50
    SESSION_MAXIMUM_CONNECTIONS_PER_HOST = 1
    SESSION_TIMEOUT = aiohttp.ClientTimeout(total=10, connect=5)

    _HEADERS_PLAINTEXT = {aiohttp.hdrs.CONTENT_TYPE: "application/json"}
    _HEADERS_ENCRYPTED = {aiohttp.hdrs.CONTENT_TYPE: "application/octet-stream"}

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
        "_url",
        "_session",
        "_requests",
        "_cipher",
    )

    def __init__(
        self, host: str, parent: "LoggerType | None" = None, /, **kwargs: "Unpack[Args]"
    ):
        """
        host: the ip or hostname of the device
        kwargs:
        key: pass in the (str) device key used for signing or None to attempt 'key-hack'
        session: the shared session to use or None to use the library dedicated one
        timeout: total request timeout
        """
        self._url = URL(f"http://{host}/config")
        self._session = (
            kwargs.pop("session", HttpClient._SESSION)
            or HttpClient._get_or_create_client_session()
        )
        self._requests = set()
        self._cipher = None
        super().__init__(host, parent, **kwargs)

    @property
    @override
    def host(self):
        return self._url.host

    @host.setter
    def host(self, value: str):
        self._url = URL(f"http://{value}/config")
        self.id = value  # type: ignore (BOOM)

    @property
    def url(self):
        return self._url

    @url.setter
    def url(self, value: str | URL):
        self._url = URL(value)
        self.id = self._url.host  # type: ignore (BOOM)

    def enable_encryption(self, uuid: str, key: str, mac: str, /):
        self._cipher = HttpClient.Cipher(uuid, key, mac)

    def disable_encryption(self):
        self._cipher = None

    @override
    async def async_connect(self, /, **kwargs):
        pass

    @override
    async def async_disconnect(self, /):
        for request in tuple(self._requests):
            request.cancel()
        if self.is_connected:
            self.on_disconnect()

    async def _perform_request(
        self,
        request: MerossMessage,
        timeout: aiohttp.ClientTimeout,
        /,
    ):
        """Internal wrapper to perform the request."""
        if _cipher := self._cipher:
            data = _cipher.encript_text(request.json)
            headers = HttpClient._HEADERS_ENCRYPTED
        else:
            # no encryption: session defaults to json
            data = request.json
            headers = HttpClient._HEADERS_PLAINTEXT
        response = await self._session.post(
            url=self._url,
            data=data,
            headers=headers,
            timeout=timeout,
        )
        return (
            _cipher.decript(await response.read())
            if _cipher
            else (await response.read())
        )

    @override
    async def async_request_raw(
        self, request: MerossMessage, /, **kwargs: "Unpack[RequestRawArgs]"
    ):
        self.on_tx(request)
        try:
            if MEROSSDEBUG:
                MEROSSDEBUG.http_random_timeout()
            request_task = self.create_task(
                self._perform_request(
                    request,
                    aiohttp.ClientTimeout(total=kwargs.get("timeout", self.timeout)),
                )
            )
            requests = self._requests
            requests.add(request_task)
            request_task.add_done_callback(lambda task: requests.remove(request_task))
            response = await request_task
            if not self.is_connected:
                self.on_connect()
            return self.on_rx_raw(response)

        except (asyncio.TimeoutError, asyncio.CancelledError):
            if self.is_connected:
                self.on_disconnect()
            raise
        except Exception as e:
            self.log_exception(self.WARNING, e, "async_request_raw")
            raise

    @override
    async def async_request(
        self, *args: "Unpack[MerossRequestType]", **kwargs: "Unpack[RequestArgs]"
    ):
        key = self.key
        request = (
            MerossMessage.build_keyhack(*args, self.last_rx_message.header)
            if key is None and self.last_rx_message
            else MerossMessage.build(*args, key)
        )
        response = await self.async_request_raw(request, **kwargs)
        try:
            if response.payload[mc.KEY_ERROR][mc.KEY_CODE] == mc.ERROR_INVALIDKEY:
                if key is not None:
                    raise MerossKeyError(response)
                # sign error... hack and fool
                self.log(
                    self.WARNING,
                    "Key error on %s %s -> retrying with key-reply hack",
                    args[1],
                    args[0],
                )
                req_header = request.header
                resp_header = response.header
                req_header[mc.KEY_MESSAGEID] = resp_header[mc.KEY_MESSAGEID]
                req_header[mc.KEY_TIMESTAMP] = resp_header[mc.KEY_TIMESTAMP]
                req_header[mc.KEY_SIGN] = resp_header[mc.KEY_SIGN]
                del request.json  # force re-compute of json
                try:
                    response = await self.async_request_raw(request, **kwargs)
                except Exception:
                    # any error here is likely consequence of key-reply hack
                    # so we'll rethrow that (see #83 lacking invalid key message when configuring)
                    raise MerossKeyError(response)

        except KeyError:
            pass

        return response
