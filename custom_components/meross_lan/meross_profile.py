"""
    meross_lan module interface to access Meross Cloud services
"""

from __future__ import annotations

import abc
import asyncio
from contextlib import asynccontextmanager
from time import time
import typing

from homeassistant.config_entries import SOURCE_INTEGRATION_DISCOVERY
from homeassistant.core import callback
from homeassistant.helpers import storage

from . import const as mlc
from .const import (
    CONF_CHECK_FIRMWARE_UPDATES,
    CONF_DEVICE_ID,
    CONF_KEY,
    CONF_PASSWORD,
    CONF_PAYLOAD,
    DOMAIN,
    DeviceConfigType,
)
from .helpers import (
    ConfigEntriesHelper,
    Loggable,
    datetime_from_epoch,
    schedule_async_callback,
    versiontuple,
)
from .helpers.manager import ApiProfile, CloudApiClient
from .meross_device_hub import MerossDeviceHub
from .merossclient import (
    MEROSSDEBUG,
    HostAddress,
    MerossKeyError,
    MerossRequest,
    MerossResponse,
    check_message_strict,
    const as mc,
    get_message_uuid,
    get_replykey,
    request_get,
)
from .merossclient.cloudapi import APISTATUS_TOKEN_ERRORS, CloudApiError
from .merossclient.mqttclient import MerossMQTTAppClient, generate_app_id
from .repairs import IssueSeverity, create_issue, remove_issue
from .sensor import MLDiagnosticSensor

if typing.TYPE_CHECKING:
    from typing import Final

    from homeassistant.components import mqtt as ha_mqtt
    from homeassistant.config_entries import ConfigEntry
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


