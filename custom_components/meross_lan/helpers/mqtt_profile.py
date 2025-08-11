import abc
import asyncio
from time import time
from typing import TYPE_CHECKING, final

from homeassistant.components.sensor import DOMAIN as SENSOR_DOMAIN
from homeassistant.config_entries import SOURCE_INTEGRATION_DISCOVERY
from homeassistant.core import callback

from . import Loggable, entity as me
from .. import const as mlc
from ..const import (
    CONF_ALLOW_MQTT_PUBLISH,
    CONF_PROTOCOL_MQTT,
    DOMAIN,
)
from ..merossclient import (
    HostAddress,
    json_dumps,
)
from ..merossclient.mqttclient import MerossMQTTRateLimitException
from ..merossclient.protocol import MerossKeyError, const as mc, namespaces as mn
from ..merossclient.protocol.message import (
    MerossRequest,
    MerossResponse,
    check_message_strict,
    get_message_uuid,
    get_replykey,
)
from ..sensor import MLDiagnosticSensor
from .manager import ConfigEntryManager
from .obfuscate import obfuscated_dict

if TYPE_CHECKING:
    import asyncio
    from typing import Awaitable, Callable, ClassVar, Final, Mapping, TypedDict, Unpack

    from homeassistant.components import mqtt as ha_mqtt
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.helpers.service_info.mqtt import MqttServiceInfo
    import paho.mqtt.client as paho_mqtt

    from ..merossclient import HostAddress
    from ..merossclient.protocol.message import MerossMessage
    from ..merossclient.protocol.types import (
        MerossHeaderType,
        MerossPayloadType,
    )
    from .device import Device


