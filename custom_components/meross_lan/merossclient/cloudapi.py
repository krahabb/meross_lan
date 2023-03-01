from __future__ import annotations
from uuid import uuid4
from hashlib import md5
from base64 import b64encode
from time import time
from json import (
    dumps as json_dumps,
    loads as json_loads
)
import async_timeout
import aiohttp
import logging
import ssl
import threading

import paho.mqtt.client as mqtt

from . import (
    const as mc,
    MerossProtocolError,
    MEROSSDEBUG,
)



SECRET = "23x17ahWarFH6w29"

API_V1_URL = "https://iot.meross.com/v1"
API_LOGIN_PATH = "/Auth/Login"
API_LOGOUT_PATH = "/Profile/Logout"
API_DEVICELIST_PATH = "/Device/devList"

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
}
APISTATUS_TOKEN_ERRORS = {
    APISTATUS_TOKEN_INVALID,
    APISTATUS_TOKEN_ERROR,
    APISTATUS_TOKEN_EXPIRED,
}

LOGGER = logging.getLogger(__name__)

class MerossCloudCredentials(dict):

    @property
    def userid(self):
        return self[mc.KEY_USERID_]

    @property
    def email(self):
        return self[mc.KEY_EMAIL]

    @property
    def key(self):
        return self[mc.KEY_KEY]

    @property
    def token(self):
        return self.get(mc.KEY_TOKEN)

    @property
    def mqttpassword(self):
        return md5(f"{self.userid}{self.key}".encode("utf8")).hexdigest()


class MerossDeviceInfo(dict):

    @property
    def uuid(self):
        return self[mc.KEY_UUID]


class CloudApiError(MerossProtocolError):
    """
    signals an error when connecting to the public API endpoint
    """

    def __init__(self, response: dict):
        self.response = response
        self.apistatus = response.get(mc.KEY_APISTATUS)
        reason = APISTATUS_MAP.get(self.apistatus) # type: ignore
        if not reason:
            # 'info' sometimes carries useful msg
            reason = response.get(mc.KEY_INFO)
        if not reason:
            # fallback to raise the entire response
            reason = json_dumps(response)
        super().__init__(reason)


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
    with async_timeout.timeout(10):
        response = await (session or aiohttp.ClientSession()).post(
            url=API_V1_URL + urlpath,
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
        MEROSSDEBUG.cloudapi_login.__class__ = MerossCloudCredentials
        return MEROSSDEBUG.cloudapi_login

    response = await async_cloudapi_post(
        API_LOGIN_PATH, {"email": username, "password": password}, session=session
    )
    data = response[mc.KEY_DATA]
    if (mc.KEY_KEY in data) and (mc.KEY_TOKEN in data) and (mc.KEY_USERID_ in data):
        data.__class__ = MerossCloudCredentials
        return data
    raise CloudApiError(response)


async def async_cloudapi_devicelist(
    token: str, session: aiohttp.ClientSession | None = None
):
    if MEROSSDEBUG and MEROSSDEBUG.cloudapi_devicelist:
        return MEROSSDEBUG.cloudapi_devicelist
    response = await async_cloudapi_post(API_DEVICELIST_PATH, {}, token, session)
    return response[mc.KEY_DATA]


async def async_cloudapi_logout(
    token: str, session: aiohttp.ClientSession | None = None
):
    await async_cloudapi_post(API_LOGOUT_PATH, {}, token, session)


async def async_get_cloud_key(
    username: str, password: str, session: aiohttp.ClientSession | None = None
) -> str:

    credentials = await async_cloudapi_login(username, password, session)
    # everything good:
    # kindly invalidate login token so to not exhaust our pool...
    try:
        await async_cloudapi_logout(credentials.token, session)
    except:
        pass  # don't care if any failure here: we have the key anyway
    return credentials.key


class MerossMQTTClient(mqtt.Client):
    def __init__(self, credentials: MerossCloudCredentials):
        self.app_id = md5(uuid4().hex.encode('utf-8')).hexdigest()
        self.client_id = f"app:{self.app_id}"
        self.topic_command = f"/app/{credentials.userid}-{self.app_id}/subscribe"
        self.topic_push = f"/app/{credentials.userid}/subscribe"
        self.lock = threading.Lock()
        super().__init__(self.client_id, protocol=mqtt.MQTTv311)
        self.username_pw_set(credentials.userid, credentials.mqttpassword)
        self.tls_set(cert_reqs=ssl.CERT_REQUIRED, tls_version=ssl.PROTOCOL_TLS_CLIENT)
        self.on_connect = self._mqtt_connect
        self.suppress_exceptions = True
        if MEROSSDEBUG and MEROSSDEBUG.mqtt_client_log_enable:
            self.enable_logger(LOGGER)

    def _mqtt_connect(self, client: mqtt.Client, userdata, rc, other):
        result, mid = client.subscribe([
            (self.topic_push, 1),
            (self.topic_command, 1)
        ])
        if result != mqtt.MQTT_ERR_SUCCESS:
            LOGGER.error("Failed to subscribe to topics")

    #def _mqtt_subscribe(self, client: mqtt.Client, userdata, mid, granted_qos):
    #    pass
