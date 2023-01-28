"""An Http API Client to interact with meross devices"""
from typing import Union
from logging import Logger, getLogger, DEBUG
from uuid import uuid4
from hashlib import md5
from base64 import b64encode
from time import time
from json import (
    dumps as json_dumps,
    loads as json_loads,
)
import asyncio
import async_timeout
import aiohttp

from yarl import URL

from . import const as mc


KeyType = Union[dict, str, None]

class MerossProtocolError(Exception):
    """
    signal a protocol error like:
    - missing header keys
    - application layer ERROR(s)

    reason is an error payload (dict) if the protocol is formally correct
    and the device replied us with "method" : "ERROR"
    and "payload" : { "error": { "code": (int), "detail": (str) } }
    in a more general case it could be the exception raised by accessing missing
    fields or a "signature error" in our validation
    """
    def __init__(self, reason):
        super().__init__()
        self.reason = reason


class MerossKeyError(MerossProtocolError):
    """
    signal a protocol key error (wrong key)
    reported by device
    """

class MerossSignatureError(MerossProtocolError):
    """
    signal a protocol signature error detected
    when validating the received header
    """
    def __init__(self):
        super().__init__("Signature error")


class MerossApiError(MerossProtocolError):
    """
    signals an error when connecting to the public API endpoint
    """

def build_payload(
    namespace:str,
    method:str,
    payload:dict,
    key:KeyType,
    from_:str,
    messageid:str | None = None
)-> dict:
    if isinstance(key, dict):
        key[mc.KEY_NAMESPACE] = namespace
        key[mc.KEY_METHOD] = method
        key[mc.KEY_PAYLOADVERSION] = 1
        key[mc.KEY_FROM] = from_
        return {
            mc.KEY_HEADER: key,
            mc.KEY_PAYLOAD: payload
        }
    else:
        if messageid is None:
            messageid = uuid4().hex
        timestamp = int(time())
        return {
            mc.KEY_HEADER: {
                mc.KEY_MESSAGEID: messageid,
                mc.KEY_NAMESPACE: namespace,
                mc.KEY_METHOD: method,
                mc.KEY_PAYLOADVERSION: 1,
                mc.KEY_FROM: from_,
                #mc.KEY_FROM: "/app/0-0/subscribe",
                #"from": "/appliance/9109182170548290882048e1e9522946/publish",
                mc.KEY_TIMESTAMP: timestamp,
                mc.KEY_TIMESTAMPMS: 0,
                mc.KEY_SIGN: md5((messageid + (key or "") + str(timestamp)).encode('utf-8')).hexdigest()
            },
            mc.KEY_PAYLOAD: payload
        }

def get_namespacekey(namespace: str) -> str:
    """
    return the 'well known' key for the provided namespace
    which is used as the root key of the associated payload
    This is usually the camelCase of the last split of the namespace
    """
    if namespace in mc.PAYLOAD_GET:
        return next(iter(mc.PAYLOAD_GET[namespace]))
    key = namespace.split('.')[-1]
    return key[0].lower() + key[1:]

def build_default_payload_get(namespace: str) -> dict:
    """
    when we query a device 'namespace' with a GET method the request payload
    is usually 'well structured' (more or less). We have a dictionary of
    well-known payloads else we'll use some heuristics
    """
    if namespace in mc.PAYLOAD_GET:
        return mc.PAYLOAD_GET[namespace]
    split = namespace.split('.')
    key = split[-1]
    return { key[0].lower() + key[1:]: [] if split[1] == 'Hub' else {} }

def get_replykey(header: dict, key:KeyType = None) -> KeyType:
    """
    checks header signature against key:
    if ok return sign itsef else return the full header { "messageId", "timestamp", "sign", ...}
    in order to be able to use it in a reply scheme
    **UPDATE 28-03-2021**
    the 'reply scheme' hack doesnt work on mqtt but works on http: this code will be left since it works if the key is correct
    anyway and could be reused in a future attempt
    """
    if isinstance(key, str):
        sign = md5((header[mc.KEY_MESSAGEID] + key + str(header[mc.KEY_TIMESTAMP])).encode('utf-8')).hexdigest()
        if sign == header[mc.KEY_SIGN]:
            return key

    return header

