"""
    Implementation for an async (aiohttp.ClientSession) http client
    for Meross devices.
"""
from __future__ import annotations

import asyncio
import logging
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
    MerossResponse,
    build_message,
    check_message_strict,
    const as mc,
    json_dumps,
)

if typing.TYPE_CHECKING:
    from . import MerossMessage


class TerminatedException(Exception):
    pass


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
        host: the ip of hostname of the device
        key: pass in the (str) device key used for signing or None to attempt 'key-hack'
        session: the shared session to use or None to create a dedicated one
        logger: a shared logger to enable logging
        log_level_dump: the logging level at which the full json payloads will be dumped (costly)
        """
        self._host = host
        self._requesturl = URL(f"http://{host}/config")
        self.key = key  # key == None for hack-mode
        self.replykey = None
        self._session = session or aiohttp.ClientSession()
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

    async def async_request_message(self, request: MerossMessage) -> MerossResponse:
        self._check_terminated()
        logger = self._logger
        logid = None
        self._terminate_guard += 1
        try:
            timeout = 1
            if logger and logger.isEnabledFor(self._log_level_dump):
                # we catch the 'request' id before json dumping so
                # to reasonably set the context before any exception
                logid = f"MerossHttpClient({self._host}:{id(request)})"
                request_json = request.json()
                logger.log(
                    self._log_level_dump, "%s: HTTP Request (%s)", logid, request_json
                )
            else:
                logger = None
                request_json = request.json()
            # since device HTTP service sometimes timeouts with no apparent
            # reason we're using an increasing timeout loop to try recover
            # when this timeout is transient
            while True:
                try:
                    async with async_timeout.timeout(timeout):
                        if MEROSSDEBUG:
                            MEROSSDEBUG.http_random_timeout()
                        response = await self._session.post(
                            url=self._requesturl, data=request_json
                        )
                    break
                except asyncio.TimeoutError as e:
                    self._check_terminated()
                    if timeout < self.timeout:
                        timeout = timeout * 2
                    else:
                        raise e

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

    async def async_request_raw(
        self, request: MerossMessageType | dict
    ) -> MerossResponse:
        self._check_terminated()
        logger = self._logger
        logid = None
        self._terminate_guard += 1
        try:
            timeout = 1
            if logger and logger.isEnabledFor(self._log_level_dump):
                # we catch the 'request' id before json dumping so
                # to reasonably set the context before any exception
                logid = f"MerossHttpClient({self._host}:{id(request)})"
                request_data = json_dumps(request)
                logger.log(
                    self._log_level_dump, "%s: HTTP Request (%s)", logid, request_data
                )
            else:
                logger = None
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
                    self._check_terminated()
                    if timeout < self.timeout:
                        timeout = timeout * 2
                    else:
                        raise e

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
        response = await self.async_request_raw(request)
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
                response = await self.async_request_raw(request)
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
