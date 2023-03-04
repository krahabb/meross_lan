"""
    Implementation for an async (aiohttp.ClientSession) http client
    for Meross devices.
"""
from __future__ import annotations
from logging import Logger, getLogger, DEBUG
from json import (
    dumps as json_dumps,
    loads as json_loads,
)
import asyncio
import async_timeout
import aiohttp
from yarl import URL

from . import (
    const as mc,
    KeyType,
    MerossKeyError,
    MerossProtocolError,
    build_payload,
    get_replykey,
    MEROSSDEBUG,
)


class MerossHttpClient:

    timeout = 5  # total timeout will be 1+2+4: check relaxation algorithm

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
        self._logger = logger or getLogger(__name__)

    @property
    def host(self):
        return self._host

    @host.setter
    def host(self, value: str):
        self._host = value
        self._requesturl = URL(f"http://{value}/config")

    async def async_request_raw(self, request: dict) -> dict:
        timeout = 1
        debugid = None
        try:
            if self._logger.isEnabledFor(DEBUG):
                debugid = f"{self._host}:{id(request)}"
                request_data = json_dumps(request)
                self._logger.debug(
                    "MerossHttpClient(%s): HTTP Request (%s)", debugid, request_data
                )
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
            if debugid is not None:
                self._logger.debug(
                    "MerossHttpClient(%s): HTTP Response (%s)", debugid, text_body
                )
            json_body: dict = json_loads(text_body)
            if self.key is None:
                self.replykey = get_replykey(json_body[mc.KEY_HEADER], self.key)
        except Exception as e:
            self.replykey = None  # reset the key hack since it could became stale
            if debugid is not None:
                self._logger.debug(
                    "MerossHttpClient(%s): HTTP %s (%s)",
                    debugid,
                    type(e).__name__,
                    str(e),
                )
            raise e

        return json_body

    async def async_request(self, namespace: str, method: str, payload: dict) -> dict:
        key = self.key
        request: dict = build_payload(
            namespace,
            method,
            payload,
            self.replykey if key is None else key,
            mc.MANUFACTURER,
        )
        response: dict = await self.async_request_raw(request)
        if (
            response.get(mc.KEY_PAYLOAD, {}).get(mc.KEY_ERROR, {}).get(mc.KEY_CODE)
            == mc.ERROR_INVALIDKEY
        ):
            if key is not None:
                raise MerossKeyError(response.get(mc.KEY_PAYLOAD))
            # sign error... hack and fool
            if self._logger.isEnabledFor(DEBUG):
                self._logger.debug(
                    "Key error on %s (%s:%s) -> retrying with key-reply hack",
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
                response = await self.async_request_raw(request)
            except Exception:
                # any error here is likely consequence of key-reply hack
                # so we'll rethrow that (see #83 lacking invalid key message when configuring)
                raise MerossKeyError(response.get(mc.KEY_PAYLOAD))

        return response

    async def async_request_strict(
        self, namespace: str, method: str, payload: dict
    ) -> dict:
        """
        check the protocol layer is correct and no protocol ERROR
        is being reported
        """
        response = await self.async_request(namespace, method, payload)
        try:
            r_header: dict = response[mc.KEY_HEADER]
            r_namespace: str = r_header[mc.KEY_NAMESPACE]
            r_method: str = r_header[mc.KEY_METHOD]
            r_payload: dict = response[mc.KEY_PAYLOAD]
        except Exception as e:
            raise MerossProtocolError(e) from e

        if r_method == mc.METHOD_ERROR:
            if r_payload.get(mc.KEY_ERROR, {}).get(mc.KEY_CODE) == mc.ERROR_INVALIDKEY:
                raise MerossKeyError(r_payload)
            else:
                raise MerossProtocolError(r_payload)

        return response
