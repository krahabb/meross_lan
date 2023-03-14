"""
    meross_lan module interface to access Meross Cloud services
"""
from __future__ import annotations
from logging import DEBUG
from time import time
import typing
from json import dumps as json_dumps, loads as json_loads

import paho.mqtt.client as mqtt

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.event import async_call_later

from .merossclient import (
    const as mc,
    KeyType,
    MEROSSDEBUG,
    build_payload,
)
from .merossclient.cloudapi import (
    APISTATUS_TOKEN_ERRORS,
    CloudApiError,
    MerossCloudCredentials,
    MerossMQTTClient,
    async_cloudapi_devicelist,
    async_cloudapi_logout,
)
from .helpers import (
    LOGGER,
)
from .const import (
    PARAM_CLOUDAPI_QUERY_DEVICELIST_TIMEOUT,
)

if typing.TYPE_CHECKING:
    from . import MerossApi
    from .meross_device import MerossDevice


KEY_DEVICELIST = "devList"
KEY_DEVICELIST_TIME = "devListTime"


class MQTTProfile:
    """
    Base abstract class representing a connection to an MQTT
    broker. Historically, MQTT support was only through MerossApi
    and the HA core MQTT broker. The introduction of Meross cloud
    connection has 'generalized' the concept of the MQTT broker.
    This interface is used by devices to actually send/receive
    MQTT messages (in place of the legacy approach using MerossApi)
    and represents a link to a broker (either through HA or a
    merosss cloud mqtt)
    """

    _mqtt_is_connected = False

    def __init__(self, hass: HomeAssistant, profile_id: str, key: str):
        self.hass = hass
        self.profile_id = profile_id
        self.key = key
        self.mqttdevices: dict[str, MerossDevice] = {}

    def attach(self, device: MerossDevice):
        assert device.device_id not in self.mqttdevices
        self.mqttdevices[device.device_id] = device
        if self._mqtt_is_connected:
            device.set_mqtt_connected()
        return self

    def detach(self, device: MerossDevice):
        assert device.device_id in self.mqttdevices
        self.mqttdevices.pop(device.device_id)

    async def async_mqtt_publish(
        self,
        device_id: str,
        namespace: str,
        method: str,
        payload: dict,
        key: KeyType = None,
        messageid: str | None = None,
    ):
        raise NotImplementedError()

    async def async_mqtt_message(self, msg):
        try:
            message = json_loads(msg.payload)
            header = message[mc.KEY_HEADER]
            device_id = header[mc.KEY_FROM].split("/")[2]
            if LOGGER.isEnabledFor(DEBUG):
                LOGGER.debug(
                    "MQTTProfile(%s): MQTT RECV device_id:(%s) method:(%s) namespace:(%s)",
                    self.profile_id,
                    device_id,
                    header[mc.KEY_METHOD],
                    header[mc.KEY_NAMESPACE],
                )
            if device_id in self.mqttdevices:
                self.mqttdevices[device_id].mqtt_receive(
                    header, message[mc.KEY_PAYLOAD]
                )
            # TODO: implement mqtt discovery like in our MerossApi
        except Exception as error:
            LOGGER.error(
                "MQTTProfile(%s) %s %s",
                self.profile_id,
                type(error).__name__,
                str(error),
            )

    @property
    def mqtt_is_connected(self):
        return self._mqtt_is_connected

    @callback
    def set_mqtt_connected(self):
        for device in self.mqttdevices.values():
            device.set_mqtt_connected()
        self._mqtt_is_connected = True

    @callback
    def set_mqtt_disconnected(self):
        for device in self.mqttdevices.values():
            device.set_mqtt_disconnected()
        self._mqtt_is_connected = False


