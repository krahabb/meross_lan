from abc import abstractmethod
from typing import TYPE_CHECKING, override

from homeassistant.config_entries import SOURCE_INTEGRATION_DISCOVERY
from homeassistant.core import callback

from .. import const as mlc
from ..merossclient import HostAddress
from ..merossclient.client import Transport
from ..merossclient.client.mqtt import AbstractMQTTConnection
from ..merossclient.protocol import const as mc, namespaces as mn
from ..merossclient.protocol.message import MerossResponse, get_replykey
from ..sensor import DiagnosticSensor

# import core modules instead of symbols to ease patching in a single place
from .manager import ConfigEntryManager

if TYPE_CHECKING:
    from typing import (
        Callable,
        ClassVar,
        Final,
        Mapping,
        Never,
        NotRequired,
        Self,
        TypedDict,
        Unpack,
    )

    from homeassistant.components import mqtt as ha_mqtt
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.helpers.service_info.mqtt import MqttServiceInfo
    import paho.mqtt.client as paho_mqtt

    from ..merossclient import HostAddress
    from ..merossclient.client import Direction
    from ..merossclient.cloudapi import DeviceInfoType, LatestVersionType
    from ..merossclient.logging import Loggable
    from ..merossclient.protocol.message import MerossMessage
    from .component_api import ComponentApi
    from .device import Device


class ConnectionSensor(DiagnosticSensor):

    if TYPE_CHECKING:
        STATE_DISCONNECTED: Final
        STATE_CONNECTED: Final
        STATE_DROPPING: Final
        ATTR_DEVICES: Final
        ATTR_RECEIVED: Final
        ATTR_PUBLISHED: Final
        ATTR_DROPPED: Final

        parent: Final["MQTTProfile"]  # type: ignore[override]
        connection: Final["MQTTConnection"]

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
    _attr_available = True
    _attr_device_class = DiagnosticSensor.DeviceClass.ENUM
    _unrecorded_attributes = frozenset(
        {
            ATTR_DEVICES,
            ATTR_RECEIVED,
            ATTR_PUBLISHED,
            ATTR_DROPPED,
            *DiagnosticSensor._unrecorded_attributes,
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
                device.id: device.display_name
                for device in connection._client_devices.values()
            },
            ConnectionSensor.ATTR_RECEIVED: 0,
            ConnectionSensor.ATTR_PUBLISHED: 0,
            ConnectionSensor.ATTR_DROPPED: 0,
        }
        DiagnosticSensor.__init__(
            self,
            None,
            connection.parent,
            entity_key=str(connection.id),
            native_value=(
                self.STATE_CONNECTED
                if connection.is_connected
                else self.STATE_DISCONNECTED
            ),
        )
        connection.sensor_connection = self

    def shutdown(self):
        DiagnosticSensor.shutdown(self)
        self.connection.sensor_connection = None
        del self.connection  # type: ignore[del]

    # interface: Loggable
    def configure_logger(self):
        self.logtag = f"{self.__class__.__name__}({self.parent.loggable_broker(self.connection.id)})"

    # interface: self
    def update_devices(self):
        # rebuild the attr (sub)dict else we were keeping a reference
        # to the underlying hass.state and updates were missing
        self.extra_state_attributes[ConnectionSensor.ATTR_DEVICES] = {
            device.id: device.display_name
            for device in self.connection._client_devices.values()
        }
        self.flush_state()


