import asyncio
from bisect import bisect_right
from json import JSONDecodeError
from typing import TYPE_CHECKING, override

from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.util import dt as dt_util

from .. import const as mlc
from ..button import PersistentButton
from ..merossclient import (
    DeviceDescriptor,
    async_import_module,
    datetime_from_epoch,
    device,
)
from ..merossclient.client import AbstractClient, Direction, Transport
from ..merossclient.client.http import HttpClient
from ..merossclient.device import handler
from ..merossclient.exceptions import MerossError
from ..merossclient.obfuscate import OBFUSCATE_DICT
from ..merossclient.protocol import const as mc, namespaces as mn
from ..merossclient.protocol.message import MerossMessage, MerossResponse
from ..sensor import ProtocolSensor
from ..update import UpdateEntity

# import core modules instead of symbols to ease patching in a single place
from .manager import ConfigEntryManager

if TYPE_CHECKING:
    from asyncio import Task
    from typing import (
        Any,
        Callable,
        ClassVar,
        Final,
        Iterable,
        Iterator,
        Mapping,
        Unpack,
    )

    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

    from ..merossclient.protocol import types as mt
    from ..merossclient.protocol.types import (
        MerossPayloadType,
        MerossRequestType,
    )
    from .component_api import ComponentApi
    from .entity import Entity, ParserEntity
    from .meross_profile import DeviceInfoType, LatestVersionType
    from .mqtt_profile import MQTTConnection, MQTTProfile


class DiagnosticHandler(handler.NamespaceHandler):

    if TYPE_CHECKING:
        """Configuration to be used for unknown/unmanaged namespaces."""
        parent: Final[Device]  # type: ignore[override]
        parser_class: type[ParserEntity] | None  # type: ignore[override]

    def __init_subclass__(cls):
        super().__init_subclass__()
        # Since NamespaceHandler cannot be slotted itself because of mixin-ing with ParserEntity
        # in EntityNamespaceMixin we try this trick to provide automatic slotting for all the subclasses
        # which are not mixed with parsers and which don't define their own __slots__.
        if not issubclass(cls, handler.NamespaceParser):
            cls.__slots__ = cls._calc_slots()

    @override
    def _handle(self, message: MerossMessage, /):
        device = self.parent
        if device.create_diagnostic_entities:
            # since we're parsing an unknown namespace, our euristic about
            # the key_namespace might be wrong so we use another euristic
            ns = self.id
            if not self.polling_strategy:
                self.polling_strategy = DiagnosticHandler.async_poll_diagnostic
            for _key, _payload in message.payload.items():
                # since the ns_key might be often the same across different namespaces
                # we add the last split of the namespace to the extracted payload key
                if type(_payload) is dict:
                    device.parse_undefined_dict(
                        f"{ns.slug_end}_{_key}",
                        _payload,
                        self.index_type.index(_payload),
                    )
                elif type(_payload) is list:
                    _key = f"{ns.slug_end}_{_key}"
                    for __payload in _payload:
                        device.parse_undefined_dict(
                            _key, __payload, self.index_type.index(__payload)
                        )
                else:
                    # should we diagnostic scalar values in root payload ?
                    pass

        else:
            super()._handle(message)

    @override
    def _parse(self, payload, /):
        """Default ParserFunc automatically installed when parsing a message for which no indexed parser is registered.
        The payload is typically an 'indexed' item payload scanned by handlers like _handle_channel_list or _handle_subid.
        This is a fallback for unexpected channels/subdevices and is useful for logging purposes.
        """
        if self.parent.create_diagnostic_entities:
            # since we're parsing an unknown namespace, our euristic about
            # the key_namespace might be wrong so we use another euristic
            if not self.polling_strategy:
                self.polling_strategy = DiagnosticHandler.async_poll_diagnostic
            self.parent.parse_undefined_dict(
                f"{self.id.slug_end}_{self.id.key}",
                payload,
                self.index_type.index(payload),
            )
        else:
            super()._parse(payload)


class BaseDevice(device.PhysicalDevice):
    """
    Abstract base class for Device and SubDevice (from hub)
    giving common behaviors like device_registry interface.
    # TODO: this is a slight overhead in mros and we should try
    # remove the need for this tryiong to leverage the PhysicalDevice as much as possible,
    # but for now it gives us a clean way to share the device registry management
    """

    if TYPE_CHECKING:

        # to be implemented in derived classes
        device_entry: dr.DeviceEntry
        device_info: Entity.DeviceInfo
        entities_iterable: Iterable[Entity]

        update_firmware: UpdateEntity | None

        class Args(device.PhysicalDevice.Args):
            pass

        def __init__(
            self, id, parent: ConfigEntryManager, /, **kwargs: Unpack[Args]
        ): ...

    __SLOTS__ = (
        "device_entry",
        "device_info",
    )

    SLOTS_AUTO_INIT = ("update_firmware",)

    @override
    def on_connect(self):
        super().on_connect()
        for entity in (
            _entity for _entity in self.entities_iterable if not _entity.available
        ):
            entity.set_available()

    @override
    def on_disconnect(self):
        super().on_disconnect()
        for entity in (
            _entity for _entity in self.entities_iterable if _entity.available
        ):
            entity.set_unavailable()

    # interface: self
    device_entry = NotImplemented
    device_info = NotImplemented
    entities_iterable = NotImplemented


