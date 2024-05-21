"""
    Implementation for an async (aiohttp.ClientSession) http client
    for Meross devices.
"""

import asyncio
import logging
import socket
import sys
import typing

import aiohttp
import attr
from yarl import URL

from . import (
    MEROSSDEBUG,
    KeyType,
    MerossKeyError,
    MerossPayloadType,
    MerossResponse,
    build_message,
    check_message_strict,
    const as mc,
    json_dumps,
)


class TerminatedException(Exception):
    pass


class MerossHttpClient:
    SESSION_MAXIMUM_CONNECTIONS: typing.ClassVar = 50
    SESSION_MAXIMUM_CONNECTIONS_PER_HOST: typing.ClassVar = 1
    SESSION_TIMEOUT: typing.ClassVar = aiohttp.ClientTimeout(total=10, connect=5)

    # Use an 'isolated' and dedicated client session to better manage
    # Meross http specifics following concern from @garysargentpersonal
    # about single device concurrency:
    # https://github.com/krahabb/meross_lan/issues/206#issuecomment-1999837054.
    # Setting SESSION_MAXIMUM_CONNECTIONS_PER_HOST == 1 should prevent
    # concurrent http sessions to the same device.
    _SESSION: typing.ClassVar[aiohttp.ClientSession | None] = None

    @staticmethod
    def _get_or_create_client_session():
        if not MerossHttpClient._SESSION:
            connector = aiohttp.TCPConnector(
                family=socket.AF_INET,
                limit=MerossHttpClient.SESSION_MAXIMUM_CONNECTIONS,
                limit_per_host=MerossHttpClient.SESSION_MAXIMUM_CONNECTIONS_PER_HOST,
                ssl=False,
            )
            MerossHttpClient._SESSION = aiohttp.ClientSession(
                connector=connector,
                headers={
                    aiohttp.hdrs.USER_AGENT: "MerossLan aiohttp/{0} Python/{1[0]}.{1[1]}".format(
                        aiohttp.__version__, sys.version_info
                    ),
                    aiohttp.hdrs.CONTENT_TYPE: "application/json",
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
        "replykey",
        "timeout",
        "_session",
        "_logger",
        "_logid",
        "_log_level_dump",
        "_terminate",
        "_terminate_guard",
    )

    def __init__(
        self,
        host: str,
        key: KeyType = None,
        session: aiohttp.ClientSession | None = None,
        logger: logging.Logger | None = None,
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
        self.key = key  # key == None for hack-mode
        self.replykey = None
        self.timeout = MerossHttpClient.SESSION_TIMEOUT
        self._session = session or MerossHttpClient._get_or_create_client_session()
        self._logger = logger
        self._logid = None
        self._log_level_dump = log_level_dump
        self._terminate = False
        self._terminate_guard = 0

    @property
    def host(self):
        return self._host

    @host.setter
    def host(self, value: str):
        self._host = value
        self._requesturl = URL(f"http://{value}/config")

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

    async def async_request_raw(self, request_json: str) -> MerossResponse:
        self._check_terminated()
        logger = self._logger
        logid = None
        self._terminate_guard += 1
        try:
            if logger and logger.isEnabledFor(self._log_level_dump):
                # we catch the 'request' id before json dumping so
                # to reasonably set the context before any exception
                logid = f"MerossHttpClient({self._host}:{id(request_json)})"
                logger.log(
                    self._log_level_dump, "%s: HTTP Request (%s)", logid, request_json
                )
            else:
                logger = None
            if MEROSSDEBUG:
                MEROSSDEBUG.http_random_timeout()

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
                        data=request_json,
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
            response_json = await response.text()
            if logger:
                logger.log(
                    self._log_level_dump, "%s: HTTP Response (%s)", logid, response_json
                )
            self._check_terminated()
            return MerossResponse(response_json)
        except TerminatedException as e:
            raise e
        except Exception as e:
            self.replykey = None  # reset the key hack since it could became stale
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
        self, namespace: str, method: str, payload: MerossPayloadType
    ) -> MerossResponse:
        key = self.key
        request = build_message(
            namespace,
            method,
            payload,
            self.replykey if key is None else key,
            mc.MANUFACTURER,
        )
        response = await self.async_request_raw(json_dumps(request))
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
                response = await self.async_request_raw(json_dumps(request))
            except TerminatedException as e:
                raise e
            except Exception:
                # any error here is likely consequence of key-reply hack
                # so we'll rethrow that (see #83 lacking invalid key message when configuring)
                raise MerossKeyError(response)

        if key is None:
            self.replykey = response[mc.KEY_HEADER]
        return response

    async def async_request_strict(
        self, namespace: str, method: str, payload: MerossPayloadType
    ):
        """
        check the protocol layer is correct and no protocol ERROR
        is being reported
        """
        return check_message_strict(
            await self.async_request(namespace, method, payload)
        )