class MQTTConnection(AbstractMQTTConnection):
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

    class Client(AbstractMQTTConnection.Client):
        """Implements  a 'soft' client for a single device over an MQTTConnection."""

        if TYPE_CHECKING:
            id: Final[HostAddress]  # type: ignore[override]
            device: Final[Device]  # type: ignore[override]
            connection: Final["MQTTConnection"]  # type: ignore[override]

            class ConnectArgs(AbstractMQTTConnection.Client.ConnectArgs):
                pass

            class RequestRawArgs(AbstractMQTTConnection.Client.RequestRawArgs):
                pass

        __slots__ = ()

        def __init__(
            self,
            connection: "MQTTConnection",
            parent: "Loggable",
            /,
            uuid: str,
            key: str,
        ):
            AbstractMQTTConnection.Client.__init__(
                self,
                connection.id,
                parent,
                connection=connection,
                uuid=uuid,
                key=key,
                loop=connection.loop,
            )

        @override
        def on_async_mqtt_message(self, message: "MerossMessage", /):
            """Message processing entry point for MQTT (PUSH) messages."""
            AbstractMQTTConnection.Client.on_async_mqtt_message(self, message)
            device = self.device
            if not device.is_connected:
                device.on_connect()
                device.polling_start()
            device._handle(message)

    if TYPE_CHECKING:

        parent: Final["MQTTProfile"]  # type: ignore[override]
        _client_devices: Final[dict[str, Device]]  # type: ignore[override]

        class Args(AbstractMQTTConnection.Args):
            pass

        type SessionHandlersType = Mapping[
            str,
            Callable[[Self, MerossMessage], bool],
        ]

        SESSION_HANDLERS: ClassVar[SessionHandlersType]

        mqttdiscovering: Final[set[str]]
        session_handlers: SessionHandlersType
        sensor_connection: ConnectionSensor | None

    __SLOTS__ = (
        "mqttdiscovering",
        "session_handlers",
        "sensor_connection",
    )

    def __init__(
        self,
        broker: "HostAddress",
        profile: "MQTTProfile",
        /,
        **kwargs: "Unpack[Args]",
    ):
        self.mqttdiscovering = set()
        self.session_handlers = self.__class__.SESSION_HANDLERS
        self.sensor_connection = None
        kwargs["key"] = profile.key
        kwargs["is_cloud"] = profile.is_cloud_profile
        kwargs["allow_publish"] = profile.allow_mqtt_publish
        super().__init__(
            broker,
            profile,
            **kwargs,
        )
        profile.mqttconnections[str(broker)] = self

    def shutdown(self):
        super().shutdown()
        self.sensor_connection = None

    @override  # Loggable
    def configure_logger(self, /):
        self.logtag = (
            f"{self.__class__.__name__}({self.parent.loggable_broker(self.id)})"
        )

    @callback
    @override
    def on_connect(self, /):
        super().on_connect()
        if self.sensor_connection:
            self.sensor_connection.update_native_value(ConnectionSensor.STATE_CONNECTED)

    @callback
    @override
    def on_disconnect(self, /):
        super().on_disconnect()
        if self.sensor_connection:
            self.sensor_connection.update_native_value(
                ConnectionSensor.STATE_DISCONNECTED
            )

    @override
    def log_message(self, message: "MerossMessage", direction: "Direction", /):
        super().log_message(message, direction)
        if self.parent.is_tracing:
            self.parent.trace_msg(self.time(), message, self.TRANSPORT, direction)

    @callback
    @override
    def on_message(
        self,
        mqtt_msg: "ha_mqtt.ReceiveMessage | paho_mqtt.MQTTMessage | MqttServiceInfo",
        /,
    ):
        with self.exception_warning("async_mqtt_message"):
            if sensor_connection := self.sensor_connection:
                sensor_connection.extra_state_attributes[
                    ConnectionSensor.ATTR_RECEIVED
                ] += 1
                sensor_connection.flush_state()

            mqtt_payload = mqtt_msg.payload
            message = MerossResponse(
                mqtt_payload
                if type(mqtt_payload) is str
                else mqtt_payload.decode("utf-8")  # type: ignore
            )
            self.log_message(message, self.Direction.RX)
            # first check among pending transactions (i.e. replies to our requests)
            try:
                self._transactions[message.messageid].set_result(message)
                return
            except KeyError:
                pass

            # then check for any special 'cloud' session management:
            # cloud connections would behave differently than local MQTT.
            # The behavior will definitely be set in the dynamic/custom
            # message handlers implemented in the derived MQTTConnection
            try:
                if self.session_handlers[message.namespace](self, message):
                    # session management has already taken care of everything
                    return
            except Exception as e:
                if (type(e) is not KeyError) or (e.args[0] != message.namespace):
                    self.log_exception(
                        self.DEBUG,
                        e,
                        "async_mqtt_message session handler for namespace %s (uuid:%s)",
                        message.namespace,
                        uuid=message.uuid,
                        timeout=14400,
                    )

            # then route to the device if already binded (should be the common case
            # when PUSH messages are broadcasted by the device)
            try:
                self._client_devices[message.uuid].mqtt.on_async_mqtt_message(message)  # type: ignore[union-attr]
                return
            except KeyError as key_error:
                if key_error.args[0] != message.uuid:
                    raise

            uuid = message.uuid
            profile = self.parent
            api = profile.parent
            # device_id is not binded to this MQTTConnection
            if device := api.devices.get(uuid):
                # check among current loaded devices if they could be re-binded
                if device.configured_transport not in (Transport.AUTO, Transport.MQTT):
                    self.log(
                        self.DEBUG,
                        "Dropping MQTT message for device '%s' since its transport is set to '%s'",
                        device.display_name,
                        device.configured_transport,
                        timeout=86400,
                    )
                    return
                if device.profile == profile:
                    client = self.attach(device)
                else:
                    if (device.key != profile.key) or (
                        device.descriptor.userId != profile.id
                    ):
                        # this is not really expected and deserves a warning but is expected
                        # when you (re)bind a device and it still is connected to the old broker
                        # until reboot
                        self.log(
                            self.WARNING,
                            "Received MQTT message for device '%s' which cannot be registered for MQTT handling on this profile",
                            device.display_name,
                            timeout=14400,
                        )
                        return
                    profile.link(device)
                    # profile.link will attach to the mqtt broker known to the device cfg..
                    # we'll ensure that (in case device cfg is stale) we're correctly binded here
                    client = device.mqtt
                    if client is None:
                        client = self.attach(device)
                    elif client.connection != self:
                        device.remove_client(client)
                        client = self.attach(device)

                client.on_async_mqtt_message(message)
                return

            # the device is not configured: proceed to discovery in case
            if uuid in self.mqttdiscovering:
                return

            # lookout for any disabled/ignored entry
            if (
                (profile is api)
                and (not api.get_config_entry(mlc.DOMAIN))
                and (not api.get_config_flow(mlc.DOMAIN))
            ):
                # not really needed but we would like to always have the
                # MQTT hub entry in case so if the user removed that..retrigger
                api.create_task(
                    api.flow_manager.async_init(
                        mlc.DOMAIN,
                        context={"source": "hub"},
                        data=None,
                    ),
                    ".async_init(hub)",
                    eager_start=True,
                )

            if config_entry := (
                api.get_config_entry(uuid) or api.get_config_entry(uuid[-12:].lower())
            ):
                # entry already present...skip discovery
                self.log(
                    self.INFO,
                    "Ignoring MQTT discovery for %s uuid:%s",
                    (
                        "disabled"
                        if config_entry.disabled_by
                        else (
                            "ignored"
                            if config_entry.source == "ignore"
                            else "configured"
                        )
                    ),
                    uuid=uuid,
                    timeout=28800,  # type: ignore
                )
                return

            # also skip discovered integrations waiting in HA queue
            if api.get_config_flow(uuid):
                self.log(
                    self.DEBUG,
                    "Ignoring MQTT discovery for uuid:%s (ConfigFlow is in progress)",
                    uuid=uuid,
                    timeout=14400,  # type: ignore
                )
                return

            key = profile.key
            if get_replykey(message.header, key) is not key:
                self.log(
                    self.WARNING,
                    "Discovery key error for uuid:%s",
                    uuid=uuid,
                    timeout=300,
                )
                if key is not None:
                    return

            self.create_task(
                self.async_try_discovery(uuid),
                f".async_try_discovery({uuid})",
                eager_start=True,
            )

    @override
    def on_publish(self, /):
        if sensor_connection := self.sensor_connection:
            sensor_connection.extra_state_attributes[
                ConnectionSensor.ATTR_PUBLISHED
            ] += 1
            sensor_connection.native_value = ConnectionSensor.STATE_CONNECTED
            sensor_connection.flush_state()

    @override
    def on_drop(self, /):
        if sensor_connection := self.sensor_connection:
            sensor_connection.extra_state_attributes[ConnectionSensor.ATTR_DROPPED] += 1
            sensor_connection.native_value = ConnectionSensor.STATE_DROPPING
            sensor_connection.flush_state()

    # interface: self
    async def async_create_diagnostic_entities(self, /):
        if not self.sensor_connection:
            ConnectionSensor(self)

    def entry_update_listener(self, profile: "MQTTProfile"):
        """Called by the ApiProfile to propagate config changes"""
        self.configure_logger()
        if self.sensor_connection:
            self.sensor_connection.configure_logger()

        self.allow_publish = profile.allow_mqtt_publish  # type: ignore[assignment]
        if self.allow_publish:
            self.can_publish = self.is_connected  # type: ignore[assignment]
            # restore class method
            try:
                del self.async_publish_raw
            except AttributeError:
                pass
        else:
            # install a method override to forcibly disable MQTT publish
            self.can_publish = False  # type: ignore[assignment]
            self.async_publish_raw = MQTTConnection._async_publish_raw_disabled

    def attach(self, device: "Device", /):
        client = MQTTConnection.Client(self, device, uuid=device.id, key=device.key)
        device.add_client(client)
        return client

    async def async_identify_device(self, uuid: str, key: str, /):
        """
        Sends an ns_all and ns_ability GET requests encapsulated in an ns_multiple
        to speed up things. Raises exception in case of error
        """
        descriptor = await self.async_identify(uuid=uuid, key=key)
        return (
            mlc.DeviceConfigType(
                {
                    mlc.CONF_DEVICE_ID: descriptor.uuid,
                    mlc.CONF_PAYLOAD: descriptor.payload,
                    mlc.CONF_KEY: key,
                }
            ),
            descriptor,
        )

    async def async_try_discovery(self, uuid: str, /):
        """
        Tries device identification and starts a flow if succeded returning
        the FlowResult. Returns None if anything fails for whatever reason.
        """
        self.mqttdiscovering.add(uuid)
        try:
            device_config, descriptor = await self.async_identify_device(
                uuid, self.parent.key
            )
            return await self.parent.parent.flow_manager.async_init(
                mlc.DOMAIN,
                context={"source": SOURCE_INTEGRATION_DISCOVERY},
                data=device_config,
            )
        except Exception as e:
            self.log_exception(
                self.WARNING,
                e,
                "async_try_discovery (uuid:%s)",
                uuid=uuid,
                timeout=14400,
            )
            return None
        finally:
            self.mqttdiscovering.remove(uuid)

    def _handle_Appliance_System_Online(self, message: "MerossMessage", /):
        """
        This is likely sent by the session management layer on the Meross brokers
        to notify the app of the device connection state. We then intercept
        this message which is not intended for the device though and act accordingly
        here at our 'session management state'. At any rate, this will be set to be
        handled in every MQTTConnection (cloud, local) so we process messages
        eventually originated from the device itself.
        Returns False when device is online so that device message handling will
        trigger onlining the device itself.
        """
        return (message.method != mc.METHOD_PUSH) or (
            message.payload[mc.KEY_ONLINE].get(mc.KEY_STATUS) != mc.STATUS_ONLINE
        )


