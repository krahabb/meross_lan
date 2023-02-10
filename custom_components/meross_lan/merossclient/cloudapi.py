from __future__ import annotations
from uuid import uuid4
from hashlib import md5
from base64 import b64encode
from time import time
from json import dumps as json_dumps
import async_timeout
import aiohttp

from . import const as mc
from . import MerossProtocolError


class MerossApiError(MerossProtocolError):
    """
    signals an error when connecting to the public API endpoint
    """

async def async_merossapi_post(
        urlpath: str,
        data: object,
        token: str | None = None,
        session: aiohttp.ClientSession | None = None
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
        session: aiohttp.ClientSession | None = None
    ) -> str:
    response = await async_merossapi_post(
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
                    await async_merossapi_post(
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
