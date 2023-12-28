from __future__ import annotations

from base64 import b64encode
from hashlib import md5
import json
from time import time
import typing
from uuid import uuid4

import aiohttp
import async_timeout

from . import MEROSSDEBUG, MerossProtocolError, const as mc

SECRET = "23x17ahWarFH6w29"

API_URL = "https://iot.meross.com"
API_AUTH_LOGIN_PATH = "/v1/Auth/Login"
API_PROFILE_LOGOUT_PATH = "/v1/Profile/Logout"
API_DEVICE_DEVLIST_PATH = "/v1/Device/devList"
API_DEVICE_DEVINFO_PATH = "/v1/Device/devInfo"
API_DEVICE_DEVEXTRAINFO_PATH = "/v1/Device/devExtraInfo"
API_DEVICE_LATESTVERSION_PATH = "/v1/Device/latestVersion"
API_HUB_GETSUBDEVICES_PATH = "/v1/Hub/getSubDevices"

APISTATUS_NO_ERROR = 0
"""Not an error"""
APISTATUS_MISSING_PASSWORD = 1001
"""Wrong or missing password"""
APISTATUS_UNEXISTING_ACCOUNT = 1002
"""Account does not exist"""
APISTATUS_DISABLED_OR_DELETED_ACCOUNT = 1003
"""This account has been disabled or deleted"""
APISTATUS_WRONG_CREDENTIALS = 1004
"""Wrong email or password"""
APISTATUS_INVALID_EMAIL = 1005
"""Invalid email address"""
APISTATUS_BAD_PASSWORD_FORMAT = 1006
"""Bad password format"""
APISTATUS_WRONG_EMAIL = 1008
"""This email is not registered"""
APISTATUS_TOKEN_INVALID = 1019
"""Token expired"""
APISTATUS_TOKEN_ERROR = 1022
APISTATUS_TOKEN_EXPIRED = 1200
APISTATUS_TOO_MANY_TOKENS = 1301
"""Generic error (or unknown)"""
APISTATUS_GENERIC_ERROR = 5000

APISTATUS_MAP = {
    APISTATUS_NO_ERROR: "Not an error",
    APISTATUS_MISSING_PASSWORD: "Wrong or missing password",
    APISTATUS_UNEXISTING_ACCOUNT: "Account does not exist",
    APISTATUS_DISABLED_OR_DELETED_ACCOUNT: "This account has been disabled or deleted",
    APISTATUS_WRONG_CREDENTIALS: "Wrong email or password",
    APISTATUS_INVALID_EMAIL: "Invalid email address",
    APISTATUS_BAD_PASSWORD_FORMAT: "Bad password format",
    APISTATUS_WRONG_EMAIL: "This email is not registered",
    APISTATUS_TOKEN_INVALID: "Invalid Token",
    APISTATUS_TOKEN_ERROR: "Token error",
    APISTATUS_TOKEN_EXPIRED: "Token expired",
    APISTATUS_TOO_MANY_TOKENS: "Too many tokens",
    APISTATUS_GENERIC_ERROR: "Generic error",
}
# this is a guessed list of errors possibly due to invalid token
APISTATUS_TOKEN_ERRORS = {
    APISTATUS_DISABLED_OR_DELETED_ACCOUNT,
    APISTATUS_TOKEN_INVALID,
    APISTATUS_TOKEN_ERROR,
    APISTATUS_TOKEN_EXPIRED,
    APISTATUS_TOO_MANY_TOKENS,
}


class MerossCloudCredentials(typing.TypedDict):
    """
    Meross cloud credentyials as recovered from meross cloud api "/Auth/Login"
    """

    userid: str
    email: str
    key: str
    token: str


class DeviceInfoType(typing.TypedDict, total=False):
    """
    Device info as recovered from meross cloud api "/Device/devList"
    """

    uuid: str
    onlineStatus: int
    devName: str
    devIconId: str
    bindTime: int
    deviceType: str
    subType: str
    channels: list
    region: str
    fmwareVersion: str
    hdwareVersion: str
    userDevIcon: str
    iconType: int
    cluster: int
    domain: str  # optionally formatted as host:port
    reservedDomain: str  # optionally formatted as host:port
    __subDeviceInfo: dict[str, SubDeviceInfoType]  # this key is not from meross api


class LatestVersionType(typing.TypedDict, total=False):
    """
    firmware latest version(s) as recovered from meross cloud api "/Device/latestVersion"
    """

    type: str
    subType: str
    md5: str
    url: str
    version: str
    alias: str
    mcu: list
    upgradeType: str
    description: str


class SubDeviceInfoType(typing.TypedDict, total=False):
    """
    (Hub) SubDevice info as recovered from meross cloud api "/Hub/getSubDevices"
    """

    subDeviceId: str
    subDeviceType: str
    subDeviceVendor: str
    subDeviceName: str
    subDeviceIconId: str


class CloudApiError(MerossProtocolError):
    """
    signals an error when connecting to the public API endpoint
    """

    def __init__(self, response: dict, reason: object | None = None):
        self.apistatus = response.get(mc.KEY_APISTATUS)
        super().__init__(
            response,
            reason
            or APISTATUS_MAP.get(self.apistatus)  # type: ignore
            or response.get(mc.KEY_INFO)
            or json.dumps(response),
        )


