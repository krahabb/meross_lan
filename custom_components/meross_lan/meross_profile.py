"""
    meross_lan module interface to access Meross Cloud services
"""
from __future__ import annotations

import abc
import asyncio
from contextlib import asynccontextmanager
from logging import DEBUG, INFO, WARNING
from time import time
import typing

from homeassistant.config_entries import SOURCE_INTEGRATION_DISCOVERY
from homeassistant.core import callback
from homeassistant.helpers import issue_registry, storage
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import (
    CONF_CHECK_FIRMWARE_UPDATES,
    CONF_DEVICE_ID,
    CONF_EMAIL,
    CONF_KEY,
    CONF_PASSWORD,
    CONF_PAYLOAD,
    CONF_PROTOCOL_MQTT,
    DOMAIN,
    ISSUE_CLOUD_TOKEN_EXPIRED,
    PARAM_CLOUDPROFILE_DELAYED_SAVE_TIMEOUT,
    PARAM_CLOUDPROFILE_QUERY_DEVICELIST_TIMEOUT,
    PARAM_CLOUDPROFILE_QUERY_LATESTVERSION_TIMEOUT,
    DeviceConfigType,
)
from .helpers import (
    ApiProfile,
    ConfigEntriesHelper,
    Loggable,
    datetime_from_epoch,
    schedule_async_callback,
    schedule_callback,
    versiontuple,
)
from .meross_device_hub import MerossDeviceHub
from .merossclient import (
    MEROSSDEBUG,
    HostAddress,
    MerossRequest,
    check_message_strict,
    const as mc,
    get_default_arguments,
    get_message_uuid,
    get_replykey,
    json_loads,
)
from .merossclient.cloudapi import (
    APISTATUS_TOKEN_ERRORS,
    CloudApiError,
    async_cloudapi_device_devlist,
    async_cloudapi_device_latestversion,
    async_cloudapi_hub_getsubdevices,
    async_cloudapi_login,
    async_cloudapi_logout,
)
from .merossclient.mqttclient import MerossMQTTAppClient, generate_app_id
from .sensor import MLSensor

if typing.TYPE_CHECKING:
    from typing import Final

    from homeassistant.components import mqtt as ha_mqtt
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.service_info.mqtt import MqttServiceInfo
    import paho.mqtt.client as paho_mqtt

    from . import MerossApi
    from .const import ProfileConfigType
    from .meross_device import MerossDevice, MerossDeviceDescriptor
    from .merossclient import (
        MerossHeaderType,
        MerossMessage,
        MerossMessageType,
        MerossPayloadType,
    )
    from .merossclient.cloudapi import (
        DeviceInfoType,
        LatestVersionType,
        MerossCloudCredentials,
        SubDeviceInfoType,
    )

    UuidType = str
    DeviceInfoDictType = dict[UuidType, DeviceInfoType]


class ConnectionSensor(MLSensor):
    STATE_DISCONNECTED: Final = "disconnected"
    STATE_CONNECTED: Final = "connected"
    STATE_QUEUING: Final = "queuing"
    STATE_DROPPING: Final = "dropping"

    class AttrDictType(typing.TypedDict):
        devices: dict[str, str]
        received: int
        published: int
        dropped: int
        queued: int
        queue_length: int

    ATTR_DEVICES: Final = "devices"
    ATTR_RECEIVED: Final = "received"
    ATTR_PUBLISHED: Final = "published"
    ATTR_DROPPED: Final = "dropped"
    ATTR_QUEUED: Final = "queued"
    ATTR_QUEUE_LENGTH: Final = "queue_length"

    manager: ApiProfile

    _attr_entity_category = MLSensor.EntityCategory.DIAGNOSTIC
    _attr_extra_state_attributes: AttrDictType
    _attr_state: str
    _attr_options = [STATE_DISCONNECTED, STATE_CONNECTED, STATE_QUEUING, STATE_DROPPING]

    def __init__(self, connection: MQTTConnection):
        self._attr_extra_state_attributes = {
            ConnectionSensor.ATTR_DEVICES: {},
            ConnectionSensor.ATTR_RECEIVED: 0,
            ConnectionSensor.ATTR_PUBLISHED: 0,
            ConnectionSensor.ATTR_DROPPED: 0,
            ConnectionSensor.ATTR_QUEUED: 0,
            ConnectionSensor.ATTR_QUEUE_LENGTH: 0,
        }
        super().__init__(
            connection.profile, None, connection.id, MLSensor.DeviceClass.ENUM
        )
        self.update_state(
            self.STATE_CONNECTED
            if connection.mqtt_is_connected
            else self.STATE_DISCONNECTED
        )

    @property
    def available(self):
        return True

    @property
    def options(self) -> list[str] | None:
        return self._attr_options

    def set_unavailable(self):
        raise NotImplementedError

    # interface: self
    def add_device(self, device: MerossDevice):
        self._attr_extra_state_attributes[ConnectionSensor.ATTR_DEVICES][
            device.id
        ] = device.name
        self.flush_state()

    def remove_device(self, device: MerossDevice):
        self._attr_extra_state_attributes[ConnectionSensor.ATTR_DEVICES].pop(
            device.id, None
        )
        self.flush_state()

    def inc_counter(self, attr_name: str):
        self._attr_extra_state_attributes[attr_name] += 1
        self.flush_state()

    def inc_counter_with_state(self, attr_name: str, state: str):
        self._attr_extra_state_attributes[attr_name] += 1
        self._attr_state = state
        self.flush_state()

    def inc_queued(self, queue_length: int):
        self._attr_extra_state_attributes[ConnectionSensor.ATTR_QUEUED] += 1
        self._attr_extra_state_attributes[
            ConnectionSensor.ATTR_QUEUE_LENGTH
        ] = queue_length
        self._attr_state = ConnectionSensor.STATE_QUEUING
        self.flush_state()


class _MQTTTransaction:
    """Context for pending MQTT publish(es) waiting for responses.
    This will allow to synchronize message request-response flow on MQTT
    """

    __slots__ = (
        "mqtt_connection",
        "device_id",
        "namespace",
        "messageid",
        "method",
        "request_time",
        "response_future",
    )

    def __init__(
        self,
        mqtt_connection: MQTTConnection,
        device_id: str,
        request: MerossMessage,
    ):
        self.mqtt_connection = mqtt_connection
        self.device_id = device_id
        self.namespace = request.namespace
        self.messageid = request.messageid
        self.method = request.method
        self.request_time = time()
        self.response_future = asyncio.get_running_loop().create_future()
        mqtt_connection._mqtt_transactions[request.messageid] = self

    def cancel(self):
        self.mqtt_connection.log(
            DEBUG,
            "cancelling mqtt transaction on %s %s (messageId: %s, device_id: %s)",
            self.method,
            self.namespace,
            self.messageid,
            self.device_id,
        )
        self.response_future.cancel()
        self.mqtt_connection._mqtt_transactions.pop(self.messageid, None)