class ConnectionSensor(me.MEAlwaysAvailableMixin, MLDiagnosticSensor):

    if TYPE_CHECKING:
        STATE_DISCONNECTED: Final
        STATE_CONNECTED: Final
        STATE_DROPPING: Final
        ATTR_DEVICES: Final
        ATTR_RECEIVED: Final
        ATTR_PUBLISHED: Final
        ATTR_DROPPED: Final

        manager: "MQTTProfile"

        # HA core entity attributes:
        class AttrDictType(TypedDict):
            devices: dict[str, str]
            received: int
            published: int
            dropped: int

        extra_state_attributes: AttrDictType
        native_value: str
        options: list[str]

    STATE_DISCONNECTED = "disconnected"
    STATE_CONNECTED = "connected"
    STATE_DROPPING = "dropping"

    ATTR_DEVICES = "devices"
    ATTR_RECEIVED = "received"
    ATTR_PUBLISHED = "published"
    ATTR_DROPPED = "dropped"

    # HA core entity attributes:
    _unrecorded_attributes = frozenset(
        {
            ATTR_DEVICES,
            ATTR_RECEIVED,
            ATTR_PUBLISHED,
            ATTR_DROPPED,
            *MLDiagnosticSensor._unrecorded_attributes,
        }
    )

    options = [
        STATE_DISCONNECTED,
        STATE_CONNECTED,
        STATE_DROPPING,
    ]

    __slots__ = ("connection",)

    def __init__(self, connection: "MQTTConnection"):
        self.connection = connection
        self.extra_state_attributes = {
            ConnectionSensor.ATTR_DEVICES: {
                device.id: device.name for device in connection.mqttdevices.values()
            },
            ConnectionSensor.ATTR_RECEIVED: 0,
            ConnectionSensor.ATTR_PUBLISHED: 0,
            ConnectionSensor.ATTR_DROPPED: 0,
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
        mqtt_connection: "MQTTConnection",
        device_id: str,
        request: "MerossMessage",
    ):
        self.mqtt_connection = mqtt_connection
        self.device_id = device_id
        self.namespace = request.namespace
        self.messageid = request.messageid
        self.method = request.method
        self.request_time = time()
        self.response_future: "asyncio.Future[MerossResponse]" = (
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
    broker. Historically, MQTT support was only through ComponentApi
    and the HA core MQTT broker. The introduction of Meross cloud
    connection has 'generalized' the concept of the MQTT broker.
    This interface is used by devices to actually send/receive
    MQTT messages (in place of the legacy approach using ComponentApi)
    and represents a link to a broker (either through HA or a
    merosss cloud mqtt)
    """

    if TYPE_CHECKING:
        _MQTT_DROP: Final
        _MQTT_PUBLISH: Final
        _MQTT_RECV: Final

        SessionHandlersType = Mapping[
            str,
            Callable[
                ["MQTTConnection", str, MerossHeaderType, MerossPayloadType],
                Awaitable[bool],
            ],
        ]

        SESSION_HANDLERS: ClassVar[SessionHandlersType]
        is_cloud_connection: bool
        profile: Final["MQTTProfile"]
        broker: Final[HostAddress]
        topic_response: Final[str]
        mqttdevices: Final[dict[str, "Device"]]
        mqttdiscovering: Final[set[str]]
        namespace_handlers: SessionHandlersType
        sensor_connection: ConnectionSensor | None

        _mqtt_transactions: Final[dict[str, _MQTTTransaction]]
        _mqtt_is_connected: bool

    _MQTT_DROP = "DROP"
    _MQTT_PUBLISH = "PUBLISH"
    _MQTT_RECV = "RECV"

    DEFAULT_RESPONSE_TIMEOUT = 5

    SESSION_HANDLERS = {}

    __slots__ = (
        "profile",
        "broker",
        "topic_response",
        "mqttdevices",
        "mqttdiscovering",
        "namespace_handlers",
        "is_cloud_connection",
        "sensor_connection",
        "_mqtt_transactions",
        "_mqtt_is_connected",
    )

    def __init__(
        self,
        profile: "MQTTProfile",
        broker: "HostAddress",
        topic_response: str,
    ):
        self.profile = profile
        self.broker = broker
        self.topic_response = topic_response
        self.mqttdevices = {}
        self.mqttdiscovering = set()
        self.namespace_handlers = self.__class__.SESSION_HANDLERS
        self.sensor_connection = None
        # self.is_cloud_connection = False to be fixed in derived
        self._mqtt_transactions = {}
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

    async def entry_update_listener(self, profile: "MQTTProfile"):
        """Called by the ApiProfile to propagate config changes"""
        self.configure_logger()
        if self.sensor_connection:
            self.sensor_connection.configure_logger()

    @abc.abstractmethod
    def get_rl_safe_delay(self, uuid: str):
        raise NotImplementedError()

    @property
    def mqtt_is_connected(self):
        return self._mqtt_is_connected

    def attach(self, device: "Device"):
        assert device.id not in self.mqttdevices, (
            "unexpected MQTTConnection.attach",
            device.id,
        )
        device.mqtt_attached(self)
        self.mqttdevices[device.id] = device
        if sensor_connection := self.sensor_connection:
            sensor_connection.update_devices()

    def detach(self, device: "Device"):
        device_id = device.id
        assert device_id in self.mqttdevices, (
            "unexpected MQTTConnection.detach",
            device_id,
        )
        for mqtt_transaction in list(self._mqtt_transactions.values()):
            if mqtt_transaction.device_id == device_id:
                mqtt_transaction.cancel()
        device.mqtt_detached()
        self.mqttdevices.pop(device_id)
        if sensor_connection := self.sensor_connection:
            sensor_connection.update_devices()

    @final
    def mqtt_publish(
        self,
        device_id: str,
        request: "MerossMessage",
    ):
        return self.profile.async_create_task(
            self.async_mqtt_publish(device_id, request), f".mqtt_publish({device_id})"
        )

    @final
    async def async_mqtt_publish(
        self,
        device_id: str,
        request: "MerossMessage",
    ) -> MerossResponse | None:
        if request.method in mc.METHOD_ACK_MAP.keys():
            transaction = _MQTTTransaction(self, device_id, request)
        else:
            transaction = None
        try:
            self.profile.trace_or_log(self, device_id, request, MQTTProfile.TRACE_TX)
            await self._async_mqtt_publish(device_id, request)
            if transaction:
                try:
                    return await asyncio.wait_for(
                        transaction.response_future, self.DEFAULT_RESPONSE_TIMEOUT
                    )
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
                finally:
                    self._mqtt_transactions.pop(transaction.messageid, None)
            return None

        except MerossMQTTRateLimitException:
            if sensor_connection := self.sensor_connection:
                sensor_connection.inc_counter_with_state(
                    ConnectionSensor.ATTR_DROPPED,
                    ConnectionSensor.STATE_DROPPING,
                )
            self.log(
                self.WARNING,
                "MQTT publish rate-limit exceeded for device uuid:%s",
                self.profile.loggable_device_id(device_id),
            )

        except Exception as exception:
            self.log_exception(
                self.WARNING,
                exception,
                "async_mqtt_publish %s %s (uuid:%s messageId:%s)",
                request.method,
                request.namespace,
                self.profile.loggable_device_id(device_id),
                request.messageid,
                timeout=14400,
            )

        if transaction:
            transaction.cancel()
        return None

    @final
    async def async_mqtt_message(
        self,
        mqtt_msg: "ha_mqtt.ReceiveMessage | paho_mqtt.MQTTMessage | MqttServiceInfo",
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
            api = profile.api
            profile.trace_or_log(self, device_id, message, MQTTProfile.TRACE_RX)

            try:
                if self._mqtt_transactions[messageid].namespace == namespace:
                    self._mqtt_transactions.pop(messageid).response_future.set_result(
                        message
                    )
            except KeyError:
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

            try:
                self.mqttdevices[device_id].mqtt_receive(message)
                return
            except KeyError:
                # device is not binded to this MQTTConnection
                if device := api.devices.get(device_id):
                    # check among current loaded devices if they could be re-binded
                    if device.conf_protocol is mlc.CONF_PROTOCOL_HTTP:
                        self.log(
                            self.DEBUG,
                            "Dropping MQTT received message for device uuid:%s since it is configured for HTTP only",
                            profile.loggable_device_id(device_id),
                        )
                        return
                    if device._profile == profile:
                        self.attach(device)
                    else:
                        if (device.key != profile.key) or (
                            device.descriptor.userId != profile.id
                        ):
                            # this is not really expected and deserves a warning but is expected
                            # when you (re)bind a device and it still is connected to the old broker
                            # until reboot
                            self.log(
                                self.WARNING,
                                "Received MQTT message for device uuid:%s which cannot be registered for MQTT handling on this profile",
                                profile.loggable_device_id(device_id),
                                timeout=14400,
                            )
                            return
                        profile.link(device)
                        # profile.link will attach to the mqtt broker known to the device cfg..
                        # we'll ensure that (in case device cfg is stale) we're correctly binded here
                        if device._mqtt_connection != self:
                            self.attach(device)

                    device.mqtt_receive(message)
                    return

            # the device is not configured: proceed to discovery in case
            if device_id in self.mqttdiscovering:
                return

            # lookout for any disabled/ignored entry
            if (
                (profile is api)
                and (not api.get_config_entry(DOMAIN))
                and (not api.get_config_flow(DOMAIN))
            ):
                # not really needed but we would like to always have the
                # MQTT hub entry in case so if the user removed that..retrigger
                await api.hass.config_entries.flow.async_init(
                    DOMAIN,
                    context={"source": "hub"},
                    data=None,
                )

            if config_entry := (
                api.get_config_entry(device_id)
                or api.get_config_entry(device_id[-12:].lower())
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
            if api.get_config_flow(device_id):
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

            profile.async_create_task(
                self.async_try_discovery(device_id),
                f".async_try_discovery({device_id})",
            )

    async def async_identify_device(
        self, device_id: str, key: str
    ) -> mlc.DeviceConfigType:
        """
        Sends an ns_all and ns_ability GET requests encapsulated in an ns_multiple
        to speed up things. Raises exception in case of error
        """
        try:
            response = check_message_strict(
                await self.async_mqtt_publish(
                    device_id,
                    MerossRequest(
                        *mn.Appliance_System_Ability.request_get,
                        key,
                        self.topic_response,
                        mlc.DOMAIN,
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
                        *mn.Appliance_System_All.request_get,
                        key,
                        self.topic_response,
                        mlc.DOMAIN,
                    ),
                )
            )
            all = response[mc.KEY_PAYLOAD][mc.KEY_ALL]
        except MerossKeyError as error:
            raise error
        except Exception as exception:
            raise Exception("Unable to identify device (all)") from exception
        return {
            mlc.CONF_DEVICE_ID: device_id,
            mlc.CONF_PAYLOAD: {
                mc.KEY_ALL: all,
                mc.KEY_ABILITY: ability,
            },
            mlc.CONF_KEY: key,
        }

    async def async_try_discovery(self, device_id: str):
        """
        Tries device identification and starts a flow if succeded returning
        the FlowResult. Returns None if anything fails for whatever reason.
        """
        profile = self.profile
        self.mqttdiscovering.add(device_id)
        try:
            result = await profile.hass.config_entries.flow.async_init(
                DOMAIN,
                context={"source": SOURCE_INTEGRATION_DISCOVERY},
                data=await self.async_identify_device(device_id, profile.key),
            )
        except Exception as e:
            result = None
            self.log_exception(
                self.WARNING,
                e,
                "async_try_discovery (uuid:%s)",
                profile.loggable_device_id(device_id),
                timeout=14400,
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
        request: "MerossMessage",
    ):
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
        self, device_id: str, header: "MerossHeaderType", payload: "MerossPayloadType"
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


class MQTTProfile(ConfigEntryManager):
    """
    Base class for both MerossProfile and ComponentApi allowing lightweight
    sharing of globals and defining some common interfaces.
    """

    if TYPE_CHECKING:
        is_cloud_profile: bool
        linkeddevices: dict[str, Device]
        mqttconnections: dict[str, MQTTConnection]

    DEFAULT_PLATFORMS = ConfigEntryManager.DEFAULT_PLATFORMS | {
        SENSOR_DOMAIN: None,
    }

    __slots__ = (
        "is_cloud_profile",
        "linkeddevices",
        "mqttconnections",
    )

    def __init__(self, id: str, **kwargs: "Unpack[ConfigEntryManager.Args]"):
        super().__init__(id, **kwargs)
        self.linkeddevices = {}
        self.mqttconnections = {}

    # interface: ConfigEntryManager
    async def async_shutdown(self):
        for mqttconnection in self.mqttconnections.values():
            await mqttconnection.async_shutdown()
        self.mqttconnections.clear()
        for device in self.linkeddevices.values():
            device.profile_unlinked()
        self.linkeddevices.clear()
        await super().async_shutdown()

    async def entry_update_listener(self, hass, config_entry: "ConfigEntry"):
        config = config_entry.data
        # the ComponentApi always enable (independent of config) mqtt publish
        allow_mqtt_publish = config.get(CONF_ALLOW_MQTT_PUBLISH) or (self is self.api)
        if allow_mqtt_publish != self.allow_mqtt_publish:
            # device._mqtt_publish is rather 'passive' so
            # we do some fast 'smart' updates:
            if allow_mqtt_publish:
                for device in self.linkeddevices.values():
                    device._mqtt_publish = device._mqtt_connected
            else:
                for device in self.linkeddevices.values():
                    device._mqtt_publish = None
        await super().entry_update_listener(hass, config_entry)
        for mqttconnection in self.mqttconnections.values():
            await mqttconnection.entry_update_listener(self)

    async def async_create_diagnostic_entities(self):
        await super().async_create_diagnostic_entities()
        for mqttconnection in self.mqttconnections.values():
            await mqttconnection.async_create_diagnostic_entities()

    # interface: self
    @property
    def allow_mqtt_publish(self):
        return self.config.get(CONF_ALLOW_MQTT_PUBLISH)

    def link(self, device: "Device"):
        device_id = device.id
        assert device_id not in self.linkeddevices
        device.profile_linked(self)
        self.linkeddevices[device_id] = device

    def unlink(self, device: "Device"):
        device_id = device.id
        assert device_id in self.linkeddevices
        device.profile_unlinked()
        self.linkeddevices.pop(device_id)

    @abc.abstractmethod
    def attach_mqtt(self, device: "Device"):
        pass

    def trace_or_log(
        self,
        connection: "MQTTConnection",
        device_id: str,
        message: "MerossMessage",
        rxtx: str,
    ):
        if self.is_tracing:
            header = message[mc.KEY_HEADER]
            self.trace(
                time(),
                message[mc.KEY_PAYLOAD],
                header[mc.KEY_NAMESPACE],
                header[mc.KEY_METHOD],
                CONF_PROTOCOL_MQTT,
                rxtx,
            )
        if self.isEnabledFor(self.VERBOSE):
            header = message[mc.KEY_HEADER]
            connection.log(
                self.VERBOSE,
                "%s(%s) %s %s (uuid:%s messageId:%s) %s",
                rxtx,
                CONF_PROTOCOL_MQTT,
                header[mc.KEY_METHOD],
                header[mc.KEY_NAMESPACE],
                self.loggable_device_id(device_id),
                header[mc.KEY_MESSAGEID],
                (
                    json_dumps(obfuscated_dict(message))
                    if self.obfuscate
                    else message.json()
                ),
            )
        elif self.isEnabledFor(self.DEBUG):
            header = message[mc.KEY_HEADER]
            connection.log(
                self.DEBUG,
                "%s(%s) %s %s (uuid:%s messageId:%s)",
                rxtx,
                CONF_PROTOCOL_MQTT,
                header[mc.KEY_METHOD],
                header[mc.KEY_NAMESPACE],
                self.loggable_device_id(device_id),
                header[mc.KEY_MESSAGEID],
            )
