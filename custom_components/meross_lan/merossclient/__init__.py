"""An Http API Client to interact with meross devices"""
from email import header
import logging
from types import MappingProxyType
from typing import List, MappingView, Optional, Dict, Any, Callable, Union
from enum import Enum
from uuid import uuid4
from hashlib import md5
from time import time
from json import (
    dumps as json_dumps,
    loads as json_loads,
)

import aiohttp
from yarl import URL
import socket
import asyncio
import async_timeout

from . import const as mc

KeyType = Union[dict, Optional[str]] # pylint: disable=unsubscriptable-object


def build_payload(namespace:str, method:str, payload:dict = {}, key:KeyType = None, _from:str = None)-> dict:
    if isinstance(key, dict):
        key[mc.KEY_NAMESPACE] = namespace
        key[mc.KEY_METHOD] = method
        key[mc.KEY_PAYLOADVERSION] = 1
        key[mc.KEY_FROM] = _from
        return {
            mc.KEY_HEADER: key,
            mc.KEY_PAYLOAD: payload
        }
    else:
        messageid = uuid4().hex
        timestamp = int(time())
        return {
            mc.KEY_HEADER: {
                mc.KEY_MESSAGEID: messageid,
                mc.KEY_NAMESPACE: namespace,
                mc.KEY_METHOD: method,
                mc.KEY_PAYLOADVERSION: 1,
                mc.KEY_FROM: _from,
                #mc.KEY_FROM: "/app/0-0/subscribe",
                #"from": "/appliance/9109182170548290882048e1e9522946/publish",
                mc.KEY_TIMESTAMP: timestamp,
                mc.KEY_TIMESTAMPMS: 0,
                mc.KEY_SIGN: md5((messageid + (key or "") + str(timestamp)).encode('utf-8')).hexdigest()
            },
            mc.KEY_PAYLOAD: payload
        }



def get_replykey(header: dict, key:KeyType = None) -> KeyType:
    """
    checks header signature against key:
    if ok return sign itsef else return the full header { "messageId", "timestamp", "sign", ...}
    in order to be able to use it in a reply scheme
    **UPDATE 28-03-2021**
    the 'reply scheme' hack doesnt work on mqtt but works on http: this code will be left since it works if the key is correct
    anyway and could be reused in a future attempt
    """
    if isinstance(key, dict):
        # no way! we're already keying as replykey workflow
        return header

    sign = md5((header[mc.KEY_MESSAGEID] + (key or "") + str(header[mc.KEY_TIMESTAMP])).encode('utf-8')).hexdigest()
    if sign == header[mc.KEY_SIGN]:
        return key

    return header


def get_productname(type: str) -> str:
    for _type, _name in mc.TYPE_NAME_MAP.items():
        if type.startswith(_type):
            return _name
    return type


def get_productnameuuid(type: str, uuid: str) -> str:
    return f"{get_productname(type)} ({uuid})"


def get_productnametype(type: str) -> str:
    name = get_productname(type)
    return f"{name} ({type})" if name is not type else type


class MerossDeviceDescriptor:
    """
        Utility class to extract various info from Appliance.System.All
        device descriptor
    """
    def __init__(self, payload: dict):
        self.all = dict()
        self.ability = dict()
        self.update(payload)

    @property
    def uuid(self) -> str:
        return self.hardware.get(mc.KEY_UUID)

    @property
    def macAddress(self) -> str:
        return self.hardware.get(mc.KEY_MACADDRESS, '48:e1:e9:XX:XX:XX')

    @property
    def ipAddress(self) -> str:
        return self.firmware.get(mc.KEY_INNERIP)

    @property
    def timezone(self) -> str:
        return self.system.get(mc.KEY_TIME, {}).get(mc.KEY_TIMEZONE)

    @property
    def productname(self) -> str:
        return get_productnameuuid(self.type, self.uuid)

    @property
    def productmodel(self) -> str:
        return f"{self.type} {self.hardware.get(mc.KEY_VERSION, '')}"


    def update(self, payload: dict):
        """
            reset the cached pointers
        """
        self.all = payload.get(mc.KEY_ALL, self.all)
        self.system = self.all.get(mc.KEY_SYSTEM, {})
        self.hardware = self.system.get(mc.KEY_HARDWARE, {})
        self.firmware = self.system.get(mc.KEY_FIRMWARE, {})
        self.digest = self.all.get(mc.KEY_DIGEST)
        self.ability = payload.get(mc.KEY_ABILITY, self.ability)
        self.type = self.hardware.get(mc.KEY_TYPE, mc.MANUFACTURER)# cache because using often

class MerossHttpClient:

    DEFAULT_TIMEOUT = 5

    def __init__(self,
                host: str,
                key: str = None,
                session: aiohttp.client.ClientSession = None,
                logger: logging.Logger = None
                ):
        self._host = host
        self._requesturl = URL(f"http://{host}/config")
        self.key = key
        self.replykey = None
        self._session = session or aiohttp.ClientSession()
        self._logger = logger or logging.getLogger(__name__)


    def set_host_key(self, host: str, key: str) -> None:
        if host != self._host:
            self._host = host
            self._requesturl = URL(f"http://{host}/config")
        self.key = key


    async def async_request(
        self,
        namespace: str,
        method: str = mc.METHOD_GET,
        payload: dict = {},
        timeout=DEFAULT_TIMEOUT
    ) -> dict:

        self._logger.debug("MerossHttpClient(%s): HTTP POST method:(%s) namespace:(%s)", self._host, method, namespace)

        request: dict = build_payload(namespace, method, payload, self.key or self.replykey)
        response: dict = await self.async_raw_request(request, timeout)

        if response.get(mc.KEY_PAYLOAD, {}).get(mc.KEY_ERROR, {}).get(mc.KEY_CODE) == 5001:
            #sign error... hack and fool
            self._logger.debug(
                "Key error on %s (%s:%s) -> retrying with key-reply hack",
                self._host, method, namespace)
            req_header = request[mc.KEY_HEADER]
            resp_header = response[mc.KEY_HEADER]
            req_header[mc.KEY_MESSAGEID] = resp_header[mc.KEY_MESSAGEID]
            req_header[mc.KEY_TIMESTAMP] = resp_header[mc.KEY_TIMESTAMP]
            req_header[mc.KEY_SIGN] = resp_header[mc.KEY_SIGN]
            response = await self.async_raw_request(request, timeout)

        return response

    async def async_raw_request(self, payload: dict, timeout=DEFAULT_TIMEOUT) -> dict:

        try:
            with async_timeout.timeout(timeout):
                response = await self._session.post(
                    url=self._requesturl,
                    data=json_dumps(payload)
                )
                response.raise_for_status()

            text_body = await response.text()
            self._logger.debug("MerossHttpClient(%s): HTTP Response (%s)", self._host, text_body)
            json_body:dict = json_loads(text_body)
            self.replykey = get_replykey(json_body.get(mc.KEY_HEADER), self.key)
        except Exception as e:
            self._logger.debug("MerossHttpClient(%s): HTTP Exception (%s)", self._host, str(e) or type(e).__name__)
            raise

        return json_body
