from __future__ import annotations

from base64 import b64encode
from hashlib import md5
from json import dumps as json_dumps
import logging
import ssl
import threading
from time import time
import typing
from uuid import uuid4

import aiohttp
import async_timeout
import paho.mqtt.client as mqtt

from . import MEROSSDEBUG, MerossProtocolError, const as mc

SECRET = "23x17ahWarFH6w29"

API_URL = "https://iot.meross.com"
API_AUTH_LOGIN_PATH = "/v1/Auth/Login"
API_PROFILE_LOGOUT_PATH = "/v1/Profile/Logout"
API_DEVICE_DEVLIST_PATH = "/v1/Device/devList"
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
APISTATUS_TOKEN_ERRORS = {
    APISTATUS_TOKEN_INVALID,
    APISTATUS_TOKEN_ERROR,
    APISTATUS_TOKEN_EXPIRED,
}

LOGGER = logging.getLogger(__name__)


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


def generate_app_id():
    return md5(uuid4().hex.encode("utf-8")).hexdigest()


def parse_domain(domain: str):
    if (colon_index := domain.find(":")) != -1:
        return domain[0:colon_index], int(domain[colon_index + 1 :])
    else:
        return domain, 443


class SubDeviceInfoType(typing.TypedDict, total=False):
    """
    (Hub) SubDevice info as recovered from meross cloud api "/Hub/getSubDevices"
    """

    subDeviceId: str
    subDeviceType: str
    subDeviceVendor: str
    subDeviceName: str
    subDeviceIconId: str


"""
actually unused since we cant force cast the devList elements
class MerossDeviceInfo(dict):

    @property
    def uuid(self):
        return self[mc.KEY_UUID]
"""


class CloudApiError(MerossProtocolError):
    """
    signals an error when connecting to the public API endpoint
    """

    def __init__(self, response: dict, reason: object | None = None):
        self.apistatus = response.get(mc.KEY_APISTATUS)
        if reason is None:
            reason = APISTATUS_MAP.get(self.apistatus)  # type: ignore
        if reason is None:
            # 'info' sometimes carries useful msg
            reason = response.get(mc.KEY_INFO)
        if not reason:
            # fallback to raise the entire response
            reason = json_dumps(response)
        super().__init__(response, reason)


async def async_cloudapi_post_raw(
    urlpath: str,
    data: object,
    token: str | None = None,
    session: aiohttp.ClientSession | None = None,
) -> dict:
    timestamp = int(time() * 1000)
    nonce = uuid4().hex
    params = json_dumps(data, ensure_ascii=False)
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


async def async_cloudapi_devicelist(
    token: str, session: aiohttp.ClientSession | None = None
) -> list[DeviceInfoType]:
    if MEROSSDEBUG and MEROSSDEBUG.cloudapi_devicelist:
        return MEROSSDEBUG.cloudapi_devicelist
    response = await async_cloudapi_post(API_DEVICE_DEVLIST_PATH, {}, token, session)
    return response[mc.KEY_DATA]


async def async_cloudapi_subdevicelist(
    token: str, uuid: str, session: aiohttp.ClientSession | None = None
) -> list[SubDeviceInfoType]:
    response = await async_cloudapi_post(
        API_HUB_GETSUBDEVICES_PATH, {mc.KEY_UUID: uuid}, token, session
    )
    return response[mc.KEY_DATA]


async def async_cloudapi_logout(
    token: str, session: aiohttp.ClientSession | None = None
):
    await async_cloudapi_post(API_PROFILE_LOGOUT_PATH, {}, token, session)


async def async_get_cloud_key(
    username: str, password: str, session: aiohttp.ClientSession | None = None
) -> str:
    credentials = await async_cloudapi_login(username, password, session)
    # everything good:
    # kindly invalidate login token so to not exhaust our pool...
    try:
        await async_cloudapi_logout(credentials[mc.KEY_TOKEN], session)
    except Exception:
        pass  # don't care if any failure here: we have the key anyway
    return credentials[mc.KEY_KEY]


class MerossMQTTClient(mqtt.Client):
    STATE_CONNECTING = "connecting"
    STATE_CONNECTED = "connected"
    STATE_RECONNECTING = "reconnecting"
    STATE_DISCONNECTING = "disconnecting"
    STATE_DISCONNECTED = "disconnected"

    def __init__(self, credentials: MerossCloudCredentials, app_id: str | None = None):
        self._stateext = self.STATE_DISCONNECTED
        if not isinstance(app_id, str):
            app_id = generate_app_id()
        self.app_id = app_id
        userid = credentials[mc.KEY_USERID_]
        self.topic_command = f"/app/{userid}-{app_id}/subscribe"
        self.topic_push = f"/app/{userid}/subscribe"
        self.lock = threading.Lock()
        super().__init__(f"app:{app_id}", protocol=mqtt.MQTTv311)
        self.username_pw_set(
            userid, md5(f"{userid}{credentials[mc.KEY_KEY]}".encode("utf8")).hexdigest()
        )
        self.tls_set(cert_reqs=ssl.CERT_REQUIRED, tls_version=ssl.PROTOCOL_TLS_CLIENT)
        self.on_connect = self._mqttc_connect
        self.on_disconnect = self._mqttc_disconnect
        self.suppress_exceptions = True
        if MEROSSDEBUG and MEROSSDEBUG.mqtt_client_log_enable:
            self.enable_logger(LOGGER)

    @property
    def stateext(self):
        return self._stateext

    @property
    def state_active(self):
        return self._stateext not in (self.STATE_DISCONNECTING, self.STATE_DISCONNECTED)

    @property
    def state_inactive(self):
        return self._stateext in (self.STATE_DISCONNECTING, self.STATE_DISCONNECTED)

    def safe_connect(self, host: str, port: int):
        """
        Safe to be called from any thread (except the mqtt one). Could be a bit
        'blocking' if the thread needs to be stopped (in case it was still running).
        The effective connection is asynchronous and will be managed by the thread
        """
        with self.lock:
            # paho mqtt client has a very crazy interface so we cannot know
            # for sure the internal state (being it 'connecting' or 'new'
            # or whatever) so we just try to be as 'safe' as possible given
            # its behavior
            self.loop_stop()  # in case we're connected or connecting or disconnecting
            self.connect_async(host, port)
            self._stateext = self.STATE_CONNECTING
            self.loop_start()

    def safe_disconnect(self):
        """
        Safe to be called from any thread (except the mqtt one)
        This is non-blocking and the thread will just die
        by itself.
        """
        with self.lock:
            self._stateext = self.STATE_DISCONNECTING
            if mqtt.MQTT_ERR_NO_CONN == self.disconnect():
                self._stateext = self.STATE_DISCONNECTED

    def _mqttc_connect(self, client: mqtt.Client, userdata, rc, other):
        self._stateext = self.STATE_CONNECTED
        result, mid = client.subscribe([(self.topic_push, 1), (self.topic_command, 1)])
        if result != mqtt.MQTT_ERR_SUCCESS:
            LOGGER.error("Failed to subscribe to topics")

    def _mqttc_disconnect(self, client: mqtt.Client, userdata, rc):
        self._stateext = (
            self.STATE_DISCONNECTED
            if self._stateext in (self.STATE_DISCONNECTING, self.STATE_DISCONNECTED)
            else self.STATE_RECONNECTING
        )
