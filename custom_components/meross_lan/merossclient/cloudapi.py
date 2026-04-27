import asyncio
from base64 import b64encode
from time import time
import typing
from uuid import uuid4

import aiohttp

from . import MEROSSDEBUG, logging
from .exceptions import MerossProtocolError
from .protocol import const as mc, md5hexdigest
from .protocol.message import json_dumps, json_loads

if typing.TYPE_CHECKING:
    from .protocol.types import JsonMapping

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
APISTATUS_MISSING_PASSWORD = 1001
APISTATUS_UNEXISTING_ACCOUNT = 1002
APISTATUS_DISABLED_OR_DELETED_ACCOUNT = 1003
APISTATUS_WRONG_CREDENTIALS = 1004
APISTATUS_INVALID_EMAIL = 1005
APISTATUS_BAD_PASSWORD_FORMAT = 1006
APISTATUS_WRONG_EMAIL = 1008
APISTATUS_TOKEN_INVALID = 1019
APISTATUS_TOKEN_ERROR = 1022
APISTATUS_REQUESTED_TOO_FREQUENTLY = 1028
APISTATUS_REDIRECT_REGION = 1030
APISTATUS_USER_NAME_NOT_MATCHING = 1031
APISTATUS_WRONG_MFA_CODE = 1032
APISTATUS_MFA_CODE_REQUIRED = 1033
APISTATUS_OPERATION_IS_LOCKED = 1035
APISTATUS_REPEAT_CHECK_IN = 1041
APISTATUS_TOP_LIMIT_REACHED = 1042
APISTATUS_RESOURCE_ACCESS_DENY = 1043
APISTATUS_TOKEN_EXPIRED = 1200
APISTATUS_TOO_MANY_TOKENS = 1301