class MQTTConnection(Loggable):
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

    _MQTT_DROP = "DROP"
    _MQTT_QUEUE = "QUEUE"
    _MQTT_PUBLISH = "PUBLISH"
    _MQTT_RECV = "RECV"

    DEFAULT_RESPONSE_TIMEOUT = 5

    SESSION_HANDLERS: typing.Mapping[
        str,
        typing.Callable[
            [MQTTConnection, str, MerossHeaderType, MerossPayloadType],
            typing.Awaitable[bool],
        ],
    ] = {}

    broker: HostAddress

    __slots__ = (
        "profile",
        "broker",
        "topic_response",
        "mqttdevices",
        "mqttdiscovering",
        "namespace_handlers",
        "sensor_connection",
        "_mqtt_transactions",
        "_mqtt_is_connected",
    )

    def __init__(
        self,
        profile: MerossCloudProfile | MerossApi,
        connection_id: str,
        broker: HostAddress,
        topic_response: str,
    ):
        self.profile: Final = profile
        self.broker = broker
        self.topic_response: Final = topic_response
        self.mqttdevices: Final[dict[str, MerossDevice]] = {}
        self.mqttdiscovering: Final[set[str]] = set()
        self.namespace_handlers = self.SESSION_HANDLERS
        self.sensor_connection = None
        self._mqtt_transactions: Final[dict[str, _MQTTTransaction]] = {}
        self._mqtt_is_connected = False
        super().__init__(connection_id, logger=profile)
        if profile.create_diagnostic_entities:
            self.create_diagnostic_entities()

    async def async_shutdown(self):
        for mqtt_transaction in list(self._mqtt_transactions.values()):
            mqtt_transaction.cancel()
        self.mqttdiscovering.clear()
        for device in self.mqttdevices.values():
            device.mqtt_detached()
        self.mqttdevices.clear()
        await self.async_destroy_diagnostic_entities()

    # interface: self
    @property
    @abc.abstractmethod
    def is_cloud_connection(self):
        raise NotImplementedError()

    @property
    def allow_mqtt_publish(self):
        return self.profile.allow_mqtt_publish

    @property
    def mqtt_is_connected(self):
        return self._mqtt_is_connected

    def attach(self, device: MerossDevice):
        assert device.id not in self.mqttdevices
        self.mqttdevices[device.id] = device
        device.mqtt_attached(self)
        if sensor_connection := self.sensor_connection:
            sensor_connection.add_device(device)

    def detach(self, device: MerossDevice):
        device_id = device.id
        assert device_id in self.mqttdevices
        for mqtt_transaction in list(self._mqtt_transactions.values()):
            if mqtt_transaction.device_id == device_id:
                mqtt_transaction.cancel()
        device.mqtt_detached()
        self.mqttdevices.pop(device_id)
        if sensor_connection := self.sensor_connection:
            sensor_connection.remove_device(device)

    def create_diagnostic_entities(self):
        if not self.sensor_connection:
            self.sensor_connection = ConnectionSensor(self)
            if self.mqttdevices:
                _add_device = self.sensor_connection.add_device
                for device in self.mqttdevices.values():
                    _add_device(device)

    async def async_destroy_diagnostic_entities(self):
        if sensor_connection := self.sensor_connection:
            self.sensor_connection = None
            sensor_connection.manager.entities.pop(sensor_connection.id)
            if sensor_connection._hass_connected:
                await sensor_connection.async_remove()
            await sensor_connection.async_shutdown()


    @typing.final
    def mqtt_publish(
        self,
        device_id: str,
        request: MerossMessage,
    ):
        return ApiProfile.hass.async_create_task(
            self.async_mqtt_publish(device_id, request)
        )

    @typing.final
    async def async_mqtt_publish(
        self,
        device_id: str,
        request: MerossMessage,
    ) -> MerossMessageType | None:
        if request.method in mc.METHOD_ACK_MAP.keys():
            transaction = _MQTTTransaction(self, device_id, request)
        else:
            transaction = None
        try:
            if self.isEnabledFor(DEBUG):
                self.log(
                    DEBUG,
                    "MQTT PUBLISH %s %s (device_id: %s, messageId: %s)",
                    request.method,
                    request.namespace,
                    device_id,
                    request.messageid,
                )
            profile = self.profile
            if profile.trace_file:
                profile.trace(
                    time(),
                    request.payload,
                    request.namespace,
                    request.method,
                    CONF_PROTOCOL_MQTT,
                    profile.TRACE_TX,
                )
            _mqtt_tx_code, timeout = await self._async_mqtt_publish(device_id, request)
            if transaction:
                if _mqtt_tx_code is self._MQTT_DROP:
                    transaction.cancel()
                    return None
                try:
                    return await asyncio.wait_for(transaction.response_future, timeout)
                except Exception as exception:
                    self.log_exception(
                        DEBUG,
                        exception,
                        "waiting for MQTT reply to %s %s (device_id: %s, messageId: %s)",
                        request.method,
                        request.namespace,
                        device_id,
                        request.messageid,
                    )
                    return None
                finally:
                    self._mqtt_transactions.pop(transaction.messageid, None)

        except Exception as exception:
            self.log(
                DEBUG,
                "%s(%s) in _async_mqtt_publish device_id:(%s) method:(%s) namespace:(%s)",
                exception.__class__.__name__,
                str(exception),
                device_id,
                request.method,
                request.namespace,
            )
            if transaction:
                transaction.cancel()
        return None

    @typing.final
    async def async_mqtt_message(
        self, mqtt_msg: ha_mqtt.ReceiveMessage | paho_mqtt.MQTTMessage | MqttServiceInfo
    ):
        with self.exception_warning("async_mqtt_message"):
            if sensor_connection := self.sensor_connection:
                sensor_connection.inc_counter(ConnectionSensor.ATTR_RECEIVED)
            mqtt_payload = mqtt_msg.payload
            message: MerossMessageType = json_loads(
                mqtt_payload
                if type(mqtt_payload) is str
                else mqtt_payload.decode("utf-8")  # type: ignore
            )
            header = message[mc.KEY_HEADER]
            device_id = get_message_uuid(header)
            namespace = header[mc.KEY_NAMESPACE]
            method = header[mc.KEY_METHOD]
            messageid = header[mc.KEY_MESSAGEID]
            payload = message[mc.KEY_PAYLOAD]

            profile = self.profile

            if self.isEnabledFor(DEBUG):
                self.log(
                    DEBUG,
                    "MQTT RECV %s %s (device_id: %s, messageId: %s)",
                    method,
                    namespace,
                    device_id,
                    messageid,
                )
            if profile.trace_file:
                profile.trace(
                    time(),
                    payload,
                    namespace,
                    method,
                    CONF_PROTOCOL_MQTT,
                    profile.TRACE_RX,
                )

            if messageid in self._mqtt_transactions:
                mqtt_transaction = self._mqtt_transactions[messageid]
                if mqtt_transaction.namespace == namespace:
                    self._mqtt_transactions.pop(messageid, None)
                    mqtt_transaction.response_future.set_result(message)
            else:
                # special session management: cloud connections would
                # behave differently than the local MQTT. Their behavior
                # will definitevely be set in the dynamic/custom message handlers
                # implemented in the derived MQTTConnections
                if namespace in self.namespace_handlers:
                    if await self.namespace_handlers[namespace](
                        self, device_id, header, payload
                    ):
                        # session management has already taken care of everything
                        return

            if device := ApiProfile.devices.get(device_id):
                if device._mqtt_connection == self:
                    device.mqtt_receive(header, payload)
                    return
                # we have the device registered but somehow it is not 'mqtt binded'
                # either it's configuration is ONLY_HTTP or it is paired to
                # another profile. In this case we could automagically fix this (see later)
                self.log(
                    WARNING,
                    "device(%s) not registered for MQTT handling on this profile",
                    device.name,
                    timeout=14400,
                )
                # all in all (device._mqtt_connection != self) when
                # - the device is configured for HTTP only (_mqtt_connection will be null anyway)
                # - the device is registered on another profile
                if device._profile != self.profile:
                    # the device could be HTTP only but we want to at
                    # least link the correct profile
                    if self.profile.try_link(device):
                        # keep checking MQTT proto is allowed at the device level
                        if device._mqtt_connection == self:
                            device.mqtt_receive(header, payload)
                return

            # the device is not configured: proceed to discovery in case
            if device_id in self.mqttdiscovering:
                return

            key = profile.key
            if get_replykey(header, key) is not key:
                self.log(
                    WARNING,
                    "discovery key error for device_id: %s",
                    device_id,
                    timeout=300,
                )
                if key is not None:
                    return

            # lookout for any disabled/ignored entry
            config_entries_helper = ConfigEntriesHelper(ApiProfile.hass)
            if (
                (self.profile is ApiProfile.api)
                and (not config_entries_helper.get_config_entry(DOMAIN))
                and (not config_entries_helper.get_config_flow(DOMAIN))
            ):
                # not really needed but we would like to always have the
                # MQTT hub entry in case so if the user removed that..retrigger
                await ApiProfile.hass.config_entries.flow.async_init(
                    DOMAIN,
                    context={"source": "hub"},
                    data=None,
                )

            if config_entry := config_entries_helper.get_config_entry(device_id):
                # entry already present...skip discovery
                self.log(
                    INFO,
                    "ignoring MQTT discovery for already configured device_id: %s (ConfigEntry is %s)",
                    device_id,
                    "disabled"
                    if config_entry.disabled_by
                    else "ignored"
                    if config_entry.source == "ignore"
                    else "unknown",
                    timeout=14400,  # type: ignore
                )
                return

            # also skip discovered integrations waiting in HA queue
            if config_entries_helper.get_config_flow(device_id):
                self.log(
                    DEBUG,
                    "ignoring discovery for device_id: %s (ConfigFlow is in progress)",
                    device_id,
                    timeout=14400,  # type: ignore
                )
                return

            await self.async_try_discovery(device_id)

    async def async_identify_device(self, device_id: str, key: str) -> DeviceConfigType:
        """
        Sends an ns_all and ns_ability GET requests encapsulated in an ns_multiple
        to speed up things. Raises exception in case of error
        """
        topic_response = self.topic_response
        response = await self.async_mqtt_publish(
            device_id,
            MerossRequest(
                key,
                mc.NS_APPLIANCE_CONTROL_MULTIPLE,
                mc.METHOD_SET,
                {
                    mc.KEY_MULTIPLE: [
                        MerossRequest(
                            key,
                            *get_default_arguments(mc.NS_APPLIANCE_SYSTEM_ALL),
                            topic_response,
                        ),
                        MerossRequest(
                            key,
                            *get_default_arguments(mc.NS_APPLIANCE_SYSTEM_ABILITY),
                            topic_response,
                        ),
                    ]
                },
                topic_response,
            ),
        )

        if not response:
            raise Exception("No response")

        try:
            # optimistically start considering valid response.
            # only investigate the response if this doesn't work
            multiple_response: list[MerossMessageType] = response[mc.KEY_PAYLOAD][
                mc.KEY_MULTIPLE
            ]
            # this syntax ensures both the responses are the expected ones
            return {
                CONF_DEVICE_ID: device_id,
                CONF_PAYLOAD: {
                    mc.KEY_ALL: multiple_response[0][mc.KEY_PAYLOAD][mc.KEY_ALL],
                    mc.KEY_ABILITY: multiple_response[1][mc.KEY_PAYLOAD][
                        mc.KEY_ABILITY
                    ],
                },
                CONF_KEY: key,
            }
        except KeyError as error:
            # formally checks the message and raises a typed except
            check_message_strict(response)
            # else go with the wind
            raise error

    async def async_try_discovery(self, device_id: str):
        """
        Tries device identification and starts a flow if succeded returning
        the FlowResult. Returns None if anything fails for whatever reason.
        """
        result = None
        self.mqttdiscovering.add(device_id)
        with self.exception_warning(
            "trying discover for device id:%s", device_id, timeout=14400
        ):
            result = await ApiProfile.hass.config_entries.flow.async_init(
                DOMAIN,
                context={"source": SOURCE_INTEGRATION_DISCOVERY},
                data=await self.async_identify_device(device_id, self.profile.key),
            )
        self.mqttdiscovering.remove(device_id)
        return result

    def _mqtt_transactions_clean(self):
        if self._mqtt_transactions:
            # check and cleanup stale transactions
            epoch = time()
            for transaction in list(self._mqtt_transactions.values()):
                if (epoch - transaction.request_time) > 15:
                    transaction.cancel()

    @abc.abstractmethod
    async def _async_mqtt_publish(
        self,
        device_id: str,
        request: MerossMessage,
    ) -> tuple[str, int]:
        """
        Actually sends the message to the transport. On return gives
        (status_code, timeout) with the expected timeout-to-reply depending
        on the queuing system in place (MerossMQTTConnection/paho client).
        Should raise an exception when the message could not be sent
        """
        raise NotImplementedError()

    @callback
    def _mqtt_connected(self):
        """called when the underlying mqtt.Client connects to the broker"""
        for device in self.mqttdevices.values():
            device.mqtt_connected()
        self._mqtt_is_connected = True
        if sensor_connection := self.sensor_connection:
            sensor_connection.update_state(ConnectionSensor.STATE_CONNECTED)

    @callback
    def _mqtt_disconnected(self):
        """called when the underlying mqtt.Client disconnects from the broker"""
        for device in self.mqttdevices.values():
            device.mqtt_disconnected()
        self._mqtt_is_connected = False
        if sensor_connection := self.sensor_connection:
            sensor_connection.update_state(ConnectionSensor.STATE_DISCONNECTED)

    @callback
    def _mqtt_published(self):
        """called when the underlying mqtt.Client successfully publishes a message"""
        if sensor_connection := self.sensor_connection:
            sensor_connection.inc_counter(ConnectionSensor.ATTR_PUBLISHED)

    async def _handle_Appliance_System_Online(
        self, device_id: str, header: MerossHeaderType, payload: MerossPayloadType
    ):
        """
        This is likely sent by the session management layer on the Meross brokers
        to notify the app of the device connection state. We then intercept
        this message which is not intended for the device though and act accordingly
        here at our 'session management state'. At any rate, this will be set to be
        handled in every MQTTConnection (cloud, local) so we process even messages
        originated from the device itself
        """
        if header[mc.KEY_METHOD] == mc.METHOD_PUSH:
            status = payload[mc.KEY_ONLINE].get(mc.KEY_STATUS)
            if status == mc.STATUS_ONLINE:
                # the device is now online on this connection: tell the pipe to continue processing
                # This will in turn (eventually) link the device to the current profile/connection
                # (if not already) and online it since it will receive a 'fresh' MQTT
                return False
        # any other condition will instruct the message pipe
        # to abort processing since the device is not online or we don't
        # understand this message
        return True


