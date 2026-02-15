from abc import abstractmethod
from typing import TYPE_CHECKING, override

from homeassistant.components.sensor import DOMAIN as SENSOR_DOMAIN
from homeassistant.config_entries import SOURCE_INTEGRATION_DISCOVERY
from homeassistant.core import callback

# import core modules instead of symbols to ease patching in a single place
from . import manager as mlm
from .. import const as mlc
from ..merossclient import HostAddress
from ..merossclient.client import Transport
from ..merossclient.client.mqtt import AbstractMQTTConnection
from ..merossclient.protocol import const as mc, namespaces as mn
from ..merossclient.protocol.message import MerossResponse, get_replykey
from ..sensor import MLDiagnosticSensor

if TYPE_CHECKING:
    from typing import (
        Callable,
        ClassVar,
        Final,
        Mapping,
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
    from ..merossclient.protocol.message import MerossMessage
    from .component_api import ComponentApi
    from .device import Device


class ConnectionSensor(MLDiagnosticSensor):

    if TYPE_CHECKING:
        STATE_DISCONNECTED: Final
        STATE_CONNECTED: Final
        STATE_DROPPING: Final
        ATTR_DEVICES: Final
        ATTR_RECEIVED: Final
        ATTR_PUBLISHED: Final
        ATTR_DROPPED: Final

        manager: "MQTTProfile"
        connection: "MQTTConnection"

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
                device.id: device.display_name
                for device in connection.mqttdevices.values()
            },
            ConnectionSensor.ATTR_RECEIVED: 0,
            ConnectionSensor.ATTR_PUBLISHED: 0,
            ConnectionSensor.ATTR_DROPPED: 0,
        }
        super().__init__(
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

    # interface: Loggable
    def configure_logger(self):
        self.logtag = (
            f"{self.__class__.__name__}({self.manager.loggable_broker(self.id)})"
        )

    # interface: MLDiagnosticSensor
    async def async_shutdown(self):
        await super().async_shutdown()
        self.connection.sensor_connection = None
        del self.connection

    # interface: self
    def update_devices(self):
        # rebuild the attr (sub)dict else we were keeping a reference
        # to the underlying hass.state and updates were missing
        self.extra_state_attributes[ConnectionSensor.ATTR_DEVICES] = {
            device.id: device.display_name
            for device in self.connection.mqttdevices.values()
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

    if TYPE_CHECKING:

        parent: Final["MQTTProfile"]  # type: ignore[override]

        class Args(AbstractMQTTConnection.Args):
            pass

        type SessionHandlersType = Mapping[
            str,
            Callable[[Self, MerossMessage], bool],
        ]

        SESSION_HANDLERS: ClassVar[SessionHandlersType]
        is_cloud_connection: Final[bool]  # type: ignore
        mqttdevices: Final[dict[str, Device]]
        mqttdiscovering: Final[set[str]]
        session_handlers: SessionHandlersType
        sensor_connection: ConnectionSensor | None

    __SLOTS__ = (
        "mqttdevices",
        "mqttdiscovering",
        "session_handlers",
        "is_cloud_connection",
        "sensor_connection",
    )

    def __init__(
        self,
        broker: "HostAddress",
        profile: "MQTTProfile",
        /,
        **kwargs: "Unpack[Args]",
    ):
        # to be set in derived classes
        assert self.is_cloud_connection is not None
        self.mqttdevices = {}
        self.mqttdiscovering = set()
        self.session_handlers = self.__class__.SESSION_HANDLERS
        self.sensor_connection = None
        kwargs["key"] = profile.key
        super().__init__(broker, profile, **kwargs)
        profile.mqttconnections[str(broker)] = self
        if profile.create_diagnostic_entities:
            ConnectionSensor(self)

    async def async_shutdown(self):
        await super().async_shutdown()
        self.mqttdiscovering.clear()
        for device in self.mqttdevices.values():
            device.mqtt_detached()
        self.mqttdevices.clear()
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
        for device in self.mqttdevices.values():
            device.mqtt_connected()
        if self.sensor_connection:
            self.sensor_connection.update_native_value(ConnectionSensor.STATE_CONNECTED)

    @callback
    @override
    def on_disconnect(self, /):
        super().on_disconnect()
        for device in self.mqttdevices.values():
            device.mqtt_disconnected()
        if self.sensor_connection:
            self.sensor_connection.update_native_value(
                ConnectionSensor.STATE_DISCONNECTED
            )

    @override
    def log_message(self, message: "MerossMessage", direction: "Direction", /):
        super().log_message(message, direction)
        if self.parent.is_tracing:
            self.parent.trace(
                self.time(),
                message.payload,
                message.namespace,
                message.method,
                self.TRANSPORT,
                direction,
            )

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
            uuid = message.uuid
            # first check among pending transactions (i.e. replies to our requests)
            try:
                _mqtt_transaction = self._transactions.pop(message.messageid)
                if _mqtt_transaction.uuid == uuid:
                    _mqtt_transaction.response_future.set_result(message)
                    return
                else:  # this is unlikely to happen
                    self._transactions[message.messageid] = _mqtt_transaction
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
                        uuid=uuid,
                        timeout=14400,
                    )

            # then route to the device if already binded (should be the common case
            # when PUSH messages are broadcasted by the device)
            try:
                self.mqttdevices[uuid].mqtt_receive(message)
                return
            except KeyError as key_error:
                if key_error.args[0] != uuid:
                    raise

            profile = self.parent
            api = profile.api
            # device_id is not binded to this MQTTConnection
            if device := api.devices.get(uuid):
                # check among current loaded devices if they could be re-binded
                if device.conf_protocol is Transport.HTTP:
                    self.log(
                        self.DEBUG,
                        "Dropping MQTT received message for device uuid:%s since it is configured for HTTP only",
                        uuid=uuid,
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
                            uuid=uuid,
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
                profile.async_create_task(
                    api.hass.config_entries.flow.async_init(
                        mlc.DOMAIN,
                        context={"source": "hub"},
                        data=None,
                    ),
                    ".async_init(hub)",
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

            profile.async_create_task(
                self.async_try_discovery(uuid),
                f".async_try_discovery({uuid})",
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

    async def entry_update_listener(self, profile: "MQTTProfile"):
        """Called by the ApiProfile to propagate config changes"""
        self.configure_logger()
        if self.sensor_connection:
            self.sensor_connection.configure_logger()

    def attach(self, device: "Device", /):
        assert device.id not in self.mqttdevices, (
            "unexpected MQTTConnection.attach",
            device.id,
        )
        device.mqtt_attached(self)
        self.mqttdevices[device.id] = device
        if self.sensor_connection:
            self.sensor_connection.update_devices()

    def detach(self, device: "Device", /):
        device_id = device.id
        assert device_id in self.mqttdevices, (
            "unexpected MQTTConnection.detach",
            device_id,
        )

        for mqtt_transaction in [
            _t for _t in self._transactions.values() if _t.uuid == device_id
        ]:
            mqtt_transaction.cancel(True)
        device.mqtt_detached()
        self.mqttdevices.pop(device_id)
        if self.sensor_connection:
            self.sensor_connection.update_devices()

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
            return await self.parent.api.hass.config_entries.flow.async_init(
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
        handled in every MQTTConnection (cloud, local) so we process even messages
        originated from the device itself.
        Returns False when device is online and the message pipe should continue processing.
        """
        return (message.method != mc.METHOD_PUSH) or (
            message.payload[mc.KEY_ONLINE].get(mc.KEY_STATUS) != mc.STATUS_ONLINE
        )


MQTTConnection.SESSION_HANDLERS = {
    mn.Appliance_System_Online: MQTTConnection._handle_Appliance_System_Online,
}


class MQTTProfile(mlm.ConfigEntryManager):
    """
    Base class for both MerossProfile and ComponentApi allowing lightweight
    sharing of globals and defining some common interfaces.
    """

    if TYPE_CHECKING:
        is_cloud_profile: bool
        linkeddevices: Final[dict[str, Device]]
        mqttconnections: Final[dict[str, MQTTConnection]]

    DEFAULT_PLATFORMS = mlm.ConfigEntryManager.DEFAULT_PLATFORMS | {
        SENSOR_DOMAIN: None,
    }

    __slots__ = (
        "is_cloud_profile",
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
        super().__init__(id, api, config_entry, **kwargs)
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
        allow_mqtt_publish = config.get(mlc.CONF_ALLOW_MQTT_PUBLISH) or (
            self is self.api
        )
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
        return self.config.get(mlc.CONF_ALLOW_MQTT_PUBLISH)

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
        device_id = device.id
        assert device_id not in self.linkeddevices
        device.profile_linked(self)
        self.linkeddevices[device_id] = device

    def unlink(self, device: "Device"):
        device_id = device.id
        assert device_id in self.linkeddevices
        device.profile_unlinked()
        self.linkeddevices.pop(device_id)

    @abstractmethod
    def attach_mqtt(self, device: "Device"):
        pass
