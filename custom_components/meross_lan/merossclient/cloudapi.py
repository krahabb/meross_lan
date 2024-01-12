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

LEGACY_API_URL = "https://iot.meross.com"
API_URL_MAP: dict[str | None, str] = {
    "ap": "https://iotx-ap.meross.com",
    "eu": "https://iotx-eu.meross.com",
    "us": "https://iotx-us.meross.com",
}
API_AUTH_LOGIN_PATH = "/v1/Auth/Login"
API_AUTH_SIGNIN_PATH = "/v1/Auth/signIn"
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
"""Token invalid"""
APISTATUS_TOKEN_ERROR = 1022
"""Token error"""
APISTATUS_REQUESTED_TOO_FREQUENTLY = 1028
"""Requested too frequently"""
APISTATUS_REDIRECT_REGION = 1030
"""HTTP api endpoint has moved"""
APISTATUS_USER_NAME_NOT_MATCHING = 1031
"""Username does not match"""
APISTATUS_WRONG_MFA_CODE = 1032
"""Wrong MFA Code"""
APISTATUS_MFA_CODE_REQUIRED = 1033
"""MFA Code required"""
APISTATUS_OPERATION_IS_LOCKED = 1035
"""Operation is locked"""
APISTATUS_REPEAT_CHECK_IN = 1041
"""Repeat checkin"""
APISTATUS_TOP_LIMIT_REACHED = 1042
"""API Top limit reached"""
APISTATUS_RESOURCE_ACCESS_DENY = 1043
"""Resource access denied"""
APISTATUS_TOKEN_EXPIRED = 1200
"""Token expired"""
APISTATUS_TOO_MANY_TOKENS = 1301
"""Too many tokens"""
APISTATUS_GENERIC_ERROR = 5000
"""Generic error (or unknown)"""