class MerossMQTTConnection(MQTTConnection, MerossMQTTAppClient):
    _MSG_PRIORITY_MAP = {
        mc.METHOD_SET: True,
        mc.METHOD_PUSH: False,
        mc.METHOD_GET: None,
    }

    # here we're acrobatically slottizing MerossMQTTAppClient
    # since it cannot be slotted itself leading to multiple inheritance
    # "forbidden" slots

    __slots__ = (
        "_future_connected",
        "_lock_state",
        "_lock_queue",
        "_rl_lastpublish",
        "_rl_qeque",
        "_rl_queue_length",
        "_rl_dropped",
        "_rl_avgperiod",
        "_stateext",
        "_unsub_random_disconnect",
    )

    def __init__(
        self, profile: MerossCloudProfile, connection_id: str, broker: HostAddress
    ):
        MerossMQTTAppClient.__init__(self, profile.config, profile.app_id)
        MQTTConnection.__init__(
            self, profile, connection_id, broker, self.topic_command
        )
        self.user_data_set(ApiProfile.hass)  # speedup hass lookup in callbacks
        self.on_connect = self._mqttc_connect
        self.on_disconnect = self._mqttc_disconnect
        self.on_message = self._mqttc_message
        self.on_publish = self._mqttc_publish

        if MEROSSDEBUG:

            @callback
            def _random_disconnect():
                if self.state_inactive:
                    if MEROSSDEBUG.mqtt_random_connect():
                        self.log(DEBUG, "random connect")
                        self.safe_start(self.broker)
                else:
                    if MEROSSDEBUG.mqtt_random_disconnect():
                        self.log(DEBUG, "random disconnect")
                        self.safe_stop()
                self._unsub_random_disconnect = schedule_callback(
                    ApiProfile.hass, 60, _random_disconnect
                )

            self._unsub_random_disconnect = schedule_callback(
                ApiProfile.hass, 60, _random_disconnect
            )
        else:
            self._unsub_random_disconnect = None

    # interface: MQTTConnection
    async def async_shutdown(self):
        if self._unsub_random_disconnect:
            self._unsub_random_disconnect.cancel()
            self._unsub_random_disconnect = None
        await self.schedule_disconnect_async()
        await super().async_shutdown()

    @property
    def is_cloud_connection(self):
        return True

    async def _async_mqtt_publish(
        self,
        device_id: str,
        request: MerossMessage,
    ) -> tuple[str, int]:
        return await ApiProfile.hass.async_add_executor_job(
            self._publish, device_id, request
        )

    @callback
    def _mqtt_published(self):
        if sensor_connection := self.sensor_connection:
            queue_length = self.rl_queue_length
            # queue_length and dropped are exactly calculated
            # inside our MerossMQTTClient so we'll update/force
            # the sensor with 'real' values here..just to be sure
            # this is especially true for 'dropped' since
            # the client itself could drop packets at any time
            # from its (de)queue
            sensor_connection._attr_extra_state_attributes[
                ConnectionSensor.ATTR_QUEUE_LENGTH
            ] = queue_length
            sensor_connection._attr_extra_state_attributes[
                ConnectionSensor.ATTR_DROPPED
            ] = self.rl_dropped
            sensor_connection._attr_extra_state_attributes[
                ConnectionSensor.ATTR_PUBLISHED
            ] += 1
            if self.mqtt_is_connected and not queue_length:
                # enforce the state eventually cancelling queued, dropped...
                sensor_connection._attr_state = ConnectionSensor.STATE_CONNECTED
            sensor_connection.flush_state()

    # interface: self
    def schedule_connect_async(self, future: asyncio.Future | None = None):
        # even if safe_connect should be as fast as possible and thread-safe
        # we still might incur some contention with thread stop/restart
        # so we delegate its call to an executor
        return ApiProfile.hass.async_add_executor_job(
            self.safe_start, self.broker, future
        )

    def schedule_disconnect_async(self):
        # same as connect. safe_disconnect should be even faster and less
        # contending but...
        return ApiProfile.hass.async_add_executor_job(self.safe_stop)

    def _publish(self, device_id: str, request: MerossMessage) -> tuple[str, int]:
        """
        this function runs in an executor
        Beware when calling HA api's (like when we want to update sensors)
        """
        if not self.allow_mqtt_publish:
            raise Exception("MQTT publishing is not allowed for this profile")

        ret = self.rl_publish(
            mc.TOPIC_REQUEST.format(device_id),
            request.to_string(),
            MerossMQTTConnection._MSG_PRIORITY_MAP[request.method],
        )
        if ret is False:
            if sensor_connection := self.sensor_connection:
                ApiProfile.hass.loop.call_soon_threadsafe(
                    sensor_connection.inc_counter_with_state,
                    ConnectionSensor.ATTR_DROPPED,
                    ConnectionSensor.STATE_DROPPING,
                )
            if self.isEnabledFor(DEBUG):
                self.log(
                    DEBUG,
                    "MQTT DROP %s %s (device_id: %s, messageId: %s)",
                    request.method,
                    request.namespace,
                    device_id,
                    request.messageid,
                )
            return (self._MQTT_DROP, 0)
        if ret is True:
            if sensor_connection := self.sensor_connection:
                ApiProfile.hass.loop.call_soon_threadsafe(
                    sensor_connection.inc_queued,
                    self.rl_queue_length,
                )
            if self.isEnabledFor(DEBUG):
                self.log(
                    DEBUG,
                    "MQTT QUEUE %s %s (device_id: %s, messageId: %s)",
                    request.method,
                    request.namespace,
                    device_id,
                    request.messageid,
                )
            return (
                self._MQTT_QUEUE,
                self.rl_queue_duration + self.DEFAULT_RESPONSE_TIMEOUT,
            )
        return (self._MQTT_PUBLISH, self.DEFAULT_RESPONSE_TIMEOUT)

    # paho mqtt calbacks
    def _mqttc_connect(self, client, userdata: HomeAssistant, rc, other):
        MerossMQTTAppClient._mqttc_connect(self, client, userdata, rc, other)
        userdata.add_job(self._mqtt_connected)

    def _mqttc_disconnect(self, client, userdata: HomeAssistant, rc):
        MerossMQTTAppClient._mqttc_disconnect(self, client, userdata, rc)
        userdata.add_job(self._mqtt_disconnected)

    def _mqttc_message(
        self, client, userdata: HomeAssistant, msg: paho_mqtt.MQTTMessage
    ):
        userdata.create_task(self.async_mqtt_message(msg))

    def _mqttc_publish(self, client, userdata: HomeAssistant, mid):
        userdata.add_job(self._mqtt_published)