class ConnectionSensor(MLDiagnosticSensor):
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

    # HA core entity attributes:
    _attr_available = True
    extra_state_attributes: AttrDictType
    native_value: str
    options: list[str] = [
        STATE_DISCONNECTED,
        STATE_CONNECTED,
        STATE_QUEUING,
        STATE_DROPPING,
    ]

    __slots__ = ("connection",)

    def __init__(self, connection: MQTTConnection):
        self.connection = connection
        self.extra_state_attributes = {
            ConnectionSensor.ATTR_DEVICES: {
                device.id: device.name for device in connection.mqttdevices.values()
            },
            ConnectionSensor.ATTR_RECEIVED: 0,
            ConnectionSensor.ATTR_PUBLISHED: 0,
            ConnectionSensor.ATTR_DROPPED: 0,
            ConnectionSensor.ATTR_QUEUED: 0,
            ConnectionSensor.ATTR_QUEUE_LENGTH: 0,
        }
        super().__init__(
            connection.profile,
            None,
            connection.id,
            native_value=(
                self.STATE_CONNECTED
                if connection.mqtt_is_connected
                else self.STATE_DISCONNECTED
            ),
        )
        connection.sensor_connection = self

    # interface: Loggable
    def configure_logger(self):
        self.logtag = (
            f"{self.__class__.__name__}({self.manager.loggable_broker(self.id)})"
        )

    # interface: MLDiagnosticSensor
    async def async_shutdown(self):
        await super().async_shutdown()
        self.connection.sensor_connection = None
        self.connection: MQTTConnection = None  # type: ignore

    # interface: self
    def update_devices(self):
        # rebuild the attr (sub)dict else we were keeping a reference
        # to the underlying hass.state and updates were missing
        self.extra_state_attributes[ConnectionSensor.ATTR_DEVICES] = {
            device.id: device.name for device in self.connection.mqttdevices.values()
        }
        self.flush_state()

    def inc_counter(self, attr_name: str):
        self.extra_state_attributes[attr_name] += 1
        self.flush_state()

    def inc_counter_with_state(self, attr_name: str, state: str):
        self.extra_state_attributes[attr_name] += 1
        self.native_value = state
        self.flush_state()

    def inc_queued(self, queue_length: int):
        attrs = self.extra_state_attributes
        attrs[ConnectionSensor.ATTR_QUEUED] += 1
        attrs[ConnectionSensor.ATTR_QUEUE_LENGTH] = queue_length
        self.native_value = ConnectionSensor.STATE_QUEUING
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
        self.response_future: asyncio.Future[MerossResponse] = (
            asyncio.get_running_loop().create_future()
        )
        mqtt_connection._mqtt_transactions[request.messageid] = self

    def cancel(self):
        mqtt_connection = self.mqtt_connection
        mqtt_connection.log(
            mqtt_connection.DEBUG,
            "Cancelling mqtt transaction on %s %s (uuid:%s messageId:%s)",
            self.method,
            self.namespace,
            mqtt_connection.profile.loggable_device_id(self.device_id),
            self.messageid,
        )
        self.response_future.cancel()
        mqtt_connection._mqtt_transactions.pop(self.messageid, None)


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
        broker: HostAddress,
        topic_response: str,
    ):
        self.profile: Final = profile
        self.broker = broker
        self.topic_response: Final = topic_response
        self.mqttdevices: Final[dict[str, MerossDevice]] = {}
        self.mqttdiscovering: Final[set[str]] = set()
        self.namespace_handlers = self.SESSION_HANDLERS
        self.sensor_connection: ConnectionSensor | None = None
        self._mqtt_transactions: Final[dict[str, _MQTTTransaction]] = {}
        self._mqtt_is_connected = False
        super().__init__(
            str(broker),
            logger=profile,
        )
        profile.mqttconnections[self.id] = self
        if profile.create_diagnostic_entities:
            ConnectionSensor(self)

    # interface: Loggable
    def configure_logger(self):
        self.logtag = (
            f"{self.__class__.__name__}({self.profile.loggable_broker(self.broker)})"
        )

    # interface: self
    async def async_shutdown(self):
        for mqtt_transaction in list(self._mqtt_transactions.values()):
            mqtt_transaction.cancel()
        self.mqttdiscovering.clear()
        for device in self.mqttdevices.values():
            device.mqtt_detached()
        self.mqttdevices.clear()
        self.sensor_connection = None

    async def async_create_diagnostic_entities(self):
        if not self.sensor_connection:
            ConnectionSensor(self)

    async def entry_update_listener(self, profile: ApiProfile):
        """Called by the ApiProfile to propagate config changes"""
        self.configure_logger()
        if self.sensor_connection:
            self.sensor_connection.configure_logger()

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
        device.mqtt_attached(self)
        self.mqttdevices[device.id] = device
        if sensor_connection := self.sensor_connection:
            sensor_connection.update_devices()

    def detach(self, device: MerossDevice):
        device_id = device.id
        assert device_id in self.mqttdevices
        for mqtt_transaction in list(self._mqtt_transactions.values()):
            if mqtt_transaction.device_id == device_id:
                mqtt_transaction.cancel()
        device.mqtt_detached()
        self.mqttdevices.pop(device_id)
        if sensor_connection := self.sensor_connection:
            sensor_connection.update_devices()

    @typing.final
    def mqtt_publish(
        self,
        device_id: str,
        request: MerossMessage,
    ):
        return self.hass.async_create_task(self.async_mqtt_publish(device_id, request))

    @typing.final
    async def async_mqtt_publish(
        self,
        device_id: str,
        request: MerossMessage,
    ) -> MerossResponse | None:
        if request.method in mc.METHOD_ACK_MAP.keys():
            transaction = _MQTTTransaction(self, device_id, request)
        else:
            transaction = None
        try:
            self.profile.trace_or_log(self, device_id, request, ApiProfile.TRACE_TX)
            _mqtt_tx_code, timeout = await self._async_mqtt_publish(device_id, request)
            if transaction:
                if _mqtt_tx_code is self._MQTT_DROP:
                    transaction.cancel()
                    return None
                try:
                    return await asyncio.wait_for(transaction.response_future, timeout)
                except Exception as exception:
                    self.log_exception(
                        self.DEBUG,
                        exception,
                        "waiting for MQTT reply to %s %s (uuid:%s messageId:%s)",
                        request.method,
                        request.namespace,
                        self.profile.loggable_device_id(device_id),
                        request.messageid,
                    )
                    return None
                finally:
                    self._mqtt_transactions.pop(transaction.messageid, None)

        except Exception as exception:
            self.log_exception(
                self.DEBUG,
                exception,
                "async_mqtt_publish %s %s (uuid:%s messageId:%s)",
                request.method,
                request.namespace,
                self.profile.loggable_device_id(device_id),
                request.messageid,
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
            message = MerossResponse(
                mqtt_payload
                if type(mqtt_payload) is str
                else mqtt_payload.decode("utf-8")  # type: ignore
            )
            header = message[mc.KEY_HEADER]
            device_id = get_message_uuid(header)
            namespace = header[mc.KEY_NAMESPACE]
            messageid = header[mc.KEY_MESSAGEID]
            payload = message[mc.KEY_PAYLOAD]

            profile = self.profile
            profile.trace_or_log(self, device_id, message, ApiProfile.TRACE_RX)

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
                    device.mqtt_receive(message)
                    return
                # we have the device loaded but somehow it is not 'mqtt binded' here.
                # Either it's configuration is CONF_PROTOCOL_HTTP or it is paired to
                # another profile. In this case we could automagically fix this (see later)
                if device._profile != profile:
                    # It could happen (I guess) when devices 'switch' broker while the
                    # integration was already loaded so not really often
                    if profile.try_link(device):
                        self.log(
                            self.INFO,
                            "Device uuid:%s has been automatically re-linked to this profile",
                            profile.loggable_device_id(device_id),
                        )
                        # keep checking MQTT proto is allowed at the device level
                        if device._mqtt_connection == self:
                            device.mqtt_receive(message)
                            return
                        # else..keep going so we log the '...HTTP_ONLY..'
                    else:
                        # this is not really expected and deserves a warning but is expected
                        # when you (re)bind a device and it still is connected to the old broker
                        # until reboot
                        self.log(
                            self.WARNING,
                            "Device uuid:%s cannot be registered for MQTT handling on this profile",
                            profile.loggable_device_id(device_id),
                            timeout=14400,
                        )
                        return

                self.log(
                    self.DEBUG,
                    "Device uuid:%s not registered for MQTT handling. It is likely HTTP_ONLY",
                    profile.loggable_device_id(device_id),
                    timeout=14400,
                )
                return

            # the device is not configured: proceed to discovery in case
            if device_id in self.mqttdiscovering:
                return

            # lookout for any disabled/ignored entry
            config_entries_helper = ConfigEntriesHelper(self.hass)
            if (
                (profile is self.api)
                and (not config_entries_helper.get_config_entry(DOMAIN))
                and (not config_entries_helper.get_config_flow(DOMAIN))
            ):
                # not really needed but we would like to always have the
                # MQTT hub entry in case so if the user removed that..retrigger
                await self.hass.config_entries.flow.async_init(
                    DOMAIN,
                    context={"source": "hub"},
                    data=None,
                )

            if config_entry := (
                config_entries_helper.get_config_entry(device_id)
                or config_entries_helper.get_config_entry(device_id[-12:].lower())
            ):
                # entry already present...skip discovery
                self.log(
                    self.INFO,
                    "Ignoring MQTT discovery for already configured uuid:%s (ConfigEntry is %s)",
                    profile.loggable_device_id(device_id),
                    (
                        "disabled"
                        if config_entry.disabled_by
                        else "ignored" if config_entry.source == "ignore" else "unknown"
                    ),
                    timeout=28800,  # type: ignore
                )
                return

            # also skip discovered integrations waiting in HA queue
            if config_entries_helper.get_config_flow(device_id):
                self.log(
                    self.DEBUG,
                    "Ignoring MQTT discovery for uuid:%s (ConfigFlow is in progress)",
                    profile.loggable_device_id(device_id),
                    timeout=14400,  # type: ignore
                )
                return

            key = profile.key
            if get_replykey(header, key) is not key:
                self.log(
                    self.WARNING,
                    "Discovery key error for uuid:%s",
                    profile.loggable_device_id(device_id),
                    timeout=300,
                )
                if key is not None:
                    return

            await self.async_try_discovery(device_id)

    async def async_identify_device(self, device_id: str, key: str) -> DeviceConfigType:
        """
        Sends an ns_all and ns_ability GET requests encapsulated in an ns_multiple
        to speed up things. Raises exception in case of error
        """
        self.log(
            self.DEBUG,
            "Initiating 1-step identification for uuid:%s",
            self.profile.loggable_device_id(device_id),
        )
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
                            *request_get(mc.NS_APPLIANCE_SYSTEM_ALL),
                            topic_response,
                        ),
                        MerossRequest(
                            key,
                            *request_get(mc.NS_APPLIANCE_SYSTEM_ABILITY),
                            topic_response,
                        ),
                    ]
                },
                topic_response,
            ),
        )

        try:
            response = check_message_strict(response)
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
        except MerossKeyError as error:
            # no point in attempting 2-steps identification
            raise error
        except Exception as exception:
            self.log(
                self.DEBUG,
                "Identification error('%s') for uuid:%s. Falling back to 2-steps procedure",
                str(exception),
                self.profile.loggable_device_id(device_id),
            )
            try:
                response = check_message_strict(
                    await self.async_mqtt_publish(
                        device_id,
                        MerossRequest(
                            key,
                            *request_get(mc.NS_APPLIANCE_SYSTEM_ABILITY),
                            topic_response,
                        ),
                    )
                )
                ability = response[mc.KEY_PAYLOAD][mc.KEY_ABILITY]
            except MerossKeyError as error:
                raise error
            except Exception as exception:
                raise Exception("Unable to identify abilities") from exception
            try:
                response = check_message_strict(
                    await self.async_mqtt_publish(
                        device_id,
                        MerossRequest(
                            key,
                            *request_get(mc.NS_APPLIANCE_SYSTEM_ALL),
                            topic_response,
                        ),
                    )
                )
                all = response[mc.KEY_PAYLOAD][mc.KEY_ALL]
            except MerossKeyError as error:
                raise error
            except Exception as exception:
                raise Exception("Unable to identify device (all)") from exception
            return {
                CONF_DEVICE_ID: device_id,
                CONF_PAYLOAD: {
                    mc.KEY_ALL: all,
                    mc.KEY_ABILITY: ability,
                },
                CONF_KEY: key,
            }

    async def async_try_discovery(self, device_id: str):
        """
        Tries device identification and starts a flow if succeded returning
        the FlowResult. Returns None if anything fails for whatever reason.
        """
        result = None
        self.mqttdiscovering.add(device_id)
        with self.exception_warning(
            "async_try_discovery (uuid:%s)",
            self.profile.loggable_device_id(device_id),
            timeout=14400,
        ):
            result = await self.hass.config_entries.flow.async_init(
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
            sensor_connection.update_native_value(ConnectionSensor.STATE_CONNECTED)

    @callback
    def _mqtt_disconnected(self):
        """called when the underlying mqtt.Client disconnects from the broker"""
        for device in self.mqttdevices.values():
            device.mqtt_disconnected()
        self._mqtt_is_connected = False
        if sensor_connection := self.sensor_connection:
            sensor_connection.update_native_value(ConnectionSensor.STATE_DISCONNECTED)

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
        "_asyncio_loop",
        "_future_connected",
        "_tasks",
        "_lock_state",
        "_lock_queue",
        "_rl_lastpublish",
        "_rl_qeque",
        "_rl_queue_length",
        "_rl_dropped",
        "_rl_avgperiod",
        "_stateext",
        "_subscribe_topics",
        "_unsub_random_disconnect",
    )

    def __init__(self, profile: MerossCloudProfile, broker: HostAddress):
        hass = self.hass
        MerossMQTTAppClient.__init__(
            self, profile.key, profile.userid, app_id=profile.app_id, loop=hass.loop
        )
        MQTTConnection.__init__(self, profile, broker, self.topic_command)
        if profile.isEnabledFor(profile.VERBOSE):
            self.enable_logger(self)  # type: ignore (Loggable is duck-compatible with Logger)

        if MEROSSDEBUG:

            @callback
            async def _async_random_disconnect():
                if self.state_inactive:
                    if MEROSSDEBUG.mqtt_random_connect():
                        self.log(self.DEBUG, "Random connect")
                        await self.async_connect(self.broker)
                else:
                    if MEROSSDEBUG.mqtt_random_disconnect():
                        self.log(self.DEBUG, "Random disconnect")
                        await self.async_disconnect()
                self._unsub_random_disconnect = schedule_async_callback(
                    hass, 60, _async_random_disconnect
                )

            self._unsub_random_disconnect = schedule_async_callback(
                hass, 60, _async_random_disconnect
            )
        else:
            self._unsub_random_disconnect = None

    # interface: MQTTConnection
    async def async_shutdown(self):
        if self._unsub_random_disconnect:
            self._unsub_random_disconnect.cancel()
            self._unsub_random_disconnect = None
        await MerossMQTTAppClient.async_shutdown(self)
        await MQTTConnection.async_shutdown(self)

    async def entry_update_listener(self, profile: MerossCloudProfile):
        await MQTTConnection.entry_update_listener(self, profile)
        if profile.isEnabledFor(profile.VERBOSE):
            self.enable_logger(self)  # type: ignore (Loggable is duck-compatible with Logger)
        else:
            self.disable_logger()

    @property
    def is_cloud_connection(self):
        return True

    async def _async_mqtt_publish(
        self,
        device_id: str,
        request: MerossMessage,
    ) -> tuple[str, int]:
        return await self.hass.async_add_executor_job(self._publish, device_id, request)

    @callback
    def _mqtt_connected(self):
        MerossMQTTAppClient._mqtt_connected(self)
        MQTTConnection._mqtt_connected(self)

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
            attrs = sensor_connection.extra_state_attributes
            attrs[ConnectionSensor.ATTR_QUEUE_LENGTH] = queue_length
            attrs[ConnectionSensor.ATTR_DROPPED] = self.rl_dropped
            attrs[ConnectionSensor.ATTR_PUBLISHED] += 1
            if self.mqtt_is_connected and not queue_length:
                # enforce the state eventually cancelling queued, dropped...
                sensor_connection.native_value = ConnectionSensor.STATE_CONNECTED
            sensor_connection.flush_state()

    # interface: self
    def _publish(self, device_id: str, request: MerossMessage) -> tuple[str, int]:
        """
        this function runs in an executor
        Beware when calling HA api's (like when we want to update sensors)
        """
        if not self.allow_mqtt_publish:
            raise Exception("MQTT publishing is not allowed for this profile")

        ret = self.rl_publish(
            mc.TOPIC_REQUEST.format(device_id),
            request.json(),
            MerossMQTTConnection._MSG_PRIORITY_MAP[request.method],
        )
        if ret is False:
            if sensor_connection := self.sensor_connection:
                self.hass.loop.call_soon_threadsafe(
                    sensor_connection.inc_counter_with_state,
                    ConnectionSensor.ATTR_DROPPED,
                    ConnectionSensor.STATE_DROPPING,
                )
            self.log(
                self.DEBUG,
                "MQTT DROP %s %s (uuid:%s messageId:%s)",
                request.method,
                request.namespace,
                self.profile.loggable_device_id(device_id),
                request.messageid,
            )
            return (self._MQTT_DROP, 0)
        if ret is True:
            if sensor_connection := self.sensor_connection:
                self.hass.loop.call_soon_threadsafe(
                    sensor_connection.inc_queued,
                    self.rl_queue_length,
                )
            self.log(
                self.DEBUG,
                "MQTT QUEUE %s %s (uuid:%s messageId:%s)",
                request.method,
                request.namespace,
                self.profile.loggable_device_id(device_id),
                request.messageid,
            )
            return (
                self._MQTT_QUEUE,
                self.rl_queue_duration + self.DEFAULT_RESPONSE_TIMEOUT,
            )
        return (self._MQTT_PUBLISH, self.DEFAULT_RESPONSE_TIMEOUT)