async def async_cloudapi_post_raw(
    urlpath: str,
    data: object,
    token: str | None = None,
    session: aiohttp.ClientSession | None = None,
) -> dict:
    timestamp = int(time() * 1000)
    nonce = uuid4().hex
    params = json.dumps(data, ensure_ascii=False)
    params = b64encode(params.encode("utf-8")).decode("utf-8")
    sign = md5((SECRET + str(timestamp) + nonce + params).encode("utf-8"))
    async with async_timeout.timeout(10):
        response = await (session or aiohttp.ClientSession()).post(
            url=API_URL + urlpath,
            json={
                mc.KEY_TIMESTAMP: timestamp,
                mc.KEY_NONCE: nonce,
                mc.KEY_PARAMS: params,
                mc.KEY_SIGN: sign.hexdigest(),
            },
            headers={"Authorization": "Basic " + token} if token else None,
        )
        response.raise_for_status()
    return await response.json()


async def async_cloudapi_post(
    urlpath: str,
    data: object,
    token: str | None = None,
    session: aiohttp.ClientSession | None = None,
) -> dict:
    response = await async_cloudapi_post_raw(urlpath, data, token, session)
    if response.get(mc.KEY_APISTATUS) or (mc.KEY_DATA not in response):
        raise CloudApiError(response)
    return response


async def async_cloudapi_login(
    username: str, password: str, session: aiohttp.ClientSession | None = None
) -> MerossCloudCredentials:
    if MEROSSDEBUG and MEROSSDEBUG.cloudapi_login:
        if username == MEROSSDEBUG.cloudapi_login[mc.KEY_EMAIL]:
            return MEROSSDEBUG.cloudapi_login
        response = {mc.KEY_APISTATUS: APISTATUS_WRONG_EMAIL}
        raise CloudApiError(response)

    response = await async_cloudapi_post(
        API_AUTH_LOGIN_PATH,
        {mc.KEY_EMAIL: username, mc.KEY_PASSWORD: password},
        session=session,
    )
    data = response[mc.KEY_DATA]
    # formal check since we want to deal with 'safe' data structures
    for _key in {mc.KEY_USERID_, mc.KEY_EMAIL, mc.KEY_KEY, mc.KEY_TOKEN}:
        if _key not in data:
            raise CloudApiError(response, f"Missing '{_key}' in api response")
        _value = data[_key]
        if not isinstance(_value, str):
            raise CloudApiError(
                response,
                f"Key '{_key}' in api response is type '{_value.__class__.__name__}'. Expected 'str'",
            )
        if len(_value) == 0:
            raise CloudApiError(response, f"Key '{_key}' in api response is empty")

    return data


async def async_cloudapi_device_devlist(
    token: str, session: aiohttp.ClientSession | None = None
) -> list[DeviceInfoType]:
    """
    returns the {devInfo} list of all the account-bound devices
    """
    if MEROSSDEBUG and MEROSSDEBUG.cloudapi_device_devlist:
        return MEROSSDEBUG.cloudapi_device_devlist
    response = await async_cloudapi_post(API_DEVICE_DEVLIST_PATH, {}, token, session)
    return response[mc.KEY_DATA]


async def async_cloudapi_device_devinfo(
    token: str, uuid: str, session: aiohttp.ClientSession | None = None
) -> DeviceInfoType:
    """
    given the uuid, returns the {devInfo}
    """
    response = await async_cloudapi_post(
        API_DEVICE_DEVINFO_PATH, {mc.KEY_UUID: uuid}, token, session
    )
    return response[mc.KEY_DATA]


async def async_cloudapi_device_devextrainfo(
    token: str, session: aiohttp.ClientSession | None = None
) -> DeviceInfoType:
    """
    returns a list of all device types with their manuals download link
    """
    response = await async_cloudapi_post(
        API_DEVICE_DEVEXTRAINFO_PATH, {}, token, session
    )
    return response[mc.KEY_DATA]


async def async_cloudapi_device_latestversion(
    token: str, session: aiohttp.ClientSession | None = None
) -> list[LatestVersionType]:
    """
    returns the list of all the account-bound device types latest firmwares
    """
    if MEROSSDEBUG and MEROSSDEBUG.cloudapi_device_latestversion:
        return MEROSSDEBUG.cloudapi_device_latestversion
    response = await async_cloudapi_post(
        API_DEVICE_LATESTVERSION_PATH, {}, token, session
    )
    return response[mc.KEY_DATA]


async def async_cloudapi_hub_getsubdevices(
    token: str, uuid: str, session: aiohttp.ClientSession | None = None
) -> list[SubDeviceInfoType]:
    """
    given the uuid, returns the list of subdevices binded to the hub
    """
    response = await async_cloudapi_post(
        API_HUB_GETSUBDEVICES_PATH, {mc.KEY_UUID: uuid}, token, session
    )
    return response[mc.KEY_DATA]


async def async_cloudapi_logout(
    token: str, session: aiohttp.ClientSession | None = None
):
    await async_cloudapi_post(API_PROFILE_LOGOUT_PATH, {}, token, session)


async def async_cloudapi_logout_safe(
    token: str, session: aiohttp.ClientSession | None = None
):
    try:
        await async_cloudapi_post(API_PROFILE_LOGOUT_PATH, {}, token, session)
    except Exception:
        # this is very broad and might catch errors at the http layer which
        # mean we're not effectively invalidating the token but we don't
        # want to be too strict on token releases
        pass


async def async_get_cloud_key(
    username: str, password: str, session: aiohttp.ClientSession | None = None
) -> str:
    credentials = await async_cloudapi_login(username, password, session)
    # everything good:
    # kindly invalidate login token so to not exhaust our pool...
    await async_cloudapi_logout_safe(credentials[mc.KEY_TOKEN], session)
    return credentials[mc.KEY_KEY]