class Device(ConfigEntryManager, device.Device, BaseDevice):
    """
    Generic protocol handler class managing the physical device stack/state
    """

    class Http(HttpClient):

        if TYPE_CHECKING:
            device: Final["Device"]  # type: ignore[override]

        @override
        def on_rx_raw(self, raw: bytes | bytearray, /) -> MerossMessage:
            try:
                response = HttpClient.on_rx_raw(self, raw)
                # add a sanity check here since we have some issues (#341)
                # that might be related to misconfigured devices where the
                # host address points to a different device than configured.
                # Our current device.id in fact points (or should) to the uuid discovered
                # in configuration but if by chance the device changes ip and we miss
                # the dynamic change (either dhcp not working or HA down while dhcp updating)
                # we might end up with our configured host pointing to a different device
                # and this might (likely) be another Meross with the same key
                # so it could rightly respond here. This shouldnt happen over MQTT
                # since the device.id is being taken care of by the routing mechanism
                if self.device.id != response.uuid:
                    self.device._process_uuid_mismatch(response.uuid, None)
                    raise MerossError(
                        f"Device UUID mismatch over HTTP (expected:{self.device.id} got:{response.uuid})"
                    )
                return response
            except JSONDecodeError as jsonerror:
                # this could happen when the response carries a truncated payload
                # and might be due to an 'hard' limit in the capacity of the
                # device http output buffer (when the response is too long)
                self.log_exception(
                    self.WARNING,
                    jsonerror,
                    "response (message might be truncated)",
                )
                response_text = jsonerror.doc
                response_text_len_safe = int(len(response_text) * 0.9)
                if jsonerror.pos < response_text_len_safe:
                    # if the error is too early in the payload...
                    raise
                # the error happened because of truncated json payload
                device = self.device
                device.device_response_size_max = response_text_len_safe
                if device.device_response_size_min > response_text_len_safe:
                    device.device_response_size_min = response_text_len_safe
                self.log(
                    self.DEBUG,
                    "Updating device_response_size_min:%d device_response_size_max:%d",
                    device.device_response_size_min,
                    device.device_response_size_max,
                )
                # try to recover the message by truncating the payload
                # with various heuristics
                namespace = self.last_tx_message.namespace  # type: ignore
                if not type(namespace) is mn.Namespace:
                    namespace = mn.NAMESPACES[namespace]
                match namespace:
                    case mn.Appliance_Control_Multiple:
                        list_break_matcher = '},{"header":'
                    case _:
                        if not namespace.index_type:
                            raise
                        list_break_matcher = f'}},{{"{namespace.index_type.key[0]}":'

                trunc_pos = response_text.rfind(list_break_matcher)
                if trunc_pos == -1:
                    raise
                response_text = response_text[0:trunc_pos] + "}]}}"
                return self.on_rx_raw(response_text.encode())

        # skip key-hacking in base HTTP client
        async_request = AbstractClient.async_request

    if TYPE_CHECKING:
        # Overrides
        config_entry: Final[ConfigEntry]  # type: ignore[override]
        config: mlc.DeviceConfigType

        descriptor: Final[DeviceDescriptor]  # type: ignore[override]
        bluetooth: Final[ComponentApi.BTClient | None]  # type: ignore[override]
        http: Final[Http | None]  # type: ignore[override]
        mqtt: Final[MQTTConnection.Client | None]  # type: ignore[override]

        NAMESPACE_IGNORE: ClassVar[tuple[str, ...]]
        """ This is a set of namespaces we don't use and for which we don't want to have any diagnostic
        entity/log as well. These might be published by devices and enter our MQTT async processing
        generating 'default' handlers which will in turn partecipate in logging/diagnostics.
        That's why we use a 'void' handler for these namespaces, which will just ignore the messages."""
        TRACE_ABILITY_EXCLUDE: ClassVar[tuple[str, ...]]
        """ When tracing we enumerate appliance abilities to get insights on payload structures
        this list will be excluded from enumeration since it's redundant/exposing sensitive info
        or simply crashes/hangs the device."""

        # these are set from ConfigEntry
        configured_transport: Transport
        host: str | None

        # Device inner timestamp handling for time-sensitive features
        # These are mostly needed to ensure reliable working for metering plugs and
        # scheduled device features.
        device_timestamp: int
        """Device actual estimated timestamp as extracted from last rx message."""
        device_timedelta: float
        """Device timestamp delta against local time (device_timestamp - local_time)."""
        _check_device_time_enabled: bool
        """Scheduled 'on-demand' device time check. This is only created when enable_device_time_check is called."""

        device_entries_info: dict[Any, Entity.DeviceInfo]
        profile: Final[MQTTProfile | None]

        _async_create_diagnostic_entities_task: Task  # dynamic

        # entities
        sensor_protocol: ProtocolSensor

    PARAM_DEVICE_TIMESTAMP_TOLERANCE = 5
    """Max device timestamp diff against HA to trigger warning and (eventually) fix it."""
    PARAM_CHECK_DEVICE_TIME_START_DELAY = 60
    """Delay after which the device time check procedure starts since device connection.
    This is needed to allow the internal device timestamp estimations to stabilize."""
    PARAM_CHECK_DEVICE_TIME_REPEAT_DELAY = 86400
    """Delay between consecutive device time checks (after initial 'cold' check)."""
    PARAM_CHECK_DEVICE_TIMEZONE_FUTURE_DELTA = 86400
    PARAM_TRACING_ABILITY_POLL_TIMEOUT = 2
    """Used to delay the iteration of abilities scan while tracing."""

    NAMESPACE_INIT_PACKAGE = "custom_components.meross_lan"
    # Order of initialization matters since some features might be conditionally
    # configured here and there depending on ns combinations.
    # For example ms600/ms130 need to post-configure Sensor.LatestX handler
    # that need to be in place in order to be correctly configured.
    NAMESPACE_INIT = {
        mn.Appliance_Control_Sensor_Latest: (".devices.misc", "SensorLatestParser"),
        mn.Appliance_Control_Sensor_LatestX: (".devices.misc", "SensorLatestXParser"),
        mn.Appliance_Control_Toggle: (".switch", "Toggle"),
        # ToggleX need to be created before any other possible 'conflicting' ns
        # like .Light or .Fan
        mn.Appliance_Control_ToggleX: (".switch", "ToggleX"),
        mn.Appliance_Config_DeviceCfg: (".devices.misc", "DeviceCfgParser"),
        mn.Appliance_Config_OverTemp: (".devices.mss", "OverTempEnableSwitch"),
        mn.Appliance_Config_Alarm: (".siren", "ConfigAlarm"),
        mn.Appliance_Control_Alarm: (".siren", "ControlAlarm"),
        mn.Appliance_Control_Electricity: (".devices.mss", "ElectricitySensor"),
        mn.Appliance_Control_ElectricityX: (".devices.mss", "ElectricityXSensor"),
        mn.Appliance_Control_ConsumptionH: (
            ".devices.mss",
            "ConsumptionHNamespaceHandler",
        ),
        mn.Appliance_Control_ConsumptionX: (".devices.mss", "ConsumptionXSensor"),
        mn.Appliance_Control_Diffuser_Light: (".devices.diffuser", "DiffuserLight"),
        mn.Appliance_Control_Diffuser_Sensor: (".devices.diffuser", "DiffuserSensor"),
        mn.Appliance_Control_Diffuser_Spray: (".devices.diffuser", "DiffuserSpray"),
        mn.Appliance_Control_Fan: (".fan", "Fan"),
        mn.Appliance_Control_FilterMaintenance: (
            ".sensor",
            "FilterMaintenanceSensor",
        ),
        mn.Appliance_Control_Light: (".light", "Light"),
        mn.Appliance_Control_Mp3: (".media_player", "Mp3Player"),
        mn.Appliance_Control_PhysicalLock: (".switch", "PhysicalLockSwitch"),
        mn.Appliance_Control_Presence_Config: (
            ".devices.ms600",
            "PresenceConfigParser",
        ),
        mn.Appliance_Control_Screen_Brightness: (
            ".devices.thermostat",
            "ScreenBrightnessNamespaceHandler",
        ),
        mn.Appliance_Control_Spray: (".devices.spray", "Spray"),
        mn.Appliance_Control_TempUnit: (".devices.thermostat", "MtsTempUnit"),
        mn.thermostat.Appliance_Control_Thermostat_Mode: (
            ".devices.thermostat.mts200",
            "Mts200Climate",
        ),
        mn.thermostat.Appliance_Control_Thermostat_ModeB: (
            ".devices.thermostat.mts960",
            "Mts960Climate",
        ),
        mn.thermostat.Appliance_Control_Thermostat_ModeC: (
            ".devices.thermostat.mts300",
            "Mts300Climate",
        ),
        mn.thermostat.Appliance_Control_Thermostat_DeadZone: (
            ".devices.thermostat",
            "MtsDeadZoneNumber",
        ),
        mn.thermostat.Appliance_Control_Thermostat_Frost: (
            ".devices.thermostat",
            "MtsFrostNumber",
        ),
        mn.thermostat.Appliance_Control_Thermostat_HoldAction: (
            ".devices.thermostat",
            "MtsHoldAction",
        ),
        mn.thermostat.Appliance_Control_Thermostat_Overheat: (
            ".devices.thermostat",
            "MtsOverheatNumber",
        ),
        mn.thermostat.Appliance_Control_Thermostat_Sensor: (
            ".devices.thermostat",
            "MtsExternalSensorSwitch",
        ),
        mn.thermostat.Appliance_Control_Thermostat_SummerMode: (
            ".devices.thermostat",
            "MtsSummerMode",
        ),
        mn.thermostat.Appliance_Control_Thermostat_WindowOpened: (
            ".devices.thermostat",
            "MtsWindowOpened",
        ),
        mn.Appliance_GarageDoor_MultipleConfig: (
            ".devices.garagedoor",
            "GarageDoorMultipleConfig",
        ),
        mn.Appliance_GarageDoor_Config: (
            ".devices.garagedoor",
            "GarageDoorConfig",
        ),  # install this after MultipleConfig since it could apply some fallbacks for missing MultipleConfig entities
        mn.Appliance_GarageDoor_State: (".devices.garagedoor", "GarageDoor"),
        mn.Appliance_Mcu_Firmware: (
            ".merossclient.device.handler",
            "NamespaceHandler",  # handler in Device._handle_XXX
        ),
        mn.Appliance_Mcu_Hp110_Firmware: (
            ".merossclient.device.handler",
            "NamespaceHandler",  # handler in Device._handle_XXX
        ),
        mn.Appliance_RollerShutter_Position: (
            ".devices.rollershutter",
            "RollerShutter",
        ),
        mn.Appliance_RollerShutter_Adjust: (
            ".devices.rollershutter",
            "RollerShutterAdjustSwitch",
        ),
        mn.Appliance_System_DNDMode: (".light", "DNDLight"),
        mn.Appliance_System_Runtime: (".sensor", "SignalStrengthSensor"),
    }
    NAMESPACE_IGNORE = (
        mn.Appliance_Config_Info,
        mn.Appliance_Control_Bind,
        mn.Appliance_Control_ConsumptionConfig,
        mn.Appliance_Control_Timer,
        mn.Appliance_Control_TimerX,
        mn.Appliance_Control_Trigger,
        mn.Appliance_Control_TriggerX,
        mn.Appliance_System_Clock,
        mn.Appliance_System_Online,
        mn.Appliance_System_Report,
    )
    TRACE_ABILITY_EXCLUDE = (
        mn.Appliance_System_Ability,
        mn.Appliance_System_All,
        mn.Appliance_System_Clock,
        mn.Appliance_System_DNDMode,
        mn.Appliance_System_Firmware,
        mn.Appliance_System_Hardware,
        mn.Appliance_System_Online,
        mn.Appliance_System_Position,
        mn.Appliance_System_Time,
        mn.Appliance_Config_Wifi,
        mn.Appliance_Config_WifiList,
        mn.Appliance_Config_WifiX,
        mn.Appliance_Control_Bind,
        mn.Appliance_Control_Unbind,
    )

    init_is_connected = False

    __slots__ = device.Device._calc_slots(
        "conf_transport",
        "host",
        "device_entries_info",
        "device_timestamp",
        "device_timedelta",
        "_check_device_time_enabled",
        "device_timedelta_log_epoch",
        "device_timedelta_config_epoch",
        "_profile",
        "_async_create_diagnostic_entities_task",
        "sensor_protocol",
    )

    def __init__(
        self,
        device_id: str,
        api: "ComponentApi",
        config_entry: "ConfigEntry",
    ):
        if device_id != config_entry.data[mlc.CONF_DEVICE_ID]:
            # shouldnt really happen: it means we have a 'critical' bug in our config entry/flow management
            # or that the config_entry was tampered
            raise Exception(
                "Unrecoverable device id mismatch. 'ConfigEntry.unique_id' "
                "does not match the configured 'device_id'. "
                "Please delete the entry and reconfigure it"
            )
        descriptor = DeviceDescriptor(config_entry.data[mlc.CONF_PAYLOAD])
        if device_id != descriptor.uuid:
            # this could happen (#341 raised the suspect) if a working device
            # 'suddenly' starts talking with another one and doesn't recognize
            # the mismatch (the issue appears as the device usually keeps updating
            # the config_entry data from live communication). This behavior is being
            # fixed in 4.5.0 so that devices don't update wrong configurations 'in the wild'
            raise Exception(
                "Configuration data mismatch. Please refresh "
                "the configuration by hitting 'Configure' "
                "in the integration configuration page"
            )
        ConfigEntryManager.__init__(
            self,
            device_id,
            api,
            config_entry,
            # configure AbstractClient
            key=config_entry.data.get(mlc.CONF_KEY) or "",  # type: ignore[argument]
            descriptor=descriptor,  # type: ignore[argument],
        )
        self.device_info = {"identifiers": {(mlc.DOMAIN, device_id)}}
        self.device_entry = api.device_registry.async_get_or_create(
            config_entry_id=config_entry.entry_id,
            connections={(dr.CONNECTION_NETWORK_MAC, descriptor.macAddress)},
            manufacturer=mc.MANUFACTURER,
            name=descriptor.productname,
            model=descriptor.productmodel,
            hw_version=descriptor.hardwareVersion,
            sw_version=descriptor.firmwareVersion,
            **self.device_info,  # type: ignore
        )
        if descriptor.type.startswith(mc.TYPE_MFC100):
            # This device presents various features on different channels
            # but we prefer to show them as a single device and
            # counter our general logic where each channel is a 'logical' device
            self.device_entries_info = {
                channel: self.device_info for channel in range(3)
            }
        self.device_timestamp = 0
        self.device_timedelta = 0
        self._check_device_time_enabled = False
        self.device_timedelta_log_epoch = 0
        self.device_timedelta_config_epoch = 0
        self.profile = None
        self.sensor_protocol = ProtocolSensor(self)
        PersistentButton(
            None,
            self,
            async_press=self.async_poll_full,
            name="Refresh",
            device_class=PersistentButton.DeviceClass.RESTART,
            entity_category=PersistentButton.EntityCategory.DIAGNOSTIC,
        )
        PersistentButton(
            None,
            self,
            press=self.schedule_reload,
            name="Reload",
            device_class=PersistentButton.DeviceClass.RESTART,
            entity_category=PersistentButton.EntityCategory.DIAGNOSTIC,
        )
        self._update_config()

    def start(self):
        # Called by async_setup_entry after the entities have been registered
        # or after a config change. Here we'll register mqtt bindings,
        # fix transports according to the current configuration and profile
        # linking, and finally (re)start polling.
        try:
            profile = self.parent.profiles[self.descriptor.userId]
            if profile and (profile.key != self.key):
                profile = self.parent
        except KeyError:
            profile = self.parent
        if self.profile == profile:
            self._check_protocol()
        else:
            if self.profile:
                self.profile.unlink(self)
            if profile:
                profile.link(self)
                # _check_protocol already called
            else:
                self._check_protocol()
        self.polling_start()

    async def async_shutdown(self):
        self.remove_issue(mlc.ISSUE_DEVICE_TIMEZONE)
        self.cancel_callback(self._async_entry_update)
        if self.bluetooth:
            # bluetooth client is managed by ComponentApi so we dont shutdown it
            # (super().async_shutdown will also shutdown clients) but just unlink it from the device
            self.remove_client(self.bluetooth)
        await super().async_shutdown()
        if self.profile:
            self.profile.unlink(self)
        del self.sensor_protocol
        self.parent.devices[self.id] = None

    # miscellaneous internals to prepare/refresh internal config
    def _update_config(self):
        """
        common properties caches, read from ConfigEntry on __init__ or when a configentry updates
        """
        config = self.config
        # map CONF_PROTOCOL value to a const symbol in order to use 'is' in Device code checks
        try:
            conf_transport = Transport.from_str(config[mlc.CONF_PROTOCOL])  # type: ignore
        except KeyError:
            conf_transport = Transport.AUTO
        self.configured_transport = conf_transport
        self.polling_period = (
            config.get(mlc.CONF_POLLING_PERIOD) or mlc.CONF_POLLING_PERIOD_DEFAULT
        )
        if self.polling_period < mlc.CONF_POLLING_PERIOD_MIN:
            self.polling_period = mlc.CONF_POLLING_PERIOD_MIN
        self._polling_delay = self.polling_period

        self.enable_multiple(not config.get(mlc.CONF_DISABLE_MULTIPLE))
        self._update_host(config.get(mlc.CONF_HOST) or self.descriptor.innerIp)
        if self.mqtt:  # just to be sure key is sync'd
            self.mqtt.key = self.key

        if conf_transport is Transport.BLUETOOTH:
            if not self.bluetooth:
                if _bluetooth := self.parent.get_bt_client(self.id):
                    self.add_client(_bluetooth)
                    self.parent.device_registry.async_update_device(
                        self.device_entry.id,
                        new_connections={
                            (dr.CONNECTION_NETWORK_MAC, self.descriptor.macAddress),
                            (dr.CONNECTION_BLUETOOTH, _bluetooth.address),
                        },
                    )

        elif self.bluetooth:
            self.remove_client(self.bluetooth)
            self.parent.device_registry.async_update_device(
                self.device_entry.id,
                new_connections={
                    (dr.CONNECTION_NETWORK_MAC, self.descriptor.macAddress)
                },
            )

    def _update_host(self, host: str | None):
        # host could be either from config or from descriptor as a fallback
        if host == "0.0.0.0":  # unbinded device reports this in descriptor
            host = None

        self.host = host
        http = self.http
        if host and (self.configured_transport in (Transport.AUTO, Transport.HTTP)):
            if http:
                http.host = host
                http.key = self.key
            else:
                http = Device.Http(
                    host,
                    self,
                    key=self.key,
                    from_=mlc.DOMAIN,
                    trigger_src=self.__class__.__name__,
                    loop=self.loop,
                )
                self.add_client(http)
            if mn.Appliance_Encrypt_ECDHE in self.descriptor.ability:
                http.enable_encryption(self.id, self.key, self.descriptor.macAddress)
            else:
                http.disable_encryption()
        elif http:
            self.remove_client(http)

    def _check_protocol(self):
        """called whenever the configuration or the profile linking changes to fix transports."""
        conf_transport = self.configured_transport
        if conf_transport in (Transport.BLUETOOTH, Transport.HTTP):
            self.preferred_transport = conf_transport
            if self.mqtt:
                self.remove_client(self.mqtt)
        else:
            _profile = self.profile
            # assert profile ?
            if self.mqtt:
                if self.mqtt.connection.parent != _profile:
                    self.remove_client(self.mqtt)
                    if _profile:
                        _profile.get_connection(self).attach(self)
            else:
                if _profile:
                    _profile.get_connection(self).attach(self)

            if conf_transport is Transport.AUTO:
                # When using Transport.AUTO we try to use our 'preferred' transport.
                # When binded to a cloud_profile always prefer http since it will avoid excessive
                # MQTT traffic and related cloud 'issues' like rate-limiting
                if self.config.get(mlc.CONF_HOST) or (
                    self.mqtt and self.mqtt.connection.is_cloud
                ):
                    self.preferred_transport = Transport.HTTP
                else:
                    self.preferred_transport = Transport.MQTT
            else:
                self.preferred_transport = conf_transport

        if self.transport is not self.preferred_transport:
            if self.is_connected:
                try:
                    self._switch_client(
                        self._clients_connected[self.preferred_transport]
                    )
                except KeyError:
                    # preferred transport not connected: leave current transport whatever
                    pass
            else:
                try:
                    self._switch_client(self._clients[self.preferred_transport])
                except KeyError:
                    self.log(
                        self.WARNING,
                        "Preferred transport {%s} not available, current transport is {%s}",
                        self.preferred_transport,
                        self.transport,
                    )

    # interface: ConfigEntryManager
    @override
    async def async_setup_entry(
        self, hass: "HomeAssistant", config_entry: "ConfigEntry"
    ):
        if self.descriptor.is_hub:
            # dynamic subclassing of Device instance
            if TYPE_CHECKING:
                from ..devices import hub
            hub = await async_import_module(".devices.hub", self.NAMESPACE_INIT_PACKAGE)
            self.__class__ = hub.Hub

        await self.async_init()
        await ConfigEntryManager.async_setup_entry(self, hass, config_entry)

    @override
    def get_device_entry_info(self, index_value, /) -> "Entity.DeviceInfo":
        if not index_value:
            # Either a non parser entity or a parser entity with no indexing (i.e. unique for the device)
            # or an entity for channel == 0
            return self.device_info
        try:
            return self.device_entries_info[index_value]
        except AttributeError:
            # device_entries_info only built if needed
            self.device_entries_info = {}
        except KeyError:
            pass
        # We expect index_value to be a channel number...
        assert (
            type(index_value) is int
        ), "index_value is expected to be an int representing the channel number (got {})".format(
            type(index_value)
        )
        device_info: "Entity.DeviceInfo" = {
            "identifiers": {(mlc.DOMAIN, f"{self.id}_{index_value}")}
        }
        self.parent.device_registry.async_get_or_create(
            config_entry_id=self.config_entry.entry_id,
            manufacturer=mc.MANUFACTURER,
            name=f"{self.device_entry.name} Channel {index_value}",
            model=self.device_entry.model,
            via_device=next(iter(self.device_entry.identifiers)),
            **device_info,
        )
        self.device_entries_info[index_value] = device_info
        return device_info

    @property
    @override
    def display_name(self) -> str:
        return (
            self.device_entry.name_by_user
            or self.device_entry.name
            or self.descriptor.productname
        )

    # interface: ConfigEntryManager
    async def entry_update_listener(
        self, hass: "HomeAssistant", config_entry: "ConfigEntry"
    ):
        # TODO: remove hot-stage updating and always fully reload
        # removing this feature overall will greatly simplify the whole code base
        # and avoid possible re-entrance issues
        ability_old = self.descriptor.ability
        ability_new = config_entry.data[mc.KEY_PAYLOAD][mc.KEY_ABILITY]
        if ability_old != ability_new:
            # too hard to keep-up..reinit the device
            ability_old = ability_old.keys()
            ability_new = ability_new.keys()
            self.log(
                self.WARNING,
                "Scheduled device configuration reload since the abilities changed (added:%s - removed:%s)",
                str(ability_new - ability_old),
                str(ability_old - ability_new),
            )
            self.schedule_reload()
            return

        await ConfigEntryManager.entry_update_listener(self, hass, config_entry)
        self._update_config()
        self.start()

    async def async_destroy_diagnostic_entities(self, /):
        try:
            if self._async_create_diagnostic_entities_task.cancel():
                try:
                    await self._async_create_diagnostic_entities_task
                except (asyncio.CancelledError, Exception):
                    pass
            del self._async_create_diagnostic_entities_task
        except AttributeError:
            pass

        for namespace_handler in self.ns_handlers.values():
            if (
                namespace_handler.polling_strategy
                is DiagnosticHandler.async_poll_diagnostic
            ):
                namespace_handler.polling_strategy = None
        await ConfigEntryManager.async_destroy_diagnostic_entities(self)

    async def _async_create_diagnostic_entities(self):
        # when create_diagnostic_entities is True, we'll schedule this task
        # that will try at its best to stay alive and finish the abilities scan
        # pausing now and then when offline and/or to not interleave with polling.
        self.log(self.DEBUG, "Diagnostic entities scan begin")
        try:
            abilities = iter(self.descriptor.ability)
            while True:
                if (
                    ns_handler := self._trace_ability_next(abilities)
                ) and not ns_handler.polling_strategy:
                    async with self.polling_lock:
                        if not self.is_connected:
                            raise Exception("Device disconnected")
                        await ns_handler.async_get_safe()
        except asyncio.CancelledError:
            self.log(self.DEBUG, "Diagnostic entities scan cancelled")
            raise
        except StopIteration:
            self.log(self.DEBUG, "Diagnostic entities scan end")
        except Exception as e:
            self.log_exception(self.WARNING, e, "diagnostic entities scan")
            raise

    @override
    def get_logger_name(self) -> str:
        return f"{self.descriptor.type}_{self.loggable_device_id(self.id)}"

    @override
    def _trace_opened(self, epoch: float):
        self.schedule_async_callback(
            self.PARAM_TRACING_ABILITY_POLL_TIMEOUT,
            self._async_trace_ability,
            iter(self.descriptor.ability),
        )

    def trace_close(
        self, exception: Exception | None = None, error_context: str | None = None
    ):
        self.cancel_callback(self._async_trace_ability)
        ConfigEntryManager.trace_close(self, exception, error_context)

    def _trace_ability_next(self, abilities: "Iterator[str]", /):
        ability = next(abilities)
        if ability in self.TRACE_ABILITY_EXCLUDE:
            return None
        ns = mn.NAMESPACES.get(ability)
        if not ns:  # unknown namespace..setup generic handler
            return self.get_handler_by_name(ability)
        if ns.can_query:
            return self.get_handler(ns)
        return None

    async def _async_trace_ability(self, abilities: "Iterator[str]"):
        try:
            # avoid interleave tracing ability with polling loop
            # also, since we could trigger this at early stages
            # in device init, this check will prevent iterating
            # at least until the device fully initialize through
            # self.start()
            if self.is_connected:
                while not (ns_handler := self._trace_ability_next(abilities)):
                    continue
                async with self.polling_lock:
                    self.log(self.DEBUG, "Tracing %s ability", ns_handler.id)
                    await ns_handler.async_trace(self.async_request)
        except StopIteration:
            self.log(self.DEBUG, "Tracing abilities end")
            return
        except asyncio.CancelledError as e:
            self.log_exception(self.DEBUG, e, "_async_trace_ability")
            raise
        except Exception as e:
            self.log_exception(self.WARNING, e, "_async_trace_ability")

        if not self.is_tracing:
            return

        if self.mqtt and (self.client is self.mqtt):
            timeout = (
                self.PARAM_TRACING_ABILITY_POLL_TIMEOUT
                + self.mqtt.connection.get_rl_safe_delay(self.id)
            )
        else:
            timeout = self.PARAM_TRACING_ABILITY_POLL_TIMEOUT
        self.schedule_async_callback(
            timeout,
            self._async_trace_ability,
            abilities,
        )

    async def _async_get_diagnostics_trace(self) -> list[list]:
        """
        invoked by the diagnostics callback:
        here we set the device to start tracing the classical way (in file)
        but we also fill in a dict which will set back as the result of the
        Future we're returning to diagnostics.
        """
        if self._trace_future:
            # avoid re-entry..keep going the running trace
            return await self._trace_future
        if self.is_tracing:
            self.trace_close()

        if (http := self.http) and http.is_connected:
            # shortcut with fast HTTP querying
            self._trace_data = trace_data = [mlc.CONF_TRACE_COLUMNS]
            await self.async_poll_full()
            try:
                abilities = iter(self.descriptor.ability)
                async with self.polling_lock:
                    while http.is_connected and self.is_tracing:
                        if ns_handler := self._trace_ability_next(abilities):
                            await ns_handler.async_trace(http.async_request)
                self._trace_data = None
                return trace_data  # might be truncated because offlining or async shutting trace
            except StopIteration:
                self._trace_data = None
                return trace_data
            except Exception as exception:
                self.log_exception(self.DEBUG, exception, "async_get_diagnostics_trace")
                # in case of error we're going to try the legacy approach

        # reset and restart with a debug tracing to build the diagnostics
        self._trace_data = [mlc.CONF_TRACE_COLUMNS]
        self._trace_future = future = self.loop.create_future()
        await self.async_trace_open()
        return await future

    @override
    def loggable_diagnostic_state(self):
        """Return a 'loggable' version of the entry state (for diagnostic/logging purposes)"""
        profile = self.profile
        if profile:
            device_info = profile.get_device_info(self.id)
            latest_version = profile.get_latest_version(*self.descriptor.type_subtype)
            latest_versions = profile.get_latest_versions()
        else:
            device_info = None
            latest_version = None
            latest_versions = None
        if not latest_version:
            for _profile in self.parent.active_profiles():
                if _profile is profile:
                    continue
                if latest_version := _profile.get_latest_version(
                    *self.descriptor.type_subtype
                ):
                    break
        return {
            "class": type(self).__name__,
            "configured_transport": self.configured_transport,
            "preferred_transport": self.preferred_transport,
            "transport": self.transport,
            "polling_period": self.polling_period,
            "device_response_size_min": self.device_response_size_min,
            "device_response_size_max": self.device_response_size_max,
            "BLUETOOTH": {
                "bluetooth": bool(self.bluetooth),
                "bluetooth_active": self.bluetooth and self.bluetooth.is_connected,
            },
            "HTTP": {
                "http": bool(self.http),
                "http_active": self.http and self.http.is_connected,
            },
            "MQTT": {
                "cloud_profile": profile and profile.is_cloud_profile,
                "mqtt_connection": bool(self.mqtt),
                "mqtt_connected": self.mqtt and self.mqtt.connection.is_connected,
                "mqtt_publish": self.mqtt and self.mqtt.connection.can_publish,
                "mqtt_active": self.mqtt_active,
            },
            "namespace_handlers": {
                ns_handler.id: {
                    "last_rx_epoch": ns_handler.last_rx_epoch,
                    "last_poll_epoch": ns_handler.last_poll_epoch,
                    "next_poll_epoch": ns_handler.next_poll_epoch,
                    "polling_strategy": (
                        ns_handler.polling_strategy.__name__
                        if ns_handler.polling_strategy
                        else None
                    ),
                    "lastpush": (
                        OBFUSCATE_DICT(ns_handler.last_rx_push)
                        if (ns_handler.last_rx_push and self.obfuscate)
                        else ns_handler.last_rx_push
                    ),
                    "digest": ns_handler.digest,
                }
                for ns_handler in self.ns_handlers.values()
            },
            "device_info": (
                OBFUSCATE_DICT(device_info)
                if self.obfuscate and device_info
                else device_info
            ),
            "latest_version": latest_version,
            "latest_versions": latest_versions,
        }

    async def async_get_diagnostics(self):
        if self.is_connected:
            data = await ConfigEntryManager.async_get_diagnostics(self)
            data["trace"] = await self._async_get_diagnostics_trace()
            return data
        else:
            return await ConfigEntryManager.async_get_diagnostics(self)

    # interface: AbstractClient
    @override
    def on_connect(self, /):
        super().on_connect()
        self.sensor_protocol.set_available()
        if self._check_device_time_enabled:
            self.schedule_callback(
                self.PARAM_CHECK_DEVICE_TIME_START_DELAY, self._check_device_time
            )
        if self.config.get(mlc.CONF_CREATE_DIAGNOSTIC_ENTITIES):
            try:
                self._async_create_diagnostic_entities_task.result()
                # no exception..it was correctly finished..nothing to do
            except (
                AttributeError,
                asyncio.CancelledError,
                Exception,
                asyncio.InvalidStateError,
            ) as e:
                if type(e) is asyncio.InvalidStateError:
                    self._async_create_diagnostic_entities_task.cancel()
                self._async_create_diagnostic_entities_task = self.create_task(
                    self._async_create_diagnostic_entities(),
                    "async_create_diagnostic_entities",
                )

    @override
    def on_disconnect(self, /):
        super().on_disconnect()
        self.cancel_callback(self._check_device_time)

    @override
    async def async_request(
        self,
        *args: "Unpack[MerossRequestType]",
        **kwargs: "Unpack[Device.RequestArgs]",
    ):
        """Wrapper for common final request method in order to catch MerossErrors and raise
        HomeAssistantError instead to avoid dumping full stack trace in logs and log a
        concise error message instead on selected exceptions."""
        try:
            return await device.Device.async_request(self, *args, **kwargs)
        except Exception as e:
            raise HomeAssistantError(str(e)) from e

    @override
    def on_tx(self, message: "MerossMessage", client: "AbstractClient", /):
        self.last_tx_message = message
        self.last_tx_epoch = client.last_tx_epoch
        self.log_message(message, Direction.TX, client.TRANSPORT, self.last_tx_epoch)

    @override
    def on_rx(self, message: "MerossMessage", client: "AbstractClient", /):
        self.last_rx_message = message
        self.last_rx_epoch = client.last_rx_epoch
        message_size = len(message.json)
        if message_size > self.device_response_size_min:
            self.device_response_size_min = message_size
            if message_size > self.device_response_size_max:
                self.device_response_size_max = message_size
        transport = client.TRANSPORT
        self.log_message(message, Direction.RX, transport, self.last_rx_epoch)
        message.check()
        if self.transport is not transport:
            if (self.preferred_transport is transport) or (
                len(self._clients_connected) == 1
            ):
                self._switch_client(client)

        # we'll use the device timestamp to 'align' our time to the device one
        # this is useful for metered plugs reporting timestamped energy consumption
        # and we want to 'translate' this timings in our (local) time.
        # We ignore delays below PARAM_TIMESTAMP_TOLERANCE since
        # we'll always be a bit late in processing
        self.device_timestamp = message.header[mc.KEY_TIMESTAMP]
        self.device_timedelta = (
            9 * self.device_timedelta + (self.last_rx_epoch - self.device_timestamp)
        ) / 10

        if self.isEnabledFor(self.DEBUG):
            # it appears sometimes the devices
            # send an incorrect signature hash
            # but at the moment this is unlikely to be critical
            sign = message.compute_signature(self.key)
            if sign != message.header[mc.KEY_SIGN]:
                self.log(
                    self.DEBUG,
                    "Received signature error: computed=%s, header=%s",
                    sign,
                    _header=message.header,
                )

    @override
    def log_message(
        self, msg: MerossMessage, dir: Direction, trans: Transport, epoch: float, /
    ):
        if self.is_tracing:
            self.trace_msg(epoch, msg, trans, dir)
        # here we avoid using self.log since it would
        # log to the trace file too but we've already 'traced' the
        # message if that's the case
        logger = self.logger
        if logger.isEnabledFor(self.VERBOSE):
            logger._log(
                self.VERBOSE,
                "%s: %s(%s) %s %s %s",
                (trans.upper(), dir, msg.messageid, msg.method, msg.namespace),
                _message=msg,
                obfuscate=self.obfuscate,
            )
        elif logger.isEnabledFor(self.DEBUG):
            logger._log(
                self.DEBUG,
                "%s: %s(%s) %s %s",
                (trans.upper(), dir, msg.messageid, msg.method, msg.namespace),
            )

    @override
    async def async_configure_timezone(self, tzname: str | None):
        await super().async_configure_timezone(tzname)
        self.schedule_entry_update(False)
        self.remove_issue(mlc.ISSUE_DEVICE_TIMEZONE)

    # interface: device.PhysicalDevice
    @override
    def _handle_missing_parser(
        self,
        nh: handler.NamespaceHandler,
        index: mn.IndexValue,
        payload: "mt.JsonMapping",
        /,
    ):
        # TODO: move back this method to NamespaceHandler by introducing the concept of
        # diagnostic_parser_class so that the handler itself knows how to manage this
        # without the indirection to the device.
        if self.create_diagnostic_entities:
            from ..sensor import DiagnosticParser

            parser = DiagnosticParser(
                index.value,
                self,
                entity_key=nh.id.key,
                index=index,
            )
            nh.register_parser(parser)
            return parser
        else:
            nh.parsers[index] = nh._parse
            nh.polling_request_add_index(index)
            return nh._parse

    # interface: device.Device
    @override
    def add_client(self, client: "AbstractClient", /):
        super().add_client(client)
        self.sensor_protocol.on_client_add(client)

    @override
    def remove_client(self, client: "AbstractClient", /):
        super().remove_client(client)
        self.sensor_protocol.on_client_remove(client)

    @override
    def _switch_client(self, client: "AbstractClient"):
        super()._switch_client(client)
        if self.is_connected:
            self.sensor_protocol.set_available()

    # interface: self
    def register_togglex_channel(self, entity: "ParserEntity", active: bool, /):
        """
        Checks if entity has an associated ToggleX behavior and eventually
        registers it
        """
        try:
            for togglex_digest in self.descriptor.digest[mc.KEY_TOGGLEX]:
                if togglex_digest[mc.KEY_CHANNEL] == entity.index.value:
                    if active:
                        # by design this should be an ToggleXParser
                        # but we have enough of _parse_togglex
                        assert hasattr(entity, "_parse_togglex")
                    else:
                        entity._parse_togglex = lambda payload: None
                    ns_handler = self.get_handler(mn.Appliance_Control_ToggleX)
                    ns_handler.register_parser(entity)
                    return ns_handler
        except KeyError:
            # no "togglex" in digest ?
            pass
        return None

    def schedule_entry_update(self, query_abilities: bool, /):
        """
        Schedule the ConfigEntry update due to self.descriptor changing.
        """
        self.schedule_async_callback(
            5,
            self._async_entry_update,
            query_abilities,
        )

    async def _async_entry_update(self, query_abilities: bool, /):
        """
        Called when we detect any meaningful change in the device descriptor
        that needs to be stored in configuration.
        We generally update self.descriptor.all whenever we process NS_ALL
        while abilities are never updated in descriptor this way.
        When we need to flush the updated NS_ALL we also try refresh the NS_ABILITY
        from the device so that the subsequent entry_update_listener has a chance
        to detect if those changed too and eventually reload the device entry.
        This is in order to detect 'abilities' changes even on the OptionFlow
        execution which independently queries the device itself.
        """
        with self.exception_warning("_async_entry_update"):
            data = dict(self.config_entry.data)
            data[mlc.CONF_TIMESTAMP] = self.time()  # force ConfigEntry update..
            data[mlc.CONF_PAYLOAD][mc.KEY_ALL] = self.descriptor.all
            if query_abilities:
                # fw update or whatever might have modified the device abilities.
                # we refresh the abilities list before saving the new config_entry
                data[mlc.CONF_PAYLOAD][mc.KEY_ABILITY] = (
                    await self.async_request(
                        *mn.Appliance_System_Ability.request_default
                    )
                ).payload[mc.KEY_ABILITY]
            self.parent.config_entries.async_update_entry(self.config_entry, data=data)

        # we also take the time to sync our tz to the device timezone
        await self._async_init_zoneinfo()

    async def async_handle_request_multiple(
        self, requests: "Iterable[MerossRequestType]"
    ) -> MerossResponse:
        """Send requests in a single NS_APPLIANCE_CONTROL_MULTIPLE message.
        If the whole request is succesful (might be partial if the device response
        overflown somehow (see JSON patching in HTTP request api)
        returns the unpacked reponses in a list.
        """
        response = await AbstractClient.async_request_multiple(self, requests)
        for message in response.payload[mc.KEY_MULTIPLE]:
            self._handle(MerossMessage(message))
        return response

    def profile_linked(self, profile: "MQTTProfile", /):
        assert self.profile is not profile
        if self.profile:
            self.profile.unlink(self)
        self.profile = profile  # type: ignore[assignment]
        self.log(self.DEBUG, "linked to profile:%s", userid=profile.id)
        self._check_protocol()
        if device_info := profile.get_device_info(self.id):
            self.update_device_info(device_info, profile)

    def profile_unlinked(self):
        assert self.profile
        if self.mqtt:
            self.remove_client(self.mqtt)
        self.log(self.DEBUG, "unlinked from profile:%s", userid=self.profile.id)
        self.profile = None  # type: ignore[assignment]

    def _handle(self, message: MerossMessage, /):
        # This is almost superseeded by direct NamespaceHandler request/dispatching
        # it is left mainly for unsolicited MQTT received messages (mainly PUSH but
        # sometimes others) handling and for NS_MULTIPLE dispatching
        method = message.method
        if method == mc.METHOD_GETACK:
            pass
        elif method == mc.METHOD_SETACK:
            # SETACK generally doesn't carry any state/info so it is
            # no use parsing..moreover, our callbacks system is full
            # in place so we have no need to further process
            return
        elif method == mc.METHOD_ERROR:
            if message.payload.get(mc.KEY_ERROR) == mc.ERROR_INVALIDKEY:
                self.log(
                    self.WARNING,
                    "Key error: the configured device key is wrong",
                    timeout=14400,
                )
            else:
                self.log(
                    self.WARNING,
                    "Protocol error: namespace:%s payload:%s",
                    message.namespace,
                    _payload=message.payload,
                    timeout=14400,
                )
            return

        try:
            # We could use self.get_handler_by_name here but this is
            # more 'smart' since we can eventually add a grammar (mn.Namespace)
            # on the fly by inspecting the received message in case the ns is
            # not yet normalized.
            ns_handler = self.ns_handlers[message.namespace]  # type: ignore
        except KeyError:
            # we don't have an handler in place and this is typically due to
            # PUSHES of unknown/unmanaged namespaces
            namespace = message.namespace
            if not namespace:
                # this weird error appears in an ns_multiple response missing
                # the expected namespace key for "Appliance.Control.Runtime"
                self.log(
                    self.WARNING,
                    "Protocol error: received empty namespace (message: %s)",
                    _message=message,
                    timeout=14400,
                )
                return
            # here the namespace might be unknown to our definitions (mn.Namespace)
            # so we try, in case, to build a new one with good presets
            if namespace in self.NAMESPACE_IGNORE:
                ns_handler = handler.VoidHandler(
                    mn.NAMESPACES[namespace],
                    self,
                    config=mlc.POLLING_CONFIG_DIAGNOSTIC,
                )
            else:
                ns_handler = DiagnosticHandler(
                    mn.NAMESPACES.get(namespace)
                    or mn.Namespace.from_message(namespace, method, message.payload),
                    self,
                    config=mlc.POLLING_CONFIG_DIAGNOSTIC,
                )

        if method == mc.METHOD_PUSH:
            # we're saving for diagnostic purposes so we have knowledge of
            # which data the device pushes asynchronously
            ns_handler.last_rx_push = message.payload

        ns_handler.handle_response(message)

    def _handle_Appliance_Mcu_Firmware(self, message: MerossMessage, /):
        self.descriptor.mcu = message.payload[mc.KEY_FIRMWARE]
        if self.update_firmware:
            self.update_firmware.flush_state()

    _handle_Appliance_Mcu_Hp110_Firmware = _handle_Appliance_Mcu_Firmware

    def _handle_Appliance_System_All(self, message: MerossMessage, /):
        # see issue #341. In case we receive a formally correct response from a
        # mismatched device we should stop everything and obviously don't update our
        # ConfigEntry. Here we check first the identity of the device sending this payload
        # in order to not mess our configuration. All in all this check should be not
        # needed since the only reasonable source of 'device mismatch' is the HTTP protocol
        # which is already guarded in our async_http_request
        response_uuid = message.payload[mc.KEY_ALL][mc.KEY_SYSTEM][mc.KEY_HARDWARE][
            mc.KEY_UUID
        ]
        if self.id != response_uuid:
            self._process_uuid_mismatch(response_uuid, message.payload)
            return

        self.remove_issue(mlc.ISSUE_DEVICE_ID_MISMATCH)

        descr = self.descriptor
        oldfirmware = descr.firmware
        oldtimezone = descr.timezone

        device.Device._handle_Appliance_System_All(self, message)

        if oldfirmware != descr.firmware:
            self.schedule_entry_update(True)
            if self.update_firmware:
                self.update_firmware.flush_state()
            if not self.config.get(mlc.CONF_HOST):
                self._update_host(descr.innerIp)
        elif oldtimezone != descr.timezone:
            self.schedule_entry_update(False)

    def _handle_Appliance_System_Time(self, message: MerossMessage, /):
        self.descriptor.update_time(message.payload[mc.KEY_TIME])
        self.schedule_entry_update(False)

    def _check_device_timerules(self) -> bool:
        """
        verify the data about DST changes in the configured timezone are ok by checking
        the "time" key in the Appliance.System.All payload:
        "time": {
            "timestamp": 1560670665,
            "timezone": "Australia/Sydney",
            "timeRule": [
                [1554566400,36000,0],
                [1570291200,39600,1],
                ...
            ]
        }
        returns True in case we need to fix the device configuration
        see https://github.com/arandall/meross/blob/main/doc/protocol.md#appliancesystemtime
        """
        timestamp = self.device_timestamp  # we'll check against its own timestamp
        time = self.descriptor.time
        timerules: list = time.get(mc.KEY_TIMERULE, [])
        timezone = time.get(mc.KEY_TIMEZONE)
        if timezone:
            # assume "timeRule" entries are ordered on epoch(s)
            # timerule: [1554566400,36000,0] -> [epoch, utcoffset, isdst]
            if not timerules:
                # array empty?
                return True

            def _get_epoch(_timerule: list):
                return _timerule[0]

            idx = bisect_right(timerules, timestamp, key=_get_epoch)
            if idx == 0:
                # epoch is not (yet) covered in timerules
                return True

            timerule = timerules[idx - 1]  # timerule in effect at the 'epoch'
            device_tzinfo = self.tz

            def _check_incorrect_timerule(_epoch, _timerule):
                _device_datetime = datetime_from_epoch(_epoch, device_tzinfo)
                _utcoffset = device_tzinfo.utcoffset(_device_datetime)
                if _timerule[1] != (_utcoffset.seconds if _utcoffset else 0):
                    return True
                _dstoffset = device_tzinfo.dst(_device_datetime)
                return _timerule[2] != (1 if _dstoffset else 0)

            if _check_incorrect_timerule(timestamp, timerule):
                return True
            # actual device time is covered but we also check if the device timerules
            # are ok in the near future
            timestamp_future = timestamp + self.PARAM_CHECK_DEVICE_TIMEZONE_FUTURE_DELTA
            # we have to search (again) in the timerules but we do some
            # short-circuit checks to see if epoch_future is still
            # contained in current timerule
            if idx == len(timerules):
                # timerule is already the last in the list so it will be the only active
                # from now on
                pass
            else:
                timerule_next = timerules[idx]
                timestamp_next = timerule_next[0]
                if timestamp_future >= timestamp_next:
                    # the next timerule will take over
                    # so we check if the transition time set in the device
                    # is correct with the tz database
                    if _check_incorrect_timerule(timestamp_next - 1, timerule):
                        return True
                    if _check_incorrect_timerule(timestamp_next + 1, timerule_next):
                        return True
                    # transition set in timerule_next is coming soon
                    # and will be ok
                    return False

            if _check_incorrect_timerule(timestamp_future, timerule):
                return True

        else:
            # no timezone set in the device so we'd expect an empty timerules
            if timerules:
                return True

        return False

    def _check_device_time(self, /):
        """This is a scheduled task that runs every now and then when the device is connected
        to check the device time configuration and state.
        -> Checks device current timestamp (epoch) is aligned to HA. This is especially important
        for devices with 'Consumption' or schedules (calendar entities) in order to align device
        local time to what is expected in HA.
        -> Checks the device timezone has the same utc offset as HA local timezone.
        -> Checks if the device timezone transition times are still correctly set."""
        self.schedule_callback(
            self.PARAM_CHECK_DEVICE_TIME_REPEAT_DELAY, self._check_device_time
        )
        # The device timestamp is usually unaligned when it cannot reach NTP service.
        # It seemed some devices could allow NTP sync through a simple MQTT transaction.
        if abs(self.device_timedelta) > self.PARAM_DEVICE_TIMESTAMP_TOLERANCE:
            _log_this = True
            epoch = self.last_rx_epoch
            # only apply the time-sync procedure when locally mqtt binded
            if (
                (mqtt := self.mqtt)
                and mqtt.connection.can_publish  # online and allow_publish
                and (not mqtt.connection.is_cloud)
                and (mn.Appliance_System_Clock in self.descriptor.ability)
            ):
                last_config_delay = epoch - self.device_timedelta_config_epoch
                if last_config_delay > 1800:
                    # 30 minutes 'cooldown' in order to avoid restarting
                    # the procedure too often
                    mqtt.create_task(
                        mqtt.async_request(*mn.Appliance_System_Clock.request_default),
                        ".check_device_time",
                        eager_start=True,
                    )
                    self.device_timedelta_config_epoch = epoch
                    _log_this = False
                elif last_config_delay < 30:
                    # 30 sec 'deadzone' where we allow the timestamp
                    # transaction to complete (should really be like few seconds)
                    _log_this = False
            if (
                _log_this and (epoch - self.device_timedelta_log_epoch) > 604800
            ):  # 1 week lockout
                self.device_timedelta_log_epoch = epoch
                self.log(
                    self.WARNING,
                    "Incorrect timestamp: %d seconds behind HA (%d on average)",
                    int(epoch - self.device_timestamp),
                    int(self.device_timedelta),
                )
        else:
            # only apply the timezone rules check/fix procedure when locally mqtt binded
            if (
                (mqtt := self.mqtt)
                and mqtt.connection.can_publish  # online and allow_publish
                and (not mqtt.connection.is_cloud)
                and (mn.Appliance_System_Time in self.descriptor.ability)
            ):
                with self.exception_warning("check_device_timerules"):
                    if self._check_device_timerules():
                        # timezone trans not good..fix and check again soon
                        self.create_task(
                            self.async_configure_timezone(self.descriptor.timezone),
                            ".check_device_time",
                            eager_start=True,
                        )
        # Verifies the device timezone has the same utc offset as HA local timezone.
        # This is expecially sensible when the device has 'Consumption' or
        # schedules (calendar entities) in order to align device local time to
        # what is expected in HA.
        ha_now = dt_util.now()
        if ha_now.utcoffset() == ha_now.astimezone(self.tz).utcoffset():
            self.remove_issue(mlc.ISSUE_DEVICE_TIMEZONE)
        else:
            self.create_issue(
                mlc.ISSUE_DEVICE_TIMEZONE,
                severity=self.IssueSeverity.WARNING,
                translation_placeholders={"device_name": self.display_name},
            )

    def enable_check_device_time(self, /):
        """Public method to trigger the device time check procedure. If already in place, it will be restarted."""
        self._check_device_time_enabled = True
        if self.is_connected:
            self.schedule_callback(
                self.PARAM_CHECK_DEVICE_TIME_START_DELAY, self._check_device_time
            )

    def parse_undefined_dict(
        self, key_parent: str, payload: dict, index: mn.IndexValue, /
    ):
        device_entities = self.entities
        excluded = (
            mc.KEY_ID,
            mc.KEY_SUBID,
            mc.KEY_CHANNEL,
            mc.KEY_LMTIME,
            mc.KEY_LMTIME_,
            mc.KEY_SYNCEDTIME,
            mc.KEY_LATESTSAMPLETIME,
            "lastActiveTime",
        )
        for key, value in payload.items():
            if key in excluded:
                continue
            if type(value) is dict:
                self.parse_undefined_dict(f"{key_parent}_{key}", value, index)
                continue
            if type(value) is list:
                self.parse_undefined_list(f"{key_parent}_{key}", value, index)
                continue

            try:
                device_entities[
                    (
                        f"{index.slug}_{key_parent}_{key}"
                        if index
                        else f"{key_parent}_{key}"
                    )
                ].update_device_value(value)
            except KeyError:
                from ..sensor import DiagnosticParser

                DiagnosticParser(
                    index.value,
                    self,
                    entity_key=f"{key_parent}_{key}",
                    index=index,
                    ns_value=value,
                )
            except Exception as e:
                self.log_exception(
                    self.WARNING,
                    e,
                    "Error updating diagnostic entity for key '%s' with value '%s'",
                    key,
                    value,
                )

    def parse_undefined_list(
        self, key_parent: str, payload: list, index: mn.IndexValue, /
    ):
        pass

    def _process_uuid_mismatch(
        self, response_uuid: str, payload_all: "MerossPayloadType | None"
    ):
        """When detecting a wrong uuid from a response we offline the device and setup an issue."""
        if self.is_connected:
            self.on_disconnect()
        try:
            _remote_type = payload_all[mc.KEY_ALL][mc.KEY_SYSTEM][mc.KEY_HARDWARE][mc.KEY_TYPE]  # type: ignore
        except Exception:
            _remote_type = "<unknown>"
        self.log(
            self.CRITICAL,
            "Wrong device at configured address %s (configured uuid:%s, remote uuid:%s, type:%s)",
            self.host or "<unknown>",
            self.id,
            response_uuid,
            _remote_type,
            timeout=900,
        )
        self.create_issue(
            mlc.ISSUE_DEVICE_ID_MISMATCH,
            severity=self.IssueSeverity.CRITICAL,
            translation_placeholders={"device_name": self.display_name},
        )

    def update_device_info(self, device_info: "DeviceInfoType", profile: "MQTTProfile"):
        """Called when linked to a (cloud) profile and device info is available or whenever updated."""
        device_registry = self.parent.device_registry
        name = device_info.get(mc.KEY_DEVNAME) or self.descriptor.productname
        if name != self.device_entry.name:
            device_registry.async_update_device(self.device_entry.id, name=name)

        # check for firmware updates too
        if latest_version := profile.get_latest_version(*self.descriptor.type_subtype):
            self.latest_version = latest_version
            if self.update_firmware:
                self.update_firmware.flush_state()
            else:
                UpdateEntity(self, self)

        channels_info = device_info.get("channels")
        if not channels_info:
            return

        channel = -1
        for device_info_channel in channels_info:
            # we assume the device_info.channels struct are mapped
            # to what we consider 'default' entities for the device
            # (i.e. GarageDoor for garageDoor devices, ToggleXSwitch for
            # plain toggle devices, and so on).
            # also, the list looks like eventually containing empty dicts
            # for non-existent channel ids
            channel += 1
            if not device_info_channel:
                continue
            try:
                if name := device_info_channel.get(mc.KEY_DEVNAME):
                    device_entry = device_registry.async_get_device(
                        **self.get_device_entry_info(channel)
                    )
                    if device_entry and name != device_entry.name:
                        device_registry.async_update_device(device_entry.id, name=name)
            except Exception:
                pass