APISTATUS_MAP = {
    APISTATUS_MISSING_PASSWORD: "Wrong or missing password",
    APISTATUS_UNEXISTING_ACCOUNT: "Account does not exist",
    APISTATUS_DISABLED_OR_DELETED_ACCOUNT: "This account has been disabled or deleted",
    APISTATUS_WRONG_CREDENTIALS: "Wrong email or password",
    APISTATUS_INVALID_EMAIL: "Invalid email address",
    APISTATUS_BAD_PASSWORD_FORMAT: "Bad password format",
    APISTATUS_WRONG_EMAIL: "This email is not registered",
    APISTATUS_TOKEN_INVALID: "Token invalid",
    APISTATUS_TOKEN_ERROR: "Token error",
    APISTATUS_REQUESTED_TOO_FREQUENTLY: "Requested too frequently",
    APISTATUS_REDIRECT_REGION: "HTTP api endpoint has moved",
    APISTATUS_USER_NAME_NOT_MATCHING: "Username does not match",
    APISTATUS_WRONG_MFA_CODE: "Wrong MFA Code",
    APISTATUS_MFA_CODE_REQUIRED: "MFA Code required",
    APISTATUS_OPERATION_IS_LOCKED: "Operation is locked",
    APISTATUS_REPEAT_CHECK_IN: "Repeat checkin",
    APISTATUS_TOP_LIMIT_REACHED: "API Top limit reached",
    APISTATUS_RESOURCE_ACCESS_DENY: "Resource access denied",
    APISTATUS_TOKEN_EXPIRED: "Token expired",
    APISTATUS_TOO_MANY_TOKENS: "Too many tokens",
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
    Meross cloud credentials as recovered from meross cloud api "/Auth/signIn"
    or "/Auth/Login" (the NotRequired fields were added in /Auth/signIn)
    """

    userid: str
    email: str
    key: str
    token: str
    domain: typing.NotRequired[str]
    mqttDomain: typing.NotRequired[str]
    mfaLockExpire: typing.NotRequired[int]


class DeviceInfoChannelType(typing.TypedDict):
    type: typing.NotRequired[str]
    devName: typing.NotRequired[str]
    devIconId: typing.NotRequired[str]


class SubDeviceInfoType(typing.TypedDict):
    """
    (Hub) SubDevice info as recovered from meross cloud api "/Hub/getSubDevices"
    """

    subDeviceId: str
    subDeviceType: str
    subDeviceSubType: str
    subDeviceVendor: typing.NotRequired[str]
    subDeviceName: str
    subDeviceIconId: str
    bindTime: int
    iconType: int
    # channels appearing on mst200
    channels: typing.NotRequired[list[DeviceInfoChannelType]]


class DeviceInfoType(typing.TypedDict):
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
    # channels might be empty or contain empty dicts
    channels: list[DeviceInfoChannelType]
    region: str
    fmwareVersion: str
    hdwareVersion: str
    userDevIcon: str
    iconType: int
    # cluster: int # looks removed in latest responses (2024.1)
    domain: str  # optionally formatted as host:port
    reservedDomain: str  # optionally formatted as host:port
    hardwareCapabilities: list


class LatestVersionMcuType(typing.TypedDict):
    """Mcu firmware update details. Available for devices exposing an mcu.
    Actually related to devices exposing
    - Appliance.Mcu.Firmware (mts200/300 series...maybe more)
    - Appliance.Mcu.Hp110.Firmware (smart cherub)
    """

    md5: str
    type: str
    url: str
    version: str


class LatestVersionType(typing.TypedDict):
    """
    firmware latest version(s) as recovered from meross cloud api "/Device/latestVersion"
    """

    type: str
    subType: str
    md5: str
    url: str
    version: str
    alias: str
    mcu: list[LatestVersionMcuType]
    upgradeType: str
    description: str


class CloudApiError(MerossProtocolError):
    """
    signals an error when connecting to the public API endpoint
    """

    def __init__(self, response: "JsonMapping", reason: object | None = None):
        self.apistatus = response.get(mc.KEY_APISTATUS)
        super().__init__(
            response,
            reason
            or APISTATUS_MAP.get(self.apistatus)  # type: ignore
            or response.get(mc.KEY_INFO)
            or json_dumps(response),
        )


class CloudApiRedirectError(CloudApiError):
    pass


class CloudApiMfaError(CloudApiError):
    pass


CLOUDAPI_ERROR_MAP: dict[int | None, type[CloudApiError]] = {
    APISTATUS_REDIRECT_REGION: CloudApiRedirectError,
    APISTATUS_WRONG_MFA_CODE: CloudApiMfaError,
    APISTATUS_MFA_CODE_REQUIRED: CloudApiMfaError,
}


async def async_cloudapi_post(
    url_or_path: str,
    data: "JsonMapping",
    *,
    credentials: MerossCloudCredentials | None = None,
    session: aiohttp.ClientSession | None = None,
    logger: "logging.LoggerType | None" = None,
) -> "JsonMapping":
    """
    Low-level Meross cloud api query:
    When used to login to retrieve the MerossCloudCredentials url_or_path contains the full
    url of the api endpoint, while when used to access the endpoint with an access token (crdentials != None)
    it needs to be the path since the full url will be created from the credentials itself.
    - 'logger' must be either a logging.Loggable or a _Logger instance (obtained through logging.getLogger),
    in order to correctly manage arguments obfuscation
    """
    try:
        if logger:
            logger.log(
                logging.DEBUG,
                "async_cloudapi_post:REQUEST url:%s data:%s credentials:%s",
                url_or_path,
                _payload=data,
                _any=credentials or {},
            )

        timestamp = int(time() * 1000)
        nonce = uuid4().hex
        params = json_dumps(data)
        params = b64encode(params.encode()).decode()
        if credentials:
            url_or_path = (
                credentials.get(mc.KEY_DOMAIN) or LEGACY_API_URL
            ) + url_or_path
            headers = (
                {"Authorization": "Basic " + credentials[mc.KEY_TOKEN]}
                if mc.KEY_TOKEN in credentials
                else None
            )
        else:
            headers = None
        json_request = {
            mc.KEY_TIMESTAMP: timestamp,
            mc.KEY_NONCE: nonce,
            mc.KEY_PARAMS: params,
            mc.KEY_SIGN: md5hexdigest(SECRET, str(timestamp), nonce, params),
        }
        if logger:
            logger.log(
                logging.DEBUG,
                "async_cloudapi_post:POST url:%s request:%s headers:%s",
                url_or_path,
                _payload=json_request,
                _any=headers or {},
            )
        async with asyncio.timeout(10):
            http_response = await (session or aiohttp.ClientSession()).post(
                url=url_or_path,
                json=json_request,
                headers=headers,
            )
            http_response.raise_for_status()

        text_response = await http_response.text()
        if logger:
            logger.log(
                logging.DEBUG,
                "async_cloudapi_post:RECEIVE url:%s response:%s",
                url_or_path,
                _any=text_response,
            )

        json_response = json_loads(text_response)
        if not isinstance(json_response, dict):
            raise Exception("HTTP response is not a json dictionary")

        apistatus = json_response.get(mc.KEY_APISTATUS)
        if apistatus or (mc.KEY_DATA not in json_response):
            raise CLOUDAPI_ERROR_MAP.get(apistatus, CloudApiError)(json_response)
        if logger:
            logger.log(
                logging.DEBUG,
                "async_cloudapi_post:RESPONSE url:%s response:%s",
                url_or_path,
                _payload=json_response,
            )
        return json_response
    except Exception as exception:
        if logger:
            logger.log(
                logging.DEBUG,
                "async_cloudapi_post:EXCEPTION %s(%s)",
                exception.__class__.__name__,
                str(exception),
            )
        raise exception


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
    mfa_code: str | None = None,
    session: aiohttp.ClientSession | None = None,
    logger: "logging.LoggerType | None" = None,
) -> MerossCloudCredentials:
    request_data = {
        mc.KEY_EMAIL: email,
        mc.KEY_PASSWORD: md5hexdigest(password),
        mc.KEY_ACCOUNTCOUNTRYCODE: region,
        mc.KEY_ENCRYPTION: 1,
        mc.KEY_AGREE: 0,
    }
    if mfa_code:
        request_data[mc.KEY_MFACODE] = mfa_code

    try:
        response = await async_cloudapi_post(
            (domain or API_URL_MAP.get(region, LEGACY_API_URL)) + API_AUTH_SIGNIN_PATH,
            request_data,
            credentials=None,
            session=session,
            logger=logger,
        )
    except CloudApiRedirectError as error:
        response = await async_cloudapi_post(
            error.response[mc.KEY_DATA][mc.KEY_DOMAIN] + API_AUTH_SIGNIN_PATH,
            request_data,
            credentials=None,
            session=session,
            logger=logger,
        )

    response_data = response[mc.KEY_DATA]
    # formal check since we want to deal with 'safe' data structures
    for _key in {mc.KEY_USERID_, mc.KEY_EMAIL, mc.KEY_KEY, mc.KEY_TOKEN}:
        if _key not in response_data:
            raise CloudApiError(response, f"Missing '{_key}' in api response")
        _value = response_data[_key]
        if not isinstance(_value, str):
            raise CloudApiError(
                response,
                f"Key '{_key}' in api response is type '{_value.__class__.__name__}'. Expected 'str'",
            )
        if len(_value) == 0:
            raise CloudApiError(response, f"Key '{_key}' in api response is empty")

    return response_data


class CloudApiClient(logging.Loggable):
    """
    Object-like interface to ease mantaining cloud api connection state
    """

    __slots__ = logging.Loggable._calc_slots("credentials", "_api_kwargs")

    def __init__(
        self,
        id="",
        parent: "logging.LoggerType | None" = None,
        *,
        credentials: MerossCloudCredentials | None = None,
        session: aiohttp.ClientSession | None = None,
    ) -> None:
        self.credentials = credentials
        self._api_kwargs = {
            "session": session or aiohttp.ClientSession(),
        }
        super().__init__(id, parent)

    async def async_signin(
        self,
        email: str,
        password: str,
        *,
        region: str | None = None,
        domain: str | None = None,
        mfa_code: str | None = None,
    ) -> MerossCloudCredentials:
        if MEROSSDEBUG and MEROSSDEBUG.cloudapi_login:
            if email == MEROSSDEBUG.cloudapi_login[mc.KEY_EMAIL]:
                self.credentials = MEROSSDEBUG.cloudapi_login
                return self.credentials
            raise CloudApiError({mc.KEY_APISTATUS: APISTATUS_WRONG_EMAIL})

        self.credentials = await async_cloudapi_signin(
            email,
            password,
            region=region,
            domain=domain,
            mfa_code=mfa_code,
            **self._api_kwargs,
            logger=self,
        )
        return self.credentials

    async def async_token_refresh(
        self, password: str, credentials: MerossCloudCredentials | None = None
    ):
        credentials = credentials or self.credentials
        assert (
            credentials
        ), "CloudApiClient.async_token_refresh called without credentials"

        newcredentials = await async_cloudapi_signin(
            credentials[mc.KEY_EMAIL],
            password,
            domain=credentials.get(mc.KEY_DOMAIN),
            **self._api_kwargs,  # type: ignore
            logger=self,
        )
        if newcredentials[mc.KEY_USERID_] != credentials[mc.KEY_USERID_]:
            # why would this happen ? Nevertheless we want to be sure since
            # userid is a key parameter in our design
            try:
                await async_cloudapi_post(
                    API_PROFILE_LOGOUT_PATH,
                    {},
                    **self._api_kwargs,
                    credentials=newcredentials,
                    logger=self,
                )
            except Exception:
                pass
            raise CloudApiError(
                {mc.KEY_DATA: newcredentials},
                "Mismatching userid in refreshed Meross cloud token",
            )
        self.credentials = newcredentials
        return newcredentials

    async def async_logout(self):
        if self.credentials:
            await async_cloudapi_post(
                API_PROFILE_LOGOUT_PATH,
                {},
                credentials=self.credentials,
                logger=self,
                **self._api_kwargs,
            )
            self.credentials = None

    async def async_logout_safe(self):
        if self.credentials:
            try:
                await async_cloudapi_post(
                    API_PROFILE_LOGOUT_PATH,
                    {},
                    credentials=self.credentials,
                    logger=self,
                    **self._api_kwargs,
                )
            except Exception:
                # this is very broad and might catch errors at the http layer which
                # mean we're not effectively invalidating the token but we don't
                # want to be too strict on token releases
                pass
            self.credentials = None

    async def async_device_devlist(self) -> list[DeviceInfoType]:
        """
        returns the {devInfo} list of all the account-bound devices
        """
        if MEROSSDEBUG and MEROSSDEBUG.cloudapi_device_devlist:
            return MEROSSDEBUG.cloudapi_device_devlist
        return (
            await async_cloudapi_post(
                API_DEVICE_DEVLIST_PATH,
                {},
                credentials=self.credentials,
                logger=self,
                **self._api_kwargs,
            )
        )[mc.KEY_DATA]

    async def async_device_devinfo(self, uuid: str) -> DeviceInfoType:
        """
        given the uuid, returns the {devInfo}
        """
        return (
            await async_cloudapi_post(
                API_DEVICE_DEVINFO_PATH,
                {mc.KEY_UUID: uuid},
                credentials=self.credentials,
                logger=self,
                **self._api_kwargs,
            )
        )[mc.KEY_DATA]

    async def async_device_devextrainfo(self) -> DeviceInfoType:
        """
        returns a list of all device types with their manuals download link
        """
        return (
            await async_cloudapi_post(
                API_DEVICE_DEVEXTRAINFO_PATH,
                {},
                credentials=self.credentials,
                logger=self,
                **self._api_kwargs,
            )
        )[mc.KEY_DATA]

    async def async_device_latestversion(self) -> list[LatestVersionType]:
        """
        returns the list of all the account-bound device types latest firmwares
        """
        if MEROSSDEBUG and MEROSSDEBUG.cloudapi_device_latestversion:
            return MEROSSDEBUG.cloudapi_device_latestversion
        return (
            await async_cloudapi_post(
                API_DEVICE_LATESTVERSION_PATH,
                {},
                credentials=self.credentials,
                logger=self,
                **self._api_kwargs,
            )
        )[mc.KEY_DATA]

    async def async_hub_getsubdevices(self, uuid: str) -> list[SubDeviceInfoType]:
        """
        given the uuid, returns the list of subdevices binded to the hub
        """
        return (
            await async_cloudapi_post(
                API_HUB_GETSUBDEVICES_PATH,
                {mc.KEY_UUID: uuid},
                credentials=self.credentials,
                logger=self,
                **self._api_kwargs,
            )
        )[mc.KEY_DATA]