def get_productname(producttype: str) -> str:
    for _type, _name in mc.TYPE_NAME_MAP.items():
        if producttype.startswith(_type):
            return _name
    return producttype

def get_productnameuuid(producttype: str, uuid: str) -> str:
    return f"{get_productname(producttype)} ({uuid})"

def get_productnametype(producttype: str) -> str:
    name = get_productname(producttype)
    return f"{name} ({producttype})" if name is not producttype else producttype


async def async_httpapi_post(
        urlpath: str,
        data: object,
        token: str | None = None,
        session: aiohttp.client.ClientSession | None = None
    ) -> dict:
    session = session or aiohttp.ClientSession()
    timestamp = int(time())
    nonce = uuid4().hex
    params = json_dumps(data, ensure_ascii=False)
    params = b64encode(params.encode('utf-8')).decode('ascii')
    sign = md5(("23x17ahWarFH6w29" + str(timestamp) + nonce + params).encode('utf-8')).hexdigest()
    with async_timeout.timeout(10):
        response = await session.post(
            url=mc.MEROSS_API_V1_URL + urlpath,
            json={
                mc.KEY_TIMESTAMP: timestamp,
                mc.KEY_NONCE: nonce,
                mc.KEY_PARAMS: params,
                mc.KEY_SIGN: sign
            },
            headers= {"Authorization": "Basic " + token} if token else None
        )
        response.raise_for_status()
    return await response.json()

async def async_get_cloud_key(
        username: str,
        password: str,
        session: aiohttp.client.ClientSession | None = None
    ) -> str:
    response = await async_httpapi_post(
        mc.MEROSS_API_LOGIN_PATH,
        {"email": username, "password": password},
        session=session
    )
    try:
        data = response[mc.KEY_DATA]
        if data:
            key = data[mc.KEY_KEY]
            if key:
                # everything good:
                # kindly invalidate login token so to not exhaust our pool...
                try:
                    await async_httpapi_post(
                        mc.MEROSS_API_LOGOUT_PATH,
                        {},
                        data[mc.KEY_TOKEN],
                        session=session
                    )
                except:
                    pass# don't care if any failure here: we have the key anyway
                return key
    except:
        pass
    # try to best-effort parse the api response code for hints on the error
    if isinstance(apistatus := response.get(mc.KEY_APISTATUS), int):
        if errormsg := mc.APISTATUS_MAP.get(apistatus):
            raise MerossApiError(errormsg)
    # 'info' sometimes carries useful msg
    if info := response.get(mc.KEY_INFO):
        raise MerossApiError(info)
    # fallback to raise the entire response
    raise MerossApiError(json_dumps(response))