MerossMQTTConnection.SESSION_HANDLERS = {
    mc.NS_APPLIANCE_SYSTEM_ONLINE: MQTTConnection._handle_Appliance_System_Online,
}


class MerossCloudProfileStoreType(typing.TypedDict):
    appId: str
    # TODO credentials: typing.NotRequired[MerossCloudCredentials]
    deviceInfo: DeviceInfoDictType
    deviceInfoTime: float
    latestVersion: list[LatestVersionType]
    latestVersionTime: float
    token: str | None  # TODO remove
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
        "apiclient",
        "_data",
        "_store",
        "_unsub_polling_query_device_info",
        "_device_info_time",
    )

    def __init__(self, profile_id: str, config_entry: ConfigEntry):
        ApiProfile.__init__(self, profile_id, config_entry)
        # state of the art for credentials is that they're mixed in
        # into the config_entry.data but this is prone to issues and confusing
        # so we 'might' decide to move them to a dict valued key in configentry.data
        # or completely remove and store them in storage. Whatever
        # we might desire compatibility between storage formats with previous versions
        # so we're putting the migration code in 5.0.0 but still not going
        # to change the version(s) in storage/config. At the moment I'm still very confused
        # and opting to keep the credentials where they are embedded in ConfigEntry
        self.apiclient = CloudApiClient(self, self.config)
        self._store = MerossCloudProfileStore(profile_id)
        self._unsub_polling_query_device_info: asyncio.TimerHandle | None = None

    async def async_init(self):
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

            if not data.get(mc.KEY_TOKEN):
                # the token would be auto-refreshed when needed in
                # _async_token_manager but we'd eventually need
                # to just setup the issue registry in case we're
                # not configured to automatically refresh
                self.apiclient.credentials = None
                await self._async_token_missing(True)
        else:
            self._device_info_time = 0.0
            self._data: MerossCloudProfileStoreType = {
                self.KEY_APP_ID: generate_app_id(),
                mc.KEY_TOKEN: self.config.get(mc.KEY_TOKEN),
                self.KEY_DEVICE_INFO: {},
                self.KEY_DEVICE_INFO_TIME: 0.0,
                self.KEY_LATEST_VERSION: [],
                self.KEY_LATEST_VERSION_TIME: 0.0,
                self.KEY_TOKEN_REQUEST_TIME: 0.0,
            }

        if mc.KEY_MQTTDOMAIN in self.config:
            broker = HostAddress.build(self.config[mc.KEY_MQTTDOMAIN])  # type: ignore
            mqttconnection = MerossMQTTConnection(self, broker)
            mqttconnection.schedule_connect(broker)

        # compute the next cloud devlist query and setup the scheduled callback
        next_query_epoch = (
            self._device_info_time + mlc.PARAM_CLOUDPROFILE_QUERY_DEVICELIST_TIMEOUT
        )
        next_query_delay = next_query_epoch - time()
        if next_query_delay < mlc.PARAM_CLOUDPROFILE_DELAYED_SETUP_TIMEOUT:
            # we'll give some breath to the init process
            next_query_delay = mlc.PARAM_CLOUDPROFILE_DELAYED_SETUP_TIMEOUT
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
        self._unsub_polling_query_device_info = schedule_async_callback(
            self.hass,
            next_query_delay,
            self._async_polling_query_device_info,
        )

    async def async_shutdown(self):
        ApiProfile.profiles[self.id] = None
        if self._unsub_polling_query_device_info:
            self._unsub_polling_query_device_info.cancel()
            self._unsub_polling_query_device_info = None
        await super().async_shutdown()

    # interface: ConfigEntryManager
    async def entry_update_listener(self, hass, config_entry: ConfigEntry):
        config = config_entry.data
        await self.async_update_credentials(config)  # type: ignore
        await super().entry_update_listener(hass, config_entry)

    def get_logger_name(self) -> str:
        return f"profile_{self.loggable_profile_id(self.id)}"

    # interface: ApiProfile
    def attach_mqtt(self, device: MerossDevice):
        with self.exception_warning("attach_mqtt"):
            mqttconnection = self._get_mqttconnection(device.mqtt_broker)
            mqttconnection.attach(device)
            if mqttconnection.state_inactive:
                mqttconnection.schedule_connect(mqttconnection.broker)

    # interface: self
    @property
    def app_id(self):
        return self._data[self.KEY_APP_ID]

    @property
    def token_is_valid(self):
        return bool(self._data.get(mc.KEY_TOKEN))

    @property
    def userid(self):
        return self.config[mc.KEY_USERID_]

    def device_is_registered(self, key: str, descriptor: MerossDeviceDescriptor):
        """extensive check that the device is 'really' binded to the profile"""
        # this check, in a 'goldylock' scenario whould be pretty simple:
        # check that the device userId matches the profile Id since they represent
        # the same info. But when we rebind devices in the wild, the userId
        # in the device become 'untrustable' since you could set any value while pairing.
        # We'll then apply a 'best-effort' approach to verify if the device is (still)
        # binded by veryifing multiple conditions
        if (key != self.key) or (descriptor.userId != self.userid):
            # of course they need to match!
            return False

        device_brokers = descriptor.brokers
        if mqtt_domain := self.config.get(mc.KEY_MQTTDOMAIN):
            broker = HostAddress.build(mqtt_domain)
            if broker in device_brokers:
                return True

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
        """
        Device linking to a cloud profile sets the environment for
        the device MQTT attachment/connection. This process uses a lot
        of euristics to ensure the device really belongs to this cloud
        profile.
        A device binded to a cloud profile should:
        - have the same userid
        - have the same key
        - have a broker address compatible with the profile available brokers
        - be present in the device_info db
        The second check could be now enforced since the new Meross signin api
        tells us ('mqttDomain') which is the (only) broker assigned to this profile.
        It was historically not this way since devices binded to a cloud account could
        be spread among a pool of brokers.
        Presence in the device_info db might be unreliable since the query is only
        done once in 24 hours and thus, the db being out of sync
        """
        if (device.key != self.key) or (device.descriptor.userId != self.userid):
            return False
        if super().try_link(device):
            if device_info := self._data[self.KEY_DEVICE_INFO].get(device.id):
                device.update_device_info(device_info)
            if latest_version := self.get_latest_version(device.descriptor):
                device.update_latest_version(latest_version)
            return True
        return False

    def get_device_info(self, uuid: str):
        return self._data[self.KEY_DEVICE_INFO].get(uuid)

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
            remove_issue(mlc.ISSUE_CLOUD_TOKEN_EXPIRED, self.id)
            curr_credentials = self.apiclient.credentials
            if not curr_credentials or (
                curr_credentials[mc.KEY_TOKEN] != credentials[mc.KEY_TOKEN]
            ):
                self.log(self.DEBUG, "Updating credentials with new token")
                if curr_credentials:
                    await self.apiclient.async_logout_safe()
                self.apiclient.credentials = credentials
                self._data[mc.KEY_TOKEN] = credentials[mc.KEY_TOKEN]
                self._schedule_save_store()
                # the 'async_check_query_devices' will only occur if we didn't refresh
                # on our polling schedule for whatever reason (invalid token -
                # no connection - whatsoever) so, having a fresh token and likely
                # good connectivity we're going to retrigger that
                if self.need_query_device_info():
                    await self.async_query_device_info()

    async def async_query_device_info(self):
        async with self._async_credentials_manager(
            "async_query_device_info"
        ) as credentials:
            if not credentials:
                return None
            self.log(
                self.DEBUG,
                "Querying device list - last query was at: %s",
                datetime_from_epoch(self._device_info_time).isoformat(),
            )
            self._device_info_time = time()
            device_info_new = await self.apiclient.async_device_devlist()
            await self._process_device_info_new(device_info_new)
            self._data[self.KEY_DEVICE_INFO_TIME] = self._device_info_time
            self._schedule_save_store()
            # retrigger the poll at the right time since async_query_devices
            # might be called for whatever reason 'asynchronously'
            # at any time (say the user does a new cloud login or so...)
            if self._unsub_polling_query_device_info:
                self._unsub_polling_query_device_info.cancel()
            self._unsub_polling_query_device_info = schedule_async_callback(
                self.hass,
                mlc.PARAM_CLOUDPROFILE_QUERY_DEVICELIST_TIMEOUT,
                self._async_polling_query_device_info,
            )
            # this is a 'low relevance task' as a new feature (in 4.3.0) to just provide hints
            # when new updates are available: we're not going (yet) to manage the
            # effective update since we're not able to do any basic validation
            # of the whole process and it might be a bit 'dangerous'
            await self.async_check_query_latest_version(self._device_info_time)
            return device_info_new

        return None

    def need_query_device_info(self):
        return (
            time() - self._device_info_time
        ) > mlc.PARAM_CLOUDPROFILE_QUERY_DEVICELIST_TIMEOUT

    async def async_check_query_latest_version(self, epoch: float):
        if (
            self.config.get(CONF_CHECK_FIRMWARE_UPDATES)
            and (epoch - self._data[self.KEY_LATEST_VERSION_TIME])
            > mlc.PARAM_CLOUDPROFILE_QUERY_LATESTVERSION_TIMEOUT
        ):
            self._data[self.KEY_LATEST_VERSION_TIME] = epoch
            async with self._async_credentials_manager(
                "async_check_query_latest_version"
            ) as credentials:
                if not credentials:
                    return
                self._data[self.KEY_LATEST_VERSION] = (
                    await self.apiclient.async_device_latestversion()
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

        async def _add_connection(domain: str | None):
            if not domain:
                return
            broker = HostAddress.build(domain)
            for mqttconnection in mqttconnections:
                if mqttconnection.broker == broker:
                    return
            mqttconnection = await self._async_get_mqttconnection(broker)
            if mqttconnection:
                mqttconnections.append(mqttconnection)

        await _add_connection(self.config.get(mc.KEY_MQTTDOMAIN))

        if device_info := self.get_device_info(device_id):
            await _add_connection(device_info.get(mc.KEY_DOMAIN))
            await _add_connection(device_info.get(mc.KEY_RESERVEDDOMAIN))

        return mqttconnections

    def _get_mqttconnection(self, broker: HostAddress) -> MerossMQTTConnection:
        """
        Returns an existing connection from the managed pool or create one and add
        to the mqttconnections pool. The connection state is not ensured.
        """
        connection_id = str(broker)
        if connection_id in self.mqttconnections:
            return self.mqttconnections[connection_id]  # type: ignore
        return MerossMQTTConnection(self, broker)

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
        try:
            await asyncio.wait_for(await mqttconnection.async_connect(broker), 5)
            return mqttconnection
        except Exception as exception:
            self.log_exception(
                self.DEBUG, exception, "waiting to subscribe to %s", str(broker)
            )
            return None

    async def _async_token_missing(self, should_raise_issue: bool):
        """
        Called when the stored token is dropped (expired) or when needed.
        Tries silently (re)login or raises an issue.
        """
        with self.exception_warning("_async_token_missing"):
            config = self.config
            if (mlc.CONF_PASSWORD not in config) or (config.get(mlc.CONF_MFA_CODE)):
                if should_raise_issue:
                    create_issue(
                        mlc.ISSUE_CLOUD_TOKEN_EXPIRED,
                        self.id,
                        severity=IssueSeverity.WARNING,
                        translation_placeholders={"email": config.get(mc.KEY_EMAIL)},
                    )
                return None
            data = self._data
            if (_time := time()) < data[
                self.KEY_TOKEN_REQUEST_TIME
            ] + mlc.PARAM_CLOUDPROFILE_QUERY_DEVICELIST_TIMEOUT:
                return None
            data[self.KEY_TOKEN_REQUEST_TIME] = _time
            self._schedule_save_store()
            credentials = await self.apiclient.async_token_refresh(
                config[CONF_PASSWORD], config
            )
            # set our (stored) key so the ConfigEntry update will find everything in place
            # and not trigger any side effects. No need to re-trigger _schedule_save_store
            # since it should still be pending...
            data[mc.KEY_TOKEN] = credentials[mc.KEY_TOKEN]
            self.log(self.INFO, "Cloud api token was automatically refreshed")
            helper = ConfigEntriesHelper(self.hass)
            profile_entry = helper.get_config_entry(f"profile.{self.id}")
            if profile_entry:
                # weird enough if this isnt true...
                profile_config = dict(profile_entry.data)
                profile_config.update(credentials)
                # watchout: this will in turn call async_update_credentials
                helper.config_entries.async_update_entry(
                    profile_entry,
                    data=profile_config,
                )
            return credentials

        return None

    @asynccontextmanager
    async def _async_credentials_manager(self, msg: str, *args, **kwargs):
        try:
            # this is called every time we'd need a token to query the cloudapi
            # it just yields the current one or tries it's best to recover a fresh
            # token with a guard to avoid issuing too many requests...
            credentials = self.apiclient.credentials or (
                await self._async_token_missing(False)
            )
            if not credentials:
                self.log(self.WARNING, f"{msg} cancelled: missing cloudapi token")
            yield credentials
        except CloudApiError as clouderror:
            if clouderror.apistatus in APISTATUS_TOKEN_ERRORS:
                self.apiclient.credentials = None
                if self._data.pop(mc.KEY_TOKEN, None):  # type: ignore
                    await self._async_token_missing(True)
            self.log_exception(self.WARNING, clouderror, msg)
        except Exception as exception:
            self.log_exception(self.WARNING, exception, msg)

    async def _async_polling_query_device_info(self):
        try:
            self._unsub_polling_query_device_info = None
            await self.async_query_device_info()
        finally:
            if self._unsub_polling_query_device_info is None:
                # this happens when 'async_query_devices' is unable to
                # retrieve fresh cloud data for whatever reason
                self._unsub_polling_query_device_info = schedule_async_callback(
                    self.hass,
                    mlc.PARAM_CLOUDPROFILE_QUERY_DEVICELIST_TIMEOUT,
                    self._async_polling_query_device_info,
                )

    async def _async_query_subdevices(self, device_id: str):
        async with self._async_credentials_manager(
            "_async_query_subdevices"
        ) as credentials:
            if not credentials:
                return None
            self.log(
                self.DEBUG,
                "Querying hub subdevice list (uuid:%s)",
                self.loggable_device_id(device_id),
            )
            return await self.apiclient.async_hub_getsubdevices(device_id)
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
                self.DEBUG,
                "The uuid:%s has been removed from the cloud profile",
                self.loggable_device_id(device_id),
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
                self.WARNING,
                "Meross cloud api reported new devices but MQTT publishing is disabled: skipping automatic discovery",
                timeout=604800,  # 1 week
            )
            return

        config_entries_helper = ConfigEntriesHelper(self.hass)
        for device_info in device_info_unknown:
            with self.exception_warning("_process_device_info_unknown"):
                device_id = device_info[mc.KEY_UUID]
                self.log(
                    self.DEBUG,
                    "Trying/Initiating discovery for (new) uuid:%s",
                    self.loggable_device_id(device_id),
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
            _data_func, mlc.PARAM_CLOUDPROFILE_DELAYED_SAVE_TIMEOUT
        )