MerossMQTTConnection.SESSION_HANDLERS = {
    mc.NS_APPLIANCE_SYSTEM_ONLINE: MQTTConnection._handle_Appliance_System_Online,
}


class MerossCloudProfileStoreType(typing.TypedDict):
    appId: str
    deviceInfo: DeviceInfoDictType
    deviceInfoTime: float
    latestVersion: list[LatestVersionType]
    latestVersionTime: float
    token: str | None
    tokenRequestTime: float


class MerossCloudProfileStore(storage.Store[MerossCloudProfileStoreType]):
    VERSION = 1

    def __init__(self, profile_id: str):
        super().__init__(
            ApiProfile.hass,
            MerossCloudProfileStore.VERSION,
            f"{DOMAIN}.profile.{profile_id}",
        )


class MerossCloudProfile(ApiProfile):
    """
    Represents and manages a cloud account profile used to retrieve keys
    and/or to manage cloud mqtt connection(s)
    """

    KEY_APP_ID: Final = "appId"
    KEY_DEVICE_INFO: Final = "deviceInfo"
    KEY_DEVICE_INFO_TIME: Final = "deviceInfoTime"
    KEY_SUBDEVICE_INFO: Final = "__subDeviceInfo"
    KEY_LATEST_VERSION: Final = "latestVersion"
    KEY_LATEST_VERSION_TIME: Final = "latestVersionTime"
    KEY_TOKEN_REQUEST_TIME: Final = "tokenRequestTime"

    config: ProfileConfigType
    _data: MerossCloudProfileStoreType

    __slots__ = (
        "_data",
        "_store",
        "_unsub_polling_query_device_info",
        "_device_info_time",
    )

    def __init__(self, config_entry: ConfigEntry):
        super().__init__(config_entry.data[mc.KEY_USERID_], config_entry, "profile")
        self._store = MerossCloudProfileStore(self.id)
        self._unsub_polling_query_device_info: asyncio.TimerHandle | None = None

    async def async_start(self):
        """
        Performs 'cold' initialization of the profile by checking
        if we need to update the device_info and eventually start the
        unknown devices discovery.
        We'll eventually setup the mqtt listeners in case our
        configured devices don't match the profile list. This usually means
        the user has binded a new device and we need to 'discover' it.
        """
        if data := await self._store.async_load():
            self._data = data
            if self.KEY_APP_ID not in data:
                data[self.KEY_APP_ID] = generate_app_id()
            if not isinstance(data.get(self.KEY_DEVICE_INFO), dict):
                data[self.KEY_DEVICE_INFO] = {}
            self._device_info_time = data.get(self.KEY_DEVICE_INFO_TIME, 0.0)
            if not isinstance(self._device_info_time, float):
                data[self.KEY_DEVICE_INFO_TIME] = self._device_info_time = 0.0
            if not isinstance(data.get(self.KEY_LATEST_VERSION), list):
                data[self.KEY_LATEST_VERSION] = []
            if self.KEY_LATEST_VERSION_TIME not in data:
                data[self.KEY_LATEST_VERSION_TIME] = 0.0
            if self.KEY_TOKEN_REQUEST_TIME not in data:
                data[self.KEY_TOKEN_REQUEST_TIME] = 0.0

            if mc.KEY_TOKEN not in data:
                # the token would be auto-refreshed when needed in
                # _async_token_manager but we'd eventually need
                # to just setup the issue registry in case we're
                # not configured to automatically refresh
                await self._async_token_missing(True)
        else:
            self._device_info_time = 0.0
            data: MerossCloudProfileStoreType | None = {
                self.KEY_APP_ID: generate_app_id(),
                mc.KEY_TOKEN: self.config[mc.KEY_TOKEN],
                self.KEY_DEVICE_INFO: {},
                self.KEY_DEVICE_INFO_TIME: 0.0,
                self.KEY_LATEST_VERSION: [],
                self.KEY_LATEST_VERSION_TIME: 0.0,
                self.KEY_TOKEN_REQUEST_TIME: 0.0,
            }
            self._data = data

        # compute the next cloud devlist query and setup the scheduled callback
        next_query_epoch = (
            self._device_info_time + PARAM_CLOUDPROFILE_QUERY_DEVICELIST_TIMEOUT
        )
        next_query_delay = next_query_epoch - time()
        if next_query_delay < 5:
            # we'll give some breath to the init process
            next_query_delay = 5
        """REMOVE
        # the device_info refresh did not kick in or failed
        # for whatever reason. We just scan the device_info
        # we have and setup the polling
        device_info_unknown = [
            device_info
            for device_id, device_info in data[
                MerossCloudProfile.KEY_DEVICE_INFO
            ].items()
            if device_id not in ApiProfile.devices
        ]
        if len(device_info_unknown):
            await self._process_device_info_unknown(device_info_unknown)
        """
        """REMOVE
        with self._cloud_token_exception_manager("async_cloudapi_deviceinfo") as token:
            if token is not None:
                for device_id, device_info in self[self.KEY_DEVICE_INFO].items():
                    _data = await async_cloudapi_device_devextrainfo(
                        token, device_id, async_get_clientsession(ApiProfile.hass)
                    )
                    self.log(
                        DEBUG,
                        "Device/devExtraInfo(%s): %s",
                        device_id,
                        json_dumps(_data),
                    )
        """
        assert self._unsub_polling_query_device_info is None
        self._unsub_polling_query_device_info = schedule_async_callback(
            ApiProfile.hass,
            next_query_delay,
            self._async_polling_query_device_info,
        )

    async def async_shutdown(self):
        ApiProfile.profiles[self.id] = None
        if self._unsub_polling_query_device_info:
            self._unsub_polling_query_device_info.cancel()
            self._unsub_polling_query_device_info = None
        await super().async_shutdown()

    # interface: EntityManager
    async def entry_update_listener(self, hass, config_entry: ConfigEntry):
        config = config_entry.data
        await self.async_update_credentials(config)  # type: ignore
        await super().entry_update_listener(hass, config_entry)

    # interface: ApiProfile
    def attach_mqtt(self, device: MerossDevice):
        if device.id not in self._data[self.KEY_DEVICE_INFO]:
            self.log(
                WARNING,
                "cannot connect MQTT for MerossDevice(%s): it does not belong to the current profile",
                device.name,
            )
            return

        with self.exception_warning("attach_mqtt"):
            mqttconnection = self._get_mqttconnection(device.mqtt_broker)
            mqttconnection.attach(device)
            if mqttconnection.state_inactive:
                mqttconnection.schedule_connect_async()

    # interface: self
    @property
    def app_id(self):
        return self._data[self.KEY_APP_ID]

    @property
    def token(self):
        return self._data.get(mc.KEY_TOKEN)

    def device_is_registered(self, descriptor: MerossDeviceDescriptor):
        """extensive check that the device is 'really' binded to the profile"""
        # this check, in a 'goldylock' scenario whould be pretty simple:
        # check that the device userId matches the profile Id since they represent
        # the same info. But when we rebind devices in the wild, the userId
        # in the device become 'untrustable' since you could set any value while pairing.
        # We'll then apply a 'best-effort' approach to verify if the device is (still)
        # binded by veryifing multiple conditions
        if descriptor.userId != self.id:
            # of course they need to match!
            return False
        uuid = descriptor.uuid
        if uuid not in self._data[self.KEY_DEVICE_INFO]:
            # the cloud profile doesn't know anything of this device.
            # it is for sure not binded (or we haven't fresh device_info)
            # TODO: think about it because our cloud profile device_info only
            # gets refreshed in 24h. This would fail the logic if the device is added in HA
            # before we update the info (when the user initially binds it?)
            return False
        # now it appears the device belongs to the profile, but since the device could
        # be mqtt-rebinded at any time without unregistering from the cloud profile
        # we also have to check consistency in the device configured brokers
        device_brokers = descriptor.brokers
        device_info = self._data[self.KEY_DEVICE_INFO][uuid]
        if domain := device_info.get(mc.KEY_DOMAIN):
            broker = HostAddress.build(domain)
            if broker in device_brokers:
                return True
        if reserved_domain := device_info.get(mc.KEY_RESERVEDDOMAIN):
            if reserved_domain == domain:
                # already checked
                return False
            broker = HostAddress.build(reserved_domain)
            if broker in device_brokers:
                return True
        # no way
        return False

    def try_link(self, device: MerossDevice):
        device_id = device.id
        device_info = self._data[self.KEY_DEVICE_INFO].get(device_id)
        if not device_info:
            # does not belong here.
            # we previously used the device.descriptor.userId to 'index'
            # into the cloud profiles but that info has always been too
            # flaky to be reliable (since when we rebind the devices people
            # could use any value for the device conf and so use a 'valid'
            # userid from a cloud account even if the device is not binded
            # anymore)
            return False
        if super().try_link(device):
            device.update_device_info(device_info)
            if latest_version := self.get_latest_version(device.descriptor):
                device.update_latest_version(latest_version)
            return True
        return False

    def get_device_info(self, device_id: str):
        return self._data[self.KEY_DEVICE_INFO].get(device_id)

    def get_latest_version(self, descriptor: MerossDeviceDescriptor):
        """returns LatestVersionType info if device has an update available"""
        _type = descriptor.type
        _version = versiontuple(descriptor.firmwareVersion)
        # the LatestVersionType struct reports also the subType for the firmware
        # but the meaning of this field is a bit confusing since a lot of traces
        # are reporting the value "un" (undefined?) for the vast majority.
        # Also, the mcu field (should contain a list of supported mcus?) is not
        # reported in my api queries and I don't have enough data to guess anything
        # at any rate, actual implementation is not proceeding with effective
        # update so these infos we gather and show are just cosmethic right now and
        # will not harm anyone ;)
        # _subtype = descriptor.subType
        for latest_version in self._data[self.KEY_LATEST_VERSION]:
            if (
                latest_version.get(mc.KEY_TYPE)
                == _type
                # and latest_version.get(mc.KEY_SUBTYPE) == _subtype
            ):
                if versiontuple(latest_version.get(mc.KEY_VERSION, "")) > _version:
                    return latest_version
                else:
                    return None
        return None

    async def async_update_credentials(self, credentials: MerossCloudCredentials):
        with self.exception_warning("async_update_credentials"):
            assert self.id == credentials[mc.KEY_USERID_]
            assert self.key == credentials[mc.KEY_KEY]
            token = self._data.get(mc.KEY_TOKEN)
            if token != credentials[mc.KEY_TOKEN]:
                self.log(DEBUG, "updating credentials with new token")
                if token:
                    # discard old one to play it nice but token might be expired
                    with self.exception_warning("async_cloudapi_logout"):
                        await async_cloudapi_logout(
                            token, async_get_clientsession(ApiProfile.hass)
                        )
                else:
                    issue_registry.async_delete_issue(
                        self.hass, DOMAIN, f"{ISSUE_CLOUD_TOKEN_EXPIRED}.{self.id}"
                    )

                self._data[mc.KEY_TOKEN] = credentials[mc.KEY_TOKEN]
                self._schedule_save_store()
                # the 'async_check_query_devices' will only occur if we didn't refresh
                # on our polling schedule for whatever reason (invalid token -
                # no connection - whatsoever) so, having a fresh token and likely
                # good connectivity we're going to retrigger that
                await self.async_check_query_device_info()

    async def async_query_device_info(self):
        async with self._async_token_manager("async_query_device_info") as token:
            self.log(
                DEBUG,
                "querying device list - last query was at: %s",
                datetime_from_epoch(self._device_info_time).isoformat(),
            )
            if not token:
                self.log(WARNING, "querying device list cancelled: missing api token")
                return None
            self._device_info_time = time()
            device_info_new = await async_cloudapi_device_devlist(
                token, async_get_clientsession(ApiProfile.hass)
            )
            await self._process_device_info_new(device_info_new)
            self._data[self.KEY_DEVICE_INFO_TIME] = self._device_info_time
            self._schedule_save_store()
            # retrigger the poll at the right time since async_query_devices
            # might be called for whatever reason 'asynchronously'
            # at any time (say the user does a new cloud login or so...)
            if self._unsub_polling_query_device_info:
                self._unsub_polling_query_device_info.cancel()
            self._unsub_polling_query_device_info = schedule_async_callback(
                ApiProfile.hass,
                PARAM_CLOUDPROFILE_QUERY_DEVICELIST_TIMEOUT,
                self._async_polling_query_device_info,
            )
            # this is a 'low relevance task' as a new feature (in 4.3.0) to just provide hints
            # when new updates are available: we're not going (yet) to manage the
            # effective update since we're not able to do any basic validation
            # of the whole process and it might be a bit 'dangerous'
            await self.async_check_query_latest_version(self._device_info_time, token)
            return device_info_new

        return None

    def need_query_device_info(self):
        return (
            time() - self._device_info_time
        ) > PARAM_CLOUDPROFILE_QUERY_DEVICELIST_TIMEOUT

    async def async_check_query_device_info(self):
        if self.need_query_device_info():
            return await self.async_query_device_info()
        return None

    async def async_check_query_latest_version(self, epoch: float, token: str):
        if (
            self.config.get(CONF_CHECK_FIRMWARE_UPDATES)
            and (epoch - self._data[self.KEY_LATEST_VERSION_TIME])
            > PARAM_CLOUDPROFILE_QUERY_LATESTVERSION_TIMEOUT
        ):
            self._data[self.KEY_LATEST_VERSION_TIME] = epoch
            with self.exception_warning("async_check_query_latest_version"):
                self._data[
                    self.KEY_LATEST_VERSION
                ] = await async_cloudapi_device_latestversion(
                    token, async_get_clientsession(ApiProfile.hass)
                )
                self._schedule_save_store()
                for device in ApiProfile.active_devices():
                    if latest_version := self.get_latest_version(device.descriptor):
                        device.update_latest_version(latest_version)

    async def get_or_create_mqttconnections(self, device_id: str):
        """
        Returns a list of (active) broker connections according to the cloud configuration.
        The list is empty if device not configured or if the connection(s) to the brokers
        cannot be established (like broker is down any network issue)
        """
        mqttconnections: list[MQTTConnection] = []
        device_info = self.get_device_info(device_id)
        if device_info:
            if domain := device_info.get(mc.KEY_DOMAIN):
                mqttconnection = await self._async_get_mqttconnection(
                    HostAddress.build(domain)
                )
                if mqttconnection:
                    mqttconnections.append(mqttconnection)
            if reserveddomain := device_info.get(mc.KEY_RESERVEDDOMAIN):
                if reserveddomain != domain:
                    mqttconnection = await self._async_get_mqttconnection(
                        HostAddress.build(reserveddomain)
                    )
                    if mqttconnection:
                        mqttconnections.append(mqttconnection)
        return mqttconnections

    def _get_mqttconnection(self, broker: HostAddress) -> MerossMQTTConnection:
        """
        Returns an existing connection from the managed pool or create one and add
        to the mqttconnections pool. The connection state is not ensured.
        """
        connection_id = f"{broker.host}:{broker.port}"
        if connection_id in self.mqttconnections:
            return self.mqttconnections[connection_id]  # type: ignore
        self.mqttconnections[connection_id] = mqttconnection = MerossMQTTConnection(
            self, connection_id, broker
        )
        return mqttconnection

    async def _async_get_mqttconnection(self, broker: HostAddress):
        """
        Retrieve a connection for the broker from the managed pool (or creates it)
        and tries ensuring it is connected returning None if not (this is especially
        needed when we want to setup a broker connection for device identification
        and we so need it soon).
        """
        mqttconnection = self._get_mqttconnection(broker)
        if mqttconnection.state_active:
            if mqttconnection.stateext is mqttconnection.STATE_CONNECTED:
                return mqttconnection
            else:
                return None
        with self.exception_warning("_async_get_mqttconnection_active"):
            future = self.hass.loop.create_future()
            future = await mqttconnection.schedule_connect_async(future)
            if future:
                await asyncio.wait_for(future, 5)
                return mqttconnection
            return None
        return None

    async def _async_token_missing(self, should_raise_issue: bool):
        """
        Called when the stored token is dropped (expired) or when needed
        through _async_cloud_token_manager: try silently (re)login or raise an issue
        """
        with self.exception_warning("_async_token_missing"):
            config = self.config
            if CONF_PASSWORD not in config:
                if should_raise_issue:
                    issue_registry.async_create_issue(
                        self.hass,
                        DOMAIN,
                        f"{ISSUE_CLOUD_TOKEN_EXPIRED}.{self.id}",
                        is_fixable=True,
                        severity=issue_registry.IssueSeverity.WARNING,
                        translation_key=ISSUE_CLOUD_TOKEN_EXPIRED,
                        translation_placeholders={"email": config.get(mc.KEY_EMAIL)},
                    )
                return None
            data = self._data
            if (_time := time()) < data[
                self.KEY_TOKEN_REQUEST_TIME
            ] + PARAM_CLOUDPROFILE_QUERY_DEVICELIST_TIMEOUT:
                return None
            data[self.KEY_TOKEN_REQUEST_TIME] = _time
            self._schedule_save_store()
            credentials = await async_cloudapi_login(
                config[CONF_EMAIL],
                config[CONF_PASSWORD],  # type: ignore
                async_get_clientsession(self.hass),
            )
            profile_id = self.id
            if profile_id != credentials[mc.KEY_USERID_]:
                raise Exception("cloud_profile_mismatch")
            token = credentials[mc.KEY_TOKEN]
            # set our (stored) key so the ConfigEntry update will find everything in place
            # and not trigger any side effects. No need to re-trigger _schedule_save_store
            # since it should still be pending...
            data[mc.KEY_TOKEN] = token
            self.log(INFO, "Cloud token was automatically refreshed")
            helper = ConfigEntriesHelper(self.hass)
            profile_entry = helper.get_config_entry(f"profile.{profile_id}")
            if profile_entry:
                # weird enough if this isnt true...
                profile_config = dict(profile_entry.data)
                profile_config.update(credentials)
                # watchout: this will in turn call async_update_credentials
                helper.config_entries.async_update_entry(
                    profile_entry,
                    data=profile_config,
                )
            return token

        return None

    @asynccontextmanager
    async def _async_token_manager(self, msg: str, *args, **kwargs):
        data = self._data
        try:
            # this is called every time we'd need a token to query the cloudapi
            # it just yields the current one or tries it's best to recover a fresh
            # token with a guard to avoid issuing too many requests...
            if mc.KEY_TOKEN in data:
                yield data[mc.KEY_TOKEN]
            else:
                yield await self._async_token_missing(False)
        except CloudApiError as clouderror:
            if clouderror.apistatus in APISTATUS_TOKEN_ERRORS:
                if data.pop(mc.KEY_TOKEN, None):  # type: ignore
                    await self._async_token_missing(True)
            self.log_exception(WARNING, clouderror, msg)
        except Exception as exception:
            self.log_exception(WARNING, exception, msg)

    async def _async_polling_query_device_info(self):
        try:
            self._unsub_polling_query_device_info = None
            await self.async_query_device_info()
        finally:
            if self._unsub_polling_query_device_info is None:
                # this happens when 'async_query_devices' is unable to
                # retrieve fresh cloud data for whatever reason
                self._unsub_polling_query_device_info = schedule_async_callback(
                    ApiProfile.hass,
                    PARAM_CLOUDPROFILE_QUERY_DEVICELIST_TIMEOUT,
                    self._async_polling_query_device_info,
                )

    async def _async_query_subdevices(self, device_id: str):
        async with self._async_token_manager("_async_query_subdevices") as token:
            if not token:
                self.log(
                    WARNING, "querying subdevice list cancelled: missing api token"
                )
                return None
            self.log(DEBUG, "querying subdevice list")
            return await async_cloudapi_hub_getsubdevices(
                token, device_id, async_get_clientsession(ApiProfile.hass)
            )
        return None

    async def _process_device_info_new(
        self, device_info_list_new: list[DeviceInfoType]
    ):
        device_info_dict = self._data[self.KEY_DEVICE_INFO]
        device_info_removed = {device_id for device_id in device_info_dict.keys()}
        device_info_unknown: list[DeviceInfoType] = []
        for device_info in device_info_list_new:
            with self.exception_warning("_process_device_info_new"):
                device_id = device_info[mc.KEY_UUID]
                # preserved (old) dict of hub subdevices to process/carry over
                # for MerossDeviceHub(s)
                sub_device_info_dict: dict[str, SubDeviceInfoType] | None
                if device_id in device_info_dict:
                    # already known device
                    device_info_removed.remove(device_id)
                    sub_device_info_dict = device_info_dict[device_id].get(
                        self.KEY_SUBDEVICE_INFO
                    )
                else:
                    # new device
                    sub_device_info_dict = None
                device_info_dict[device_id] = device_info

                if device_id not in ApiProfile.devices:
                    device_info_unknown.append(device_info)
                    continue

                if (device := ApiProfile.devices[device_id]) is None:
                    # config_entry for device is not loaded
                    continue

                if isinstance(device, MerossDeviceHub):
                    if sub_device_info_dict is None:
                        sub_device_info_dict = {}
                    device_info[self.KEY_SUBDEVICE_INFO] = sub_device_info_dict
                    sub_device_info_list_new = await self._async_query_subdevices(
                        device_id
                    )
                    if sub_device_info_list_new is not None:
                        await self._process_subdevice_info_new(
                            device, sub_device_info_dict, sub_device_info_list_new
                        )
                device.update_device_info(device_info)

        for device_id in device_info_removed:
            self.log(
                DEBUG,
                "The device %s has been removed from the cloud profile",
                device_id,
            )
            device_info_dict.pop(device_id)
            if device := self.linkeddevices.get(device_id):
                self.unlink(device)

        if len(device_info_unknown):
            await self._process_device_info_unknown(device_info_unknown)

    async def _process_subdevice_info_new(
        self,
        hub_device: MerossDeviceHub,
        sub_device_info_dict: dict[str, SubDeviceInfoType],
        sub_device_info_list_new: list[SubDeviceInfoType],
    ):
        sub_device_info_removed = {
            subdeviceid for subdeviceid in sub_device_info_dict.keys()
        }
        sub_device_info_unknown: list[SubDeviceInfoType] = []

        for sub_device_info in sub_device_info_list_new:
            with self.exception_warning("_process_subdevice_info_new"):
                subdeviceid = sub_device_info[mc.KEY_SUBDEVICEID]
                if subdeviceid in sub_device_info_dict:
                    # already known device
                    sub_device_info_removed.remove(subdeviceid)

                sub_device_info_dict[subdeviceid] = sub_device_info
                if subdevice := hub_device.subdevices.get(subdeviceid):
                    subdevice.update_device_info(sub_device_info)
                else:
                    sub_device_info_unknown.append(sub_device_info)

        for subdeviceid in sub_device_info_removed:
            sub_device_info_dict.pop(subdeviceid)
            # TODO: warn the user? should we remove the subdevice from the hub?

        if len(sub_device_info_unknown):
            # subdevices were added.. discovery should be managed by the hub itself
            # TODO: warn the user ?
            pass

    async def _process_device_info_unknown(
        self, device_info_unknown: list[DeviceInfoType]
    ):
        if not self.allow_mqtt_publish:
            self.log(
                WARNING,
                "Meross cloud api reported new devices but MQTT publishing is disabled: skipping automatic discovery",
                timeout=604800,  # 1 week
            )
            return

        config_entries_helper = ConfigEntriesHelper(ApiProfile.hass)
        for device_info in device_info_unknown:
            with self.exception_warning("_process_device_info_unknown"):
                device_id = device_info[mc.KEY_UUID]
                self.log(
                    DEBUG, "Trying/Initiating discovery for (new) device:%s", device_id
                )
                if config_entries_helper.get_config_flow(device_id):
                    continue  # device configuration already progressing
                # cloud conf has a new device
                if domain := device_info.get(mc.KEY_DOMAIN):
                    # try first broker in the cloud configuration
                    if mqttconnection := await self._async_get_mqttconnection(
                        HostAddress.build(domain)
                    ):
                        if await mqttconnection.async_try_discovery(device_id):
                            continue  # identification succeded, a flow has been created
                if (reserveddomain := device_info.get(mc.KEY_RESERVEDDOMAIN)) and (
                    reserveddomain != domain
                ):
                    # try the second broker in the cloud configuration
                    # only if it's different than the previous
                    if mqttconnection := await self._async_get_mqttconnection(
                        HostAddress.build(reserveddomain)
                    ):
                        if await mqttconnection.async_try_discovery(device_id):
                            continue  # identification succeded, a flow has been created

    def _schedule_save_store(self):
        def _data_func():
            return self._data

        self._store.async_delay_save(
            _data_func, PARAM_CLOUDPROFILE_DELAYED_SAVE_TIMEOUT
        )