APISTATUS_MAP = {
    APISTATUS_NO_ERROR: APISTATUS_NO_ERROR.__doc__,
    APISTATUS_MISSING_PASSWORD: APISTATUS_MISSING_PASSWORD.__doc__,
    APISTATUS_UNEXISTING_ACCOUNT: APISTATUS_UNEXISTING_ACCOUNT.__doc__,
    APISTATUS_DISABLED_OR_DELETED_ACCOUNT: APISTATUS_DISABLED_OR_DELETED_ACCOUNT.__doc__,
    APISTATUS_WRONG_CREDENTIALS: APISTATUS_WRONG_CREDENTIALS.__doc__,
    APISTATUS_INVALID_EMAIL: APISTATUS_INVALID_EMAIL.__doc__,
    APISTATUS_BAD_PASSWORD_FORMAT: APISTATUS_BAD_PASSWORD_FORMAT.__doc__,
    APISTATUS_WRONG_EMAIL: APISTATUS_WRONG_EMAIL.__doc__,
    APISTATUS_TOKEN_INVALID: APISTATUS_TOKEN_INVALID.__doc__,
    APISTATUS_TOKEN_ERROR: APISTATUS_TOKEN_ERROR.__doc__,
    APISTATUS_REQUESTED_TOO_FREQUENTLY: APISTATUS_REQUESTED_TOO_FREQUENTLY.__doc__,
    APISTATUS_REDIRECT_REGION: APISTATUS_REDIRECT_REGION.__doc__,
    APISTATUS_USER_NAME_NOT_MATCHING: APISTATUS_USER_NAME_NOT_MATCHING.__doc__,
    APISTATUS_WRONG_MFA_CODE: APISTATUS_WRONG_MFA_CODE.__doc__,
    APISTATUS_MFA_CODE_REQUIRED: APISTATUS_MFA_CODE_REQUIRED.__doc__,
    APISTATUS_OPERATION_IS_LOCKED: APISTATUS_OPERATION_IS_LOCKED.__doc__,
    APISTATUS_REPEAT_CHECK_IN: APISTATUS_REPEAT_CHECK_IN.__doc__,
    APISTATUS_TOP_LIMIT_REACHED: APISTATUS_TOP_LIMIT_REACHED.__doc__,
    APISTATUS_RESOURCE_ACCESS_DENY: APISTATUS_RESOURCE_ACCESS_DENY.__doc__,
    APISTATUS_TOKEN_EXPIRED: APISTATUS_TOKEN_EXPIRED.__doc__,
    APISTATUS_TOO_MANY_TOKENS: APISTATUS_TOO_MANY_TOKENS.__doc__,
    APISTATUS_GENERIC_ERROR: APISTATUS_GENERIC_ERROR.__doc__,
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
    domain: typing.NotRequired[str]
    mqttDomain: typing.NotRequired[str]
    mfaLockExpire: typing.NotRequired[int]


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
    # cluster: int # looks removed in latest responses (2024.1)
    domain: str  # optionally formatted as host:port
    reservedDomain: str  # optionally formatted as host:port
    hardwareCapabilities: list
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


class CloudApiRedirectError(CloudApiError):
    pass


CLOUDAPI_ERROR_MAP: dict[int | None, type[CloudApiError]] = {
    APISTATUS_REDIRECT_REGION: CloudApiRedirectError,
}


async def async_cloudapi_post_raw(
    url_or_path: str,
    data: object,
    credentials: MerossCloudCredentials | None = None,
    session: aiohttp.ClientSession | None = None,
) -> dict:
    """
    Low-level Meross cloud api query:
    When used to login to retrieve the MerossCloudCredentials url_or_path contains the full
    url of the api endpoint, while when used to access the endpoint with an access token (crdentials != None)
    it needs to be the path since the full url will be created from the credentials itself
    """
    timestamp = int(time() * 1000)
    nonce = uuid4().hex
    params = json.dumps(data, ensure_ascii=False)
    params = b64encode(params.encode("utf-8")).decode("utf-8")
    sign = md5(
        "".join((SECRET, str(timestamp), nonce, params)).encode("utf-8")
    ).hexdigest()
    if credentials:
        url_or_path = (credentials.get(mc.KEY_DOMAIN) or LEGACY_API_URL) + url_or_path
        headers = (
            {"Authorization": "Basic " + credentials[mc.KEY_TOKEN]}
            if mc.KEY_TOKEN in credentials
            else None
        )
    else:
        headers = None
    async with async_timeout.timeout(10):
        response = await (session or aiohttp.ClientSession()).post(
            url=url_or_path,
            json={
                mc.KEY_TIMESTAMP: timestamp,
                mc.KEY_NONCE: nonce,
                mc.KEY_PARAMS: params,
                mc.KEY_SIGN: sign,
            },
            headers=headers,
        )
        response.raise_for_status()
    return await response.json()


async def async_cloudapi_post(
    url_or_path: str,
    data: object,
    credentials: MerossCloudCredentials | None = None,
    session: aiohttp.ClientSession | None = None,
) -> dict:
    response = await async_cloudapi_post_raw(url_or_path, data, credentials, session)
    apistatus = response.get(mc.KEY_APISTATUS)
    if apistatus or (mc.KEY_DATA not in response):
        raise CLOUDAPI_ERROR_MAP.get(apistatus, CloudApiError)(response)
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
        LEGACY_API_URL + API_AUTH_LOGIN_PATH,
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


async def async_cloudapi_signin(
    email: str,
    password: str,
    *,
    region: str | None = None,
    domain: str | None = None,
    session: aiohttp.ClientSession | None = None,
) -> MerossCloudCredentials:
    if MEROSSDEBUG and MEROSSDEBUG.cloudapi_login:
        if email == MEROSSDEBUG.cloudapi_login[mc.KEY_EMAIL]:
            return MEROSSDEBUG.cloudapi_login
        response = {mc.KEY_APISTATUS: APISTATUS_WRONG_EMAIL}
        raise CloudApiError(response)

    data = {
        mc.KEY_EMAIL: email,
        mc.KEY_PASSWORD: md5(password.encode("utf8")).hexdigest(),
        # "accountCountryCode": region,
        "encryption": 1,
        "agree": 0,
    }

    try:
        response = await async_cloudapi_post(
            (domain or API_URL_MAP.get(region, LEGACY_API_URL)) + API_AUTH_SIGNIN_PATH,
            data,
            session=session,
        )
    except CloudApiRedirectError as error:
        response = await async_cloudapi_post(
            error.response[mc.KEY_DATA][mc.KEY_DOMAIN] + API_AUTH_SIGNIN_PATH,
            data,
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
    credentials: MerossCloudCredentials, session: aiohttp.ClientSession | None = None
) -> list[DeviceInfoType]:
    """
    returns the {devInfo} list of all the account-bound devices
    """
    if MEROSSDEBUG and MEROSSDEBUG.cloudapi_device_devlist:
        return MEROSSDEBUG.cloudapi_device_devlist
    response = await async_cloudapi_post(
        API_DEVICE_DEVLIST_PATH, {}, credentials, session
    )
    return response[mc.KEY_DATA]


async def async_cloudapi_device_devinfo(
    credentials: MerossCloudCredentials,
    uuid: str,
    session: aiohttp.ClientSession | None = None,
) -> DeviceInfoType:
    """
    given the uuid, returns the {devInfo}
    """
    response = await async_cloudapi_post(
        API_DEVICE_DEVINFO_PATH, {mc.KEY_UUID: uuid}, credentials, session
    )
    return response[mc.KEY_DATA]


async def async_cloudapi_device_devextrainfo(
    credentials: MerossCloudCredentials, session: aiohttp.ClientSession | None = None
) -> DeviceInfoType:
    """
    returns a list of all device types with their manuals download link
    """
    response = await async_cloudapi_post(
        API_DEVICE_DEVEXTRAINFO_PATH, {}, credentials, session
    )
    return response[mc.KEY_DATA]


async def async_cloudapi_device_latestversion(
    credentials: MerossCloudCredentials, session: aiohttp.ClientSession | None = None
) -> list[LatestVersionType]:
    """
    returns the list of all the account-bound device types latest firmwares
    """
    if MEROSSDEBUG and MEROSSDEBUG.cloudapi_device_latestversion:
        return MEROSSDEBUG.cloudapi_device_latestversion
    response = await async_cloudapi_post(
        API_DEVICE_LATESTVERSION_PATH, {}, credentials, session
    )
    return response[mc.KEY_DATA]


async def async_cloudapi_hub_getsubdevices(
    credentials: MerossCloudCredentials,
    uuid: str,
    session: aiohttp.ClientSession | None = None,
) -> list[SubDeviceInfoType]:
    """
    given the uuid, returns the list of subdevices binded to the hub
    """
    response = await async_cloudapi_post(
        API_HUB_GETSUBDEVICES_PATH, {mc.KEY_UUID: uuid}, credentials, session
    )
    return response[mc.KEY_DATA]


async def async_cloudapi_logout(
    credentials: MerossCloudCredentials, session: aiohttp.ClientSession | None = None
):
    await async_cloudapi_post(API_PROFILE_LOGOUT_PATH, {}, credentials, session)


async def async_cloudapi_logout_safe(
    credentials: MerossCloudCredentials, session: aiohttp.ClientSession | None = None
):
    try:
        await async_cloudapi_post(API_PROFILE_LOGOUT_PATH, {}, credentials, session)
    except Exception:
        # this is very broad and might catch errors at the http layer which
        # mean we're not effectively invalidating the token but we don't
        # want to be too strict on token releases
        pass