class MerossDeviceDescriptor:
    """
        Utility class to extract various info from Appliance.System.All
        device descriptor
    """
    all = {}
    ability: dict
    digest: dict
    time: dict | None
    timezone: str | None

    _dynamicattrs = {
        mc.KEY_SYSTEM: lambda _self: _self.all.get(mc.KEY_SYSTEM, {}),
        mc.KEY_HARDWARE: lambda _self: _self.system.get(mc.KEY_HARDWARE, {}),
        mc.KEY_FIRMWARE: lambda _self: _self.system.get(mc.KEY_FIRMWARE, {}),
        mc.KEY_TYPE: lambda _self: _self.hardware.get(mc.KEY_TYPE, mc.MANUFACTURER),
        mc.KEY_UUID: lambda _self: _self.hardware.get(mc.KEY_UUID),
        mc.KEY_MACADDRESS: lambda _self: _self.hardware.get(mc.KEY_MACADDRESS, mc.MEROSS_MACADDRESS),
        mc.KEY_INNERIP: lambda _self: _self.firmware.get(mc.KEY_INNERIP),
        mc.KEY_TIME: lambda _self: _self.system.get(mc.KEY_TIME, {}),
        mc.KEY_TIMEZONE: lambda _self: _self.time.get(mc.KEY_TIMEZONE),
        'productname': lambda _self: get_productnameuuid(_self.type, _self.uuid),
        'productmodel': lambda _self: f"{_self.type} {_self.hardware.get(mc.KEY_VERSION, '')}"
    }

    def __init__(self, payload: dict):
        self.ability = payload.get(mc.KEY_ABILITY, {})
        self.update(payload)

    def __getattr__(self, name):
        value = MerossDeviceDescriptor._dynamicattrs[name](self)
        setattr(self, name, value)
        return value

    def update(self, payload: dict):
        """
            reset the cached pointers
        """
        self.all = payload.get(mc.KEY_ALL, self.all)
        self.digest = self.all.get(mc.KEY_DIGEST, {})
        for key in MerossDeviceDescriptor._dynamicattrs.keys():
            try:
                delattr(self, key)
            except Exception:
                continue

    def update_time(self, p_time: dict):
        self.system[mc.KEY_TIME] = p_time
        self.time = p_time
        self.timezone = p_time.get(mc.KEY_TIMEZONE)


class MerossHttpClient:

    timeout = 5 # total timeout will be 1+2+4: check relaxation algorithm

    def __init__(
        self,
        host: str,
        key: KeyType = None,
        session: aiohttp.client.ClientSession | None = None,
        logger: Logger | None = None
    ):
        self._host = host
        self._requesturl = URL(f"http://{host}/config")
        self.key = key # key == None for hack-mode
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
                self._logger.debug("MerossHttpClient(%s): HTTP Request (%s)", debugid, request_data)
            else:
                request_data = json_dumps(request)
            # since device HTTP service sometimes timeouts with no apparent
            # reason we're using an increasing timeout loop to try recover
            # when this timeout is transient
            while True:
                try:
                    with async_timeout.timeout(timeout):
                        response = await self._session.post(
                            url=self._requesturl,
                            data=request_data
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
                self._logger.debug("MerossHttpClient(%s): HTTP Response (%s)", debugid, text_body)
            json_body:dict = json_loads(text_body)
            if self.key is None:
                self.replykey = get_replykey(json_body[mc.KEY_HEADER], self.key)
        except Exception as e:
            self.replykey = None # reset the key hack since it could became stale
            if debugid is not None:
                self._logger.debug("MerossHttpClient(%s): HTTP %s (%s)", debugid, type(e).__name__, str(e))
            raise e

        return json_body

    async def async_request(self, namespace: str, method: str, payload: dict) -> dict:
        key = self.key
        request: dict = build_payload(
            namespace, method, payload, self.replykey if key is None else key, mc.MANUFACTURER)
        response: dict = await self.async_request_raw(request)
        if response.get(mc.KEY_PAYLOAD, {}).get(mc.KEY_ERROR, {}).get(mc.KEY_CODE) == mc.ERROR_INVALIDKEY:
            if key is not None:
                raise MerossKeyError(response.get(mc.KEY_PAYLOAD))
            #sign error... hack and fool
            if self._logger.isEnabledFor(DEBUG):
                self._logger.debug(
                    "Key error on %s (%s:%s) -> retrying with key-reply hack",
                    self._host, method, namespace)
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

    async def async_request_strict(self, namespace: str, method: str, payload: dict) -> dict:
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
            raise MerossProtocolError(e)

        if r_method == mc.METHOD_ERROR:
            if r_payload.get(mc.KEY_ERROR, {}).get(mc.KEY_CODE) == mc.ERROR_INVALIDKEY:
                raise MerossKeyError(r_payload)
            else:
                raise MerossProtocolError(r_payload)

        return response

    async def async_request_strict_get(self, namespace: str) -> dict:
        return await self.async_request_strict(
            namespace,
            mc.METHOD_GET,
            build_default_payload_get(namespace)
        )
