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

        class RequestRawArgs(AbstractClient.RequestRawArgs):
            pass

        class RequestArgs(AbstractClient.RequestArgs):
            pass

        SESSION_MAXIMUM_CONNECTIONS: ClassVar
        SESSION_MAXIMUM_CONNECTIONS_PER_HOST: ClassVar
        SESSION_TIMEOUT: ClassVar
        _SESSION: ClassVar[aiohttp.ClientSession | None]

        _cipher: Cipher | None

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
        "_url",
        "_session",
        "_terminate",
        "_terminate_guard",
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
        self._terminate = False
        self._terminate_guard = 0
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

    def _check_terminated(self):
        if self._terminate:
            raise TerminatedException

    @override
    async def async_connect(self, /, **kwargs):
        pass

    @override
    async def async_disconnect(self, /):
        self._terminate = True
        while self._terminate_guard:
            await asyncio.sleep(0.5)
        if self.is_connected:
            self.on_disconnect()
        self._terminate = False

    @override
    async def async_request_raw(
        self, request: MerossMessage, /, **kwargs: "Unpack[RequestRawArgs]"
    ):
        self._check_terminated()
        self._terminate_guard += 1
        try:
            self.on_tx(request)

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
            response = await self._session.post(
                url=self._url,
                data=data,
                headers=headers,
                timeout=aiohttp.ClientTimeout(
                    total=kwargs.get("timeout", self.timeout)
                ),
            )
            self._check_terminated()
            if response.status < 400:
                if not self.is_connected:
                    self.on_connect()
                return self.on_rx_raw(
                    (
                        _cipher.decript(await response.read())
                        if _cipher
                        else (await response.read())
                    )
                )
            if self.is_connected:
                self.on_disconnect()
            response.raise_for_status()
            # we should never get here since raise_for_status raises for 4xx and 5xx
            raise MerossTransportError(
                self, f"Unexpected response status {response.status}"
            )
        except asyncio.TimeoutError:
            if self.is_connected:
                self.on_disconnect()
            raise
        except Exception as e:
            self.log_exception(self.WARNING, e, "async_request_raw")
            raise
        finally:
            self._terminate_guard -= 1

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
                except TerminatedException:
                    raise
                except Exception:
                    # any error here is likely consequence of key-reply hack
                    # so we'll rethrow that (see #83 lacking invalid key message when configuring)
                    raise MerossKeyError(response)

        except KeyError:
            pass

        return response