MQTTConnection.SESSION_HANDLERS = {
    mn.Appliance_System_Online: MQTTConnection._handle_Appliance_System_Online,
}


class MQTTProfile(ConfigEntryManager):
    """
    Base class for both MerossProfile and ComponentApi allowing lightweight
    sharing of globals and defining some common interfaces.
    """

    if TYPE_CHECKING:
        linkeddevices: Final[dict[str, Device]]
        mqttconnections: Final[dict[str, MQTTConnection]]

    __slots__ = (
        "linkeddevices",
        "mqttconnections",
    )

    def __init__(
        self,
        id: str,
        api: "ComponentApi",
        config_entry: "ConfigEntry | None" = None,
        /,
        **kwargs: "Unpack[MQTTProfile.Args]",
    ):
        ConfigEntryManager.__init__(self, id, api, config_entry, **kwargs)
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
        await ConfigEntryManager.async_shutdown(self)

    async def entry_update_listener(self, hass, config_entry: "ConfigEntry"):
        await ConfigEntryManager.entry_update_listener(self, hass, config_entry)
        for mqttconnection in self.mqttconnections.values():
            mqttconnection.entry_update_listener(self)

    async def async_create_diagnostic_entities(self):
        await ConfigEntryManager.async_create_diagnostic_entities(self)
        for mqttconnection in self.mqttconnections.values():
            await mqttconnection.async_create_diagnostic_entities()

    # interface: self
    @property
    @abstractmethod
    def is_cloud_profile(self) -> bool:
        pass

    @property
    def allow_mqtt_publish(self) -> bool:
        return bool(self.config.get(mlc.CONF_ALLOW_MQTT_PUBLISH))

    @property
    @abstractmethod
    def userid(self) -> str:
        pass

    def get_device_info(self, uuid: str) -> "DeviceInfoType | None":
        return None

    def get_latest_version(
        self, type: str, subtype: str, /
    ) -> "LatestVersionType | None":
        return None

    def get_latest_versions(self, /) -> dict | None:
        return None

    def link(self, device: "Device"):
        assert device.id not in self.linkeddevices
        device.profile_linked(self)
        self.linkeddevices[device.id] = device

    def unlink(self, device: "Device"):
        self.linkeddevices.pop(device.id).profile_unlinked()

    @abstractmethod
    def get_connection(self, device: "Device") -> "MQTTConnection":
        pass
