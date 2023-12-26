from __future__ import annotations

from base64 import b64encode
from collections import deque
from hashlib import md5
import json
import logging
import ssl
import threading
from time import monotonic, time
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


def generate_app_id():
    return md5(uuid4().hex.encode("utf-8")).hexdigest()


def parse_domain(domain: str):
    if (colon_index := domain.find(":")) != -1:
        return domain[0:colon_index], int(domain[colon_index + 1 :])
    else:
        return domain, mc.MQTT_DEFAULT_PORT


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


class MerossMQTTClient(mqtt.Client):
    STATE_CONNECTING = "connecting"
    STATE_CONNECTED = "connected"
    STATE_RECONNECTING = "reconnecting"
    STATE_DISCONNECTING = "disconnecting"
    STATE_DISCONNECTED = "disconnected"

    """
    Meross cloud traffic need to be rate-limited in order
    to prevent banning.
    Here the policy is pretty simple:
    the trasmission rate is limited by RATELIMITER_MINDELAY
    which poses a minimum interval between successive publish
    when a message is published with priority == True it is queued in front of
    any other 'non priority' mesage
    RATELIMITER_MAXQUEUE_PRIORITY sets a maximum number of priority messages
    to be queued: when overflow occurs, older priority messages are discarded
    """
    RATELIMITER_MINDELAY = 12
    RATELIMITER_MAXQUEUE = 5
    RATELIMITER_MAXQUEUE_PRIORITY = 0
    RATELIMITER_AVGPERIOD_DERATE = 0.1

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
        self._rl_lastpublish = monotonic() - self.RATELIMITER_MINDELAY
        self._rl_qeque: deque[tuple[str, str, bool | None]] = deque()
        self._rl_queue_length = 0
        self.rl_dropped = 0
        self.rl_avgperiod = 0.0
        if MEROSSDEBUG and MEROSSDEBUG.mqtt_client_log_enable:
            self.enable_logger(LOGGER)

    @property
    def rl_queue_length(self):
        return self._rl_queue_length

    @property
    def rl_queue_duration(self):
        return self._rl_queue_length * self.RATELIMITER_MINDELAY

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
        if self._stateext != self.STATE_DISCONNECTED:
            self._stateext = self.STATE_DISCONNECTING
            if mqtt.MQTT_ERR_NO_CONN == self.disconnect():
                self._stateext = self.STATE_DISCONNECTED

    def rl_publish(
        self, topic: str, payload: str, priority: bool | None = None
    ) -> mqtt.MQTTMessageInfo | bool:
        with self.lock:
            queuelen = len(self._rl_qeque)
            if queuelen == 0:
                now = monotonic()
                period = now - self._rl_lastpublish
                if period > self.RATELIMITER_MINDELAY:
                    self._rl_lastpublish = now
                    self.rl_avgperiod += self.RATELIMITER_AVGPERIOD_DERATE * (
                        period - self.rl_avgperiod
                    )
                    return super().publish(topic, payload)

            if priority is None:
                if queuelen >= self.RATELIMITER_MAXQUEUE:
                    # TODO: log dropped message
                    self.rl_dropped += 1
                    return False
                _queue_pos = queuelen

            elif priority:
                # priority messages are typically SET commands and we want them to be sent
                # asap. As far as this goes we cannot really queue a lot of these
                # else we'd loose responsivity. Moreover, device level meross_lan code
                # would 'timeout' a SET request without a timely response so, actual policy is to not
                # queue too many of these (we'll eventually discard the older ones)
                _queue_pos = 0
                for topic_payload_priority in self._rl_qeque:
                    if not topic_payload_priority[2]:
                        break
                    if _queue_pos == self.RATELIMITER_MAXQUEUE_PRIORITY:
                        # discard older 'priority' msg
                        self._rl_qeque.popleft()
                        self.rl_dropped += 1
                        queuelen -= 1
                        break
                    _queue_pos += 1

            else:
                # priority == False are still prioritized but less than priority == True
                # so they'll be queued in front of priority == None
                # actual meross_lan uses this priority for PUSH messages (not a real reason to do so)
                # also, we're not typically sending PUSH messages over cloud MQTT....
                _queue_pos = 0
                for topic_payload_priority in self._rl_qeque:
                    if topic_payload_priority[2] is None:
                        break
                    if _queue_pos == self.RATELIMITER_MAXQUEUE_PRIORITY:
                        # discard older 'priority' msg
                        self._rl_qeque.popleft()
                        self.rl_dropped += 1
                        queuelen -= 1
                        break
                    _queue_pos += 1

            self._rl_qeque.insert(_queue_pos, (topic, payload, priority))
            self._rl_queue_length = queuelen + 1
            return True

    def loop_misc(self):
        ret = super().loop_misc()
        if (ret == mqtt.MQTT_ERR_SUCCESS) and self._rl_queue_length:
            if self.lock.acquire(False):
                topic_payload_priority = None
                try:
                    queuelen = len(self._rl_qeque)
                    if queuelen > 0:
                        now = monotonic()
                        period = now - self._rl_lastpublish
                        if period > self.RATELIMITER_MINDELAY:
                            topic_payload_priority = self._rl_qeque.popleft()
                            self._rl_lastpublish = now
                            self.rl_avgperiod += self.RATELIMITER_AVGPERIOD_DERATE * (
                                period - self.rl_avgperiod
                            )
                            self._rl_queue_length = queuelen - 1
                    else:
                        self._rl_queue_length = 0
                finally:
                    self.lock.release()
                    if topic_payload_priority:
                        super().publish(
                            topic_payload_priority[0], topic_payload_priority[1]
                        )
        return ret

    def _mqttc_connect(self, client: mqtt.Client, userdata, rc, other):
        with self.lock:
            self._rl_qeque.clear()
            self._rl_queue_length = 0

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