class MerossMQTTProfile(MQTTProfile, MerossMQTTClient):

    _server: str
    _port: int

    def __init__(self, profile: MerossCloudProfile, mqttprofile_id, server, port):
        MerossMQTTClient.__init__(self, profile)
        MQTTProfile.__init__(self, profile.api.hass, mqttprofile_id, profile.key)
        self._server = server
        self._port = port
        self.user_data_set(self.hass)  # speedup hass lookup in callbacks
        self.on_message = self._mqtt_message
        self.on_subscribe = self._mqtt_subscribe
        self.on_disconnect = self._mqtt_disconnect

        if MEROSSDEBUG:

            def _random_disconnect(*_):
                if self.is_connected():
                    if MEROSSDEBUG.mqtt_random_disconnect():
                        LOGGER.debug(
                            "MerossMQTTProfile(%s) random disconnect",
                            self.profile_id,
                        )
                        self._safe_disconnect()
                else:
                    if MEROSSDEBUG.mqtt_random_connect():
                        LOGGER.debug(
                            "MerossMQTTProfile(%s) random connect",
                            self.profile_id,
                        )
                        self._safe_connect()
                self.hass.loop.call_soon_threadsafe(
                    async_call_later, self.hass, 60, _random_disconnect
                )

            async_call_later(self.hass, 60, _random_disconnect)

    def _safe_connect(self):
        with self.lock:
            try:
                if not self.is_connected():
                    self.connect(self._server, self._port)
                    self.loop_start()
            except Exception as error:
                LOGGER.debug(
                    "MerossMQTTProfile(%s) %s %s",
                    self.profile_id,
                    type(error).__name__,
                    str(error),
                )

    def _safe_disconnect(self):
        with self.lock:
            try:
                if self.is_connected():
                    self.disconnect()
                    self.loop_stop()
            except Exception as error:
                LOGGER.debug(
                    "MerossMQTTProfile(%s) %s %s",
                    self.profile_id,
                    type(error).__name__,
                    str(error),
                )

    def attach(self, device: "MerossDevice"):
        if len(self.mqttdevices) == 0:
            # we need to be sure the 'attach' process completed in the main loop
            # before all the connection process starts so the mqttdevices list
            # is set when the connection signal arrives
            self.hass.loop.call_soon(self.hass.async_add_executor_job, self._safe_connect)

        return super().attach(device)

    def detach(self, device: "MerossDevice"):
        super().detach(device)
        if not self.mqttdevices:
            self.hass.async_add_executor_job(self._safe_disconnect)

    def mqtt_publish(
        self,
        device_id: str,
        namespace: str,
        method: str,
        payload: dict,
        key: KeyType = None,
        messageid: str | None = None,
    ):
        def _publish():
            LOGGER.debug(
                "MerossMQTTProfile(%s): MQTT SEND device_id:(%s) method:(%s) namespace:(%s)",
                self.profile_id,
                device_id,
                method,
                namespace,
            )
            with self.lock:
                self.publish(
                    mc.TOPIC_REQUEST.format(device_id),
                    json_dumps(
                        build_payload(
                            namespace,
                            method,
                            payload,
                            key,
                            self.topic_command,
                            messageid,
                        )
                    ),
                )

        return self.hass.async_add_executor_job(_publish)

    async def async_mqtt_publish(
        self,
        device_id: str,
        namespace: str,
        method: str,
        payload: dict,
        key: KeyType = None,
        messageid: str | None = None,
    ):
        await self.mqtt_publish(device_id, namespace, method, payload, key, messageid)

    def _mqtt_message(self, client, userdata: HomeAssistant, msg: "mqtt.MQTTMessage"):
        userdata.create_task(self.async_mqtt_message(msg))

    def _mqtt_subscribe(self, client, userdata: HomeAssistant, mid, granted_qos):
        userdata.add_job(self.set_mqtt_connected)

    def _mqtt_disconnect(self, client, userdata: HomeAssistant, rc):
        userdata.add_job(self.set_mqtt_disconnected)


class MerossCloudProfile(MerossCloudCredentials):
    """
    Represents and manages a cloud account profile used to retrieve keys
    and/or to manage cloud mqtt connection(s)
    """

    def __init__(self, api: "MerossApi", data: dict):
        self.api = api
        self.mqttprofiles: dict[str, MerossMQTTProfile] = {}
        self.update(data)
        self._last_query_devices = data.get(KEY_DEVICELIST_TIME, 0)
        api.profiles[self.profile_id] = self

    @property
    def profile_id(self):
        return self.userid

    async def async_update_credentials(self, credentials: MerossCloudCredentials):
        assert self.userid == credentials.userid
        assert self.key == credentials.key
        if credentials.token != self.token:
            await self.async_release_token()
        self.update(credentials)

    async def async_release_token(self):
        if mc.KEY_TOKEN in self:
            if token := self.pop(mc.KEY_TOKEN):
                await async_cloudapi_logout(
                    token, async_get_clientsession(self.api.hass)
                )

    def need_query_devices(self):
        return (
            time() - self._last_query_devices
        ) > PARAM_CLOUDAPI_QUERY_DEVICELIST_TIMEOUT

    async def async_query_devices(self):
        error = None
        try:
            if token := self.token:
                self._last_query_devices = int(time())
                self[KEY_DEVICELIST] = await async_cloudapi_devicelist(
                    token, async_get_clientsession(self.api.hass)
                )
                self[KEY_DEVICELIST_TIME] = self._last_query_devices
                self.api.schedule_save_store()
            return
        except CloudApiError as clouderror:
            if clouderror.apistatus in APISTATUS_TOKEN_ERRORS:
                self.pop(mc.KEY_TOKEN, None)
            error = clouderror
        except Exception as e:
            error = e
        LOGGER.warning(
            "MerossCloudProfile(%s): %s %s in async_query_devices",
            self.profile_id,
            type(error).__name__,
            str(error),
        )

    async def async_check_query_devices(self):
        if self.need_query_devices():
            await self.async_query_devices()

    def attach(self, device: MerossDevice):
        fw = device.descriptor.firmware
        server = fw.get(mc.KEY_SERVER)
        port = fw.get(mc.KEY_PORT)
        mqttprofile_id = f"{self.profile_id}:{server}:{port}"
        if mqttprofile_id in self.mqttprofiles:
            mqttprofile = self.mqttprofiles[mqttprofile_id]
        else:
            mqttprofile = MerossMQTTProfile(self, mqttprofile_id, server, port)
            self.mqttprofiles[mqttprofile_id] = mqttprofile
        return mqttprofile.attach(device)
