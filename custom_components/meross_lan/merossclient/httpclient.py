"""
    Implementation for an async (aiohttp.ClientSession) http client
    for Meross devices.
"""
from __future__ import annotations

import asyncio
from json import dumps as json_dumps, loads as json_loads
from logging import DEBUG
import typing

import aiohttp
import async_timeout
from yarl import URL

from . import (
    MEROSSDEBUG,
    KeyType,
    MerossKeyError,
    MerossMessageType,
    MerossPayloadType,
    MerossProtocolError,
    build_message,
    const as mc,
    get_replykey,
)

if typing.TYPE_CHECKING:
    from logging import Logger


class MerossHttpClient:
    timeout = 5  # total timeout will be 1+2+4: check relaxation algorithm

    __slots__ = (
        "_host",
        "_requesturl",
        "key",
        "replykey",
        "_session",
        "_logger",
        "_logid",
    )

    def __init__(
        self,
        host: str,
        key: KeyType = None,
        session: aiohttp.ClientSession | None = None,
        logger: Logger | None = None,
    ):
        """
        host: the ip of hostname of the device
        key: pass in the (str) device key used for signing or None to attempt 'key-hack'
        session: the shared session to use or None to create a dedicated one
        logger: a shared logger or None to log in its own Logger
        """
        self._host = host
        self._requesturl = URL(f"http://{host}/config")
        self.key = key  # key == None for hack-mode
        self.replykey = None
        self._session = session or aiohttp.ClientSession()
        self._logger = logger
        self._logid = None

    @property
    def host(self):
        return self._host

    @host.setter
    def host(self, value: str):
        self._host = value
        self._requesturl = URL(f"http://{value}/config")

    def set_logger(self, _logger: Logger):
        self._logger = _logger
        self._logid = None

    async def async_request_raw(self, request: MerossMessageType) -> MerossMessageType:
        timeout = 1
        try:
            self._logid = None
            if self._logger and self._logger.isEnabledFor(DEBUG):
                # we catch the 'request' id before json dumping so
                # to reasonably set the context before any exception
                self._logid = f"MerossHttpClient({self._host}:{id(request)})"
                request_data = json_dumps(request)
                self._logger.debug("%s: HTTP Request (%s)", self._logid, request_data)
            else:
                request_data = json_dumps(request)
            # since device HTTP service sometimes timeouts with no apparent
            # reason we're using an increasing timeout loop to try recover
            # when this timeout is transient
            while True:
                try:
                    async with async_timeout.timeout(timeout):
                        if MEROSSDEBUG:
                            MEROSSDEBUG.http_random_timeout()
                        response = await self._session.post(
                            url=self._requesturl, data=request_data
                        )
                    break
                except asyncio.TimeoutError as e:
                    if timeout < self.timeout:
                        timeout = timeout * 2
                    else:
                        raise e

            response.raise_for_status()
            text_body = await response.text()
            if self._logid:
                self._logger.debug("%s: HTTP Response (%s)", self._logid, text_body)  # type: ignore
            json_body: MerossMessageType = json_loads(text_body)
            if self.key is None:
                self.replykey = get_replykey(json_body[mc.KEY_HEADER], self.key)
        except Exception as e:
            self.replykey = None  # reset the key hack since it could became stale
            if self._logid:
                self._logger.debug(  # type: ignore
                    "%s: HTTP %s (%s)",
                    self._logid,
                    type(e).__name__,
                    str(e),
                )
            raise e

        return json_body

    async def async_request(
        self, namespace: str, method: str, payload: MerossPayloadType
    ) -> MerossMessageType:
        key = self.key
        request = build_message(
            namespace,
            method,
            payload,
            self.replykey if key is None else key,
            mc.MANUFACTURER,
        )
        response = await self.async_request_raw(request)
        if (
            response.get(mc.KEY_PAYLOAD, {}).get(mc.KEY_ERROR, {}).get(mc.KEY_CODE)
            == mc.ERROR_INVALIDKEY
        ):
            if key is not None:
                raise MerossKeyError(response)
            # sign error... hack and fool
            if self._logid:
                self._logger.debug(  # type: ignore
                    "%s: Key error on (%s:%s) -> retrying with key-reply hack",
                    self._logid,
                    method,
                    namespace,
                )
            req_header = request[mc.KEY_HEADER]
            resp_header = response[mc.KEY_HEADER]
            req_header[mc.KEY_MESSAGEID] = resp_header[mc.KEY_MESSAGEID]
            req_header[mc.KEY_TIMESTAMP] = resp_header[mc.KEY_TIMESTAMP]
            req_header[mc.KEY_SIGN] = resp_header[mc.KEY_SIGN]
            try:
                response = await self.async_request_raw(request)
            except Exception:
                # any error here is likely consequence of key-reply hack
                # so we'll rethrow that (see #83 lacking invalid key message when configuring)
                raise MerossKeyError(response)

        return response

    async def async_request_strict(
        self, namespace: str, method: str, payload: MerossPayloadType
    ) -> MerossMessageType:
        """
        check the protocol layer is correct and no protocol ERROR
        is being reported
        """
        response = await self.async_request(namespace, method, payload)
        try:
            r_header = response[mc.KEY_HEADER]
            r_header[mc.KEY_NAMESPACE]
            r_method = r_header[mc.KEY_METHOD]
            r_payload = response[mc.KEY_PAYLOAD]
        except Exception as e:
            raise MerossProtocolError(response, str(e)) from e

        if r_method == mc.METHOD_ERROR:
            p_error = r_payload.get(mc.KEY_ERROR, {})
            if p_error.get(mc.KEY_CODE) == mc.ERROR_INVALIDKEY:
                raise MerossKeyError(response)
            else:
                raise MerossProtocolError(response, p_error)

        return response
