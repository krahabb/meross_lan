"""
    meross_lan module interface to access Meross Cloud services
"""
from __future__ import annotations

from json import dumps as json_dumps, loads as json_loads
from logging import DEBUG, INFO, WARNING
from time import time
import typing

from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.event import async_call_later
import paho.mqtt.client as mqtt

from .const import (
    CONF_DEVICE_ID,
    CONF_KEY,
    CONF_PAYLOAD,
    CONF_PROFILE_ID,
    CONF_PROFILE_ID_LOCAL,
    DOMAIN,
    PARAM_CLOUDAPI_QUERY_DEVICELIST_TIMEOUT,
    PARAM_HEARTBEAT_PERIOD,
    PARAM_UNAVAILABILITY_TIMEOUT,
)
from .helpers import LOGGER, LOGGER_trap
from .merossclient import (
    MEROSSDEBUG,
    KeyType,
    build_payload,
    const as mc,
    get_default_arguments,
    get_replykey,
)
from .merossclient.cloudapi import (
    APISTATUS_TOKEN_ERRORS,
    CloudApiError,
    MerossCloudCredentials,
    MerossMQTTClient,
    async_cloudapi_devicelist,
    async_cloudapi_logout,
)

if typing.TYPE_CHECKING:
    from asyncio import TimerHandle
    from typing import Callable, ClassVar, Coroutine

    from homeassistant.core import HomeAssistant

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

    _KEY_STARTTIME = "__starttime"
    _KEY_REQUESTTIME = "__requesttime"

    hass: ClassVar[HomeAssistant]  # set on MerossApi init
    devices: ClassVar[dict[str, MerossDevice]] = {}

    _mqtt_is_connected = False

    def __init__(self, profile_id: str, key: str):
        self.profile_id = profile_id
        self.key = key
        self.mqttdevices: dict[str, MerossDevice] = {}
        self.mqttdiscovering: dict[str, dict] = {}
        self._unsub_discovery_callback: TimerHandle | None = None

    def shutdown(self):
        if self._unsub_discovery_callback is not None:
            self._unsub_discovery_callback.cancel()
            self._unsub_discovery_callback = None

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

            if device_id in self.devices:
                # we have the device registered but somehow it is not 'mqtt binded'
                # either it's configuration is ONLY_HTTP or it is paired to the
                # Meross cloud. In this case we shouldn't receive 'local' MQTT
                LOGGER_trap(
                    WARNING,
                    14400,
                    "MQTTProfile(%s): device(%s) not registered for MQTT handling",
                    self.profile_id,
                    self.devices[device_id].name,
                )
                return

            # lookout for any disabled/ignored entry
            hub_entry_present = False
            for config_entry in self.hass.config_entries.async_entries(DOMAIN):
                if config_entry.unique_id == device_id:
                    # entry already present...
                    # if config_entry.disabled_by == DOMAIN:
                    # we previously disabled this one due to extended anuavailability
                    # await self.hass.config_entries.async_set_disabled_by(config_entry.entry_id, None)
                    # skip discovery anyway
                    msg_reason = (
                        "disabled"
                        if config_entry.disabled_by is not None
                        else "ignored"
                        if config_entry.source == "ignore"
                        else "unknown"
                    )
                    LOGGER_trap(
                        INFO,
                        14400,
                        "MQTTProfile(%s): ignoring discovery for already configured device_id: %s (ConfigEntry is %s)",
                        self.profile_id,
                        device_id,
                        msg_reason,
                    )
                    return
                hub_entry_present |= config_entry.unique_id == DOMAIN
            # also skip discovered integrations waiting in HA queue
            for flow in self.hass.config_entries.flow.async_progress_by_handler(DOMAIN):
                flow_unique_id = flow.get("context", {}).get("unique_id")
                if flow_unique_id == device_id:
                    LOGGER_trap(
                        INFO,
                        14400,
                        "MQTTProfile(%s): ignoring discovery for device_id: %s (ConfigEntry is in progress)",
                        self.profile_id,
                        device_id,
                    )
                    return
                hub_entry_present |= flow_unique_id == DOMAIN

            if not hub_entry_present and self.profile_id is CONF_PROFILE_ID_LOCAL:
                await self.hass.config_entries.flow.async_init(
                    DOMAIN,
                    context={"source": "hub"},
                    data=None,
                )

            if (replykey := get_replykey(header, self.key)) is not self.key:
                LOGGER_trap(
                    WARNING,
                    300,
                    "Meross discovery key error for device_id: %s",
                    device_id,
                )
                if (
                    self.key is not None
                ):  # we're using a fixed key in discovery so ignore this device
                    return

            if (discovered := self.mqttdiscovering.get(device_id)) is None:
                # new device discovered: try to determine the capabilities
                await self.async_mqtt_publish(
                    device_id,
                    *get_default_arguments(mc.NS_APPLIANCE_SYSTEM_ALL),
                    replykey,
                )
                epoch = time()
                self.mqttdiscovering[device_id] = {
                    MQTTProfile._KEY_STARTTIME: epoch,
                    MQTTProfile._KEY_REQUESTTIME: epoch,
                }
                if self._unsub_discovery_callback is None:
                    self._unsub_discovery_callback = self.schedule_async_callback(
                        PARAM_UNAVAILABILITY_TIMEOUT + 2, self._async_discovery_callback
                    )
                return

            if header[mc.KEY_METHOD] == mc.METHOD_GETACK:
                namespace = header[mc.KEY_NAMESPACE]
                if namespace == mc.NS_APPLIANCE_SYSTEM_ALL:
                    discovered[mc.NS_APPLIANCE_SYSTEM_ALL] = message[mc.KEY_PAYLOAD]
                    await self.async_mqtt_publish(
                        device_id,
                        *get_default_arguments(mc.NS_APPLIANCE_SYSTEM_ABILITY),
                        replykey,
                    )
                    discovered[MQTTProfile._KEY_REQUESTTIME] = time()
                    return
                elif namespace == mc.NS_APPLIANCE_SYSTEM_ABILITY:
                    if discovered.get(mc.NS_APPLIANCE_SYSTEM_ALL) is None:
                        await self.async_mqtt_publish(
                            device_id,
                            *get_default_arguments(mc.NS_APPLIANCE_SYSTEM_ALL),
                            replykey,
                        )
                        discovered[MQTTProfile._KEY_REQUESTTIME] = time()
                        return
                    payload = message[mc.KEY_PAYLOAD]
                    payload.update(discovered[mc.NS_APPLIANCE_SYSTEM_ALL])
                    self.mqttdiscovering.pop(device_id)
                    await self.hass.config_entries.flow.async_init(
                        DOMAIN,
                        context={"source": DOMAIN},
                        data={
                            CONF_DEVICE_ID: device_id,
                            CONF_PAYLOAD: payload,
                            CONF_KEY: self.key,
                            CONF_PROFILE_ID: self.profile_id,
                        },
                    )
                    return

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

    def schedule_async_callback(
        self, delay: float, target: Callable[..., Coroutine], *args
    ) -> TimerHandle:
        @callback
        def _callback(_target, *_args):
            self.hass.async_create_task(_target(*_args))

        return self.hass.loop.call_later(delay, _callback, target, *args)

    def schedule_callback(self, delay: float, target: Callable, *args) -> TimerHandle:
        return self.hass.loop.call_later(delay, target, *args)

    async def _async_discovery_callback(self):
        """
        async task to keep alive the discovery process:
        activated when any device is initially detected
        this task is not renewed when the list of devices
        under 'discovery' is empty or these became stale
        """
        self._unsub_discovery_callback = None
        if len(discovering := self.mqttdiscovering) == 0:
            return

        epoch = time()
        for device_id, discovered in discovering.copy().items():
            if (
                epoch - discovered.get(MQTTProfile._KEY_STARTTIME, 0)
            ) > PARAM_HEARTBEAT_PERIOD:
                # stale entry...remove
                discovering.pop(device_id)
                continue
            if self._mqtt_is_connected and (
                (epoch - discovered.get(MQTTProfile._KEY_REQUESTTIME, 0))
                > PARAM_UNAVAILABILITY_TIMEOUT
            ):
                await self.async_mqtt_publish(
                    device_id,
                    *get_default_arguments(
                        mc.NS_APPLIANCE_SYSTEM_ABILITY
                        if mc.NS_APPLIANCE_SYSTEM_ALL in discovered
                        else mc.NS_APPLIANCE_SYSTEM_ALL
                    ),
                    self.key,
                )
                discovered[MQTTProfile._KEY_REQUESTTIME] = epoch

        if len(discovering):
            self._unsub_discovery_callback = self.schedule_async_callback(
                PARAM_UNAVAILABILITY_TIMEOUT + 2, self._async_discovery_callback
            )


class MerossMQTTProfile(MQTTProfile, MerossMQTTClient):

    _server: str
    _port: int

    def __init__(self, profile: MerossCloudProfile, mqttprofile_id, server, port):
        MerossMQTTClient.__init__(self, profile)
        MQTTProfile.__init__(self, mqttprofile_id, profile.key)
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
            self.hass.loop.call_soon(
                self.hass.async_add_executor_job, self._safe_connect
            )

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
