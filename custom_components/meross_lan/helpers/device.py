import asyncio
from bisect import bisect_right
from datetime import UTC
from json import JSONDecodeError
from time import time
from typing import TYPE_CHECKING, override

from homeassistant.config_entries import ConfigEntryState
from homeassistant.helpers import device_registry as dr
from homeassistant.util import dt as dt_util, slugify

# import core modules instead of symbols to ease patching in a single place
from . import manager as mlm
from .. import const as mlc
from ..button import MLPersistentButton

# only import those 'often used' symbols to get a tiny bit of speed improvement
from ..const import (
    CONF_HOST,
    CONF_PAYLOAD,
    PARAM_TIMESTAMP_TOLERANCE,
)
from ..merossclient import (
    DeviceDescriptor,
    datetime_from_epoch,
    device,
    get_active_broker,
    is_device_online,
)
from ..merossclient.client import AbstractClient, Direction, Transport
from ..merossclient.client.http import HttpClient
from ..merossclient.obfuscate import OBFUSCATE_DICT
from ..merossclient.protocol import MerossError, const as mc, namespaces as mn
from ..merossclient.protocol.message import MerossMessage, MerossResponse
from ..merossclient.protocol.namespaces import thermostat as mn_t
from ..sensor import ProtocolSensor
from ..update import MLUpdate
from .namespaces import NamespaceHandler

if TYPE_CHECKING:
    from asyncio import Future, Task, TimerHandle
    from typing import (
        Any,
        Callable,
        ClassVar,
        Collection,
        Final,
        Iterable,
        Iterator,
        Mapping,
        NotRequired,
        Unpack,
    )

    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

    from ..merossclient import HostAddress
    from ..merossclient.protocol.types import (
        JsonDict,
        JsonList,
        MerossHeaderType,
        MerossMessageType,
        MerossPayloadType,
        MerossRequestType,
        control as mt_c,
    )
    from .component_api import ComponentApi
    from .entity import ChannelType, MLEntity
    from .meross_profile import DeviceInfoType, LatestVersionType
    from .mqtt_profile import MQTTConnection, MQTTProfile

    type DigestParseFunc = Callable[[JsonDict], None] | Callable[[JsonList], None]
    type DigestInitReturnType = tuple[
        DigestParseFunc, Iterable[device.NamespaceHandler]
    ]
    type DigestInitFunc = Callable[[Device, Any], DigestInitReturnType]
    type NamespaceInitFunc = Callable[[Device, mn.Namespace], None]


T_AUTO = Transport.AUTO
T_BLUETOOTH = Transport.BLUETOOTH
T_HTTP = Transport.HTTP
T_MQTT = Transport.MQTT


class BaseDevice(mlm.EntityManager, device.PhysicalDevice):
    """
    Abstract base class for Device and SubDevice (from hub)
    giving common behaviors like device_registry interface
    """

    if TYPE_CHECKING:
        DEVICE_TYPE: ClassVar[mlc.DeviceType]

        update_firmware: MLUpdate | None
        # Overrides
        device_entry: Final[dr.DeviceEntry]  # type: ignore

        class Args(mlm.EntityManager.Args, device.PhysicalDevice.Args):
            device_entry: dr.DeviceEntry

    _attr_is_connected = False

    __SLOTS__ = ("update_firmware",)

    def __init__(self, id: str, parent: mlm.EntityManager, **kwargs: "Unpack[Args]"):
        self.update_firmware = None
        super().__init__(id, parent, **kwargs)

    async def async_shutdown(self):
        await super().async_shutdown()
        del self.update_firmware

    @override
    def on_connect(self, /):
        super().on_connect()
        for entity in self.entities.values():
            entity.set_available()

    @override
    def on_disconnect(self, /):
        super().on_disconnect()
        for entity in self.entities.values():
            entity.set_unavailable()

    # interface: self
    def update_latest_version(self, latest_version: "LatestVersionType"):
        # TODO: add the update invocation path for Hub Subdevices.
        self.latest_version = latest_version
        if self.update_firmware:
            self.update_firmware.update_info()
        else:
            self.update_firmware = MLUpdate(self)

    def parse_undefined_dict(
        self, key_parent: str, payload: dict, channel: "ChannelType | None", /
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
                self.parse_undefined_dict(f"{key_parent}_{key}", value, channel)
                continue
            if type(value) is list:
                self.parse_undefined_list(f"{key_parent}_{key}", value, channel)
                continue
            try:
                device_entities[
                    (
                        f"{channel}_{key_parent}_{key}"
                        if channel is not None
                        else f"{key_parent}_{key}"
                    )
                ].update_native_value(value)
            except KeyError:
                from ..sensor import MLDiagnosticSensor

                MLDiagnosticSensor(
                    channel,
                    self,
                    entity_key=f"{key_parent}_{key}",
                    native_value=value,
                )

    def parse_undefined_list(
        self, key_parent: str, payload: list, channel: "ChannelType | None", /
    ):
        pass


_IGNORED_NAMESPACES_CFG = (".merossclient.device.handler", "VoidNamespaceHandler")
_IGNORED_NAMESPACES = (
    mn.Appliance_Config_Info,
    mn.Appliance_Control_Bind,
    mn.Appliance_Control_ConsumptionConfig,
    mn.Appliance_System_Clock,
    mn.Appliance_System_Online,
    mn.Appliance_System_Report,
)


class Device(mlm.ConfigEntryManager, device.Device, BaseDevice):
    """
    Generic protocol handler class managing the physical device stack/state
    """

    class Http(HttpClient):

        if TYPE_CHECKING:
            device: Final["Device"]  # type: ignore[override]

        @override
        def on_rx_raw(self, raw: bytes | bytearray, /) -> MerossMessage:
            try:
                response = super().on_rx_raw(raw)
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
                    namespace = device.NAMESPACES[namespace]
                match namespace:
                    case mn.Appliance_Control_Multiple:
                        list_break_matcher = '},{"header":'
                    case _:
                        if not namespace.key_idx:
                            raise
                        list_break_matcher = f'}},{{"{namespace.key_idx}":'

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

        DIGEST_INIT: Final[dict[str, Any]]
        """ Static dict of 'digest initialization function(s)'.
        This is built on demand during Device init whenever a new digest key
        is encountered. This static dict in turn is used to setup the Device instance
        'digest_handlers' dict which contains a lookup to the digest parsing function when
        an Appliance.System.All message is received/parsed.
        The 'digest initialization function' will (at device init time) parse the digest to
        setup the dedicated entities for the particular digest key.
        The definition of this init function is looked up at runtime by an algorithm that:
        - looks-up if the digest key is in DIGEST_INITIALIZERS where it'll find either the
        function or the (str) module coordinates of the init function for the digest key.
        - if not configured, the algorithm will try load the module in meross_lan/devices
        with the same name as the digest key.
        - if any is not found we'll set a 'digest_init_empty' function in order to not
        repeat the lookup process. That function will just pass so that the key
        init/parsing will not harm."""
        NAMESPACE_INIT: Final[dict[mn.Namespace, Any]]
        """ Static dict of namespace initialization functions. This will be looked up
        and matched against the current device abilities (at device init time) and
        usually setups a dedicated namespace handler and/or a dedicated entity.
        As far as the initialization functions are looked up in related modules,
        they'll be cached in the dict.
        Namespace handlers will be initialized in the order as they appear in the dict
        and this could have consequences in the order of polls."""
        TRACE_ABILITY_EXCLUDE: ClassVar[tuple[str, ...]]
        """ When tracing we enumerate appliance abilities to get insights on payload structures
        this list will be excluded from enumeration since it's redundant/exposing sensitive info
        or simply crashes/hangs the device."""

        descriptor: Final[DeviceDescriptor]  # type: ignore[override]
        bluetooth: Final[ComponentApi.BTClient | None]  # type: ignore[override]
        http: Final[Http | None]  # type: ignore[override]
        mqtt: Final[MQTTConnection.Client | None]  # type: ignore[override]
        ns_handlers: Final[dict[str, NamespaceHandler]]  # type: ignore[override]

        def get_handler(self, ns: mn.Namespace) -> NamespaceHandler: ...

        # these are set from ConfigEntry
        conf_protocol: Transport
        host: str | None

        device_timestamp: int

        _device_entries: dict[Any, dr.DeviceEntry]
        profile: Final[MQTTProfile | None]
        digest_parsers: Final[dict[str, DigestParseFunc]]
        digest_pollers: Final[set[device.NamespaceHandler]]
        _trace_ability_callback_unsub: TimerHandle | None
        _check_device_timerules_unsub: TimerHandle  # dynamic
        _async_create_diagnostic_entities_task: Task  # dynamic

        # entities
        sensor_protocol: ProtocolSensor

    @staticmethod
    def digest_parse_empty(digest: dict | list):
        pass

    @staticmethod
    def digest_init_empty(
        device: "Device", digest: dict | list
    ) -> "DigestInitReturnType":
        return Device.digest_parse_empty, ()

    @staticmethod
    def namespace_init_empty(device: "Device", namespace: mn.Namespace):
        pass

    DEVICE_TYPE = mlc.DeviceType.DEVICE

    DIGEST_INIT = {
        mc.KEY_FAN: ".fan",
        mc.KEY_HUB: ".devices.hub",
        mc.KEY_LIGHT: ".light",
        "light.effect": ".light",
        mc.KEY_TIMER: digest_init_empty,
        mc.KEY_TIMERX: digest_init_empty,
        mc.KEY_TOGGLE: ".switch",
        mc.KEY_TOGGLEX: ".switch",
        mc.KEY_TRIGGER: digest_init_empty,
        mc.KEY_TRIGGERX: digest_init_empty,
    }

    NAMESPACE_INIT = {
        mn.Appliance_Config_OverTemp: (".devices.mss", "OverTempEnableSwitch"),
        mn.Appliance_Control_Alarm: (".siren", "MLSiren"),
        mn.Appliance_Control_Electricity: (
            ".devices.mss",
            "namespace_init_electricity",
        ),
        mn.Appliance_Control_ElectricityX: (".devices.mss", "ElectricityXSensor"),
        mn.Appliance_Control_ConsumptionH: (
            ".devices.mss",
            "ConsumptionHNamespaceHandler",
        ),
        mn.Appliance_Control_ConsumptionX: (".devices.mss", "ConsumptionXSensor"),
        mn.Appliance_Control_Fan: (".fan", "namespace_init_fan"),
        mn.Appliance_Control_FilterMaintenance: (
            ".sensor",
            "MLFilterMaintenanceSensor",
        ),
        mn.Appliance_Control_Mp3: (".media_player", "MLMp3Player"),
        mn.Appliance_Control_PhysicalLock: (".switch", "PhysicalLockSwitch"),
        mn.Appliance_Control_Presence_Config: (
            ".devices.ms600",
            "PresenceConfigMode",
        ),
        mn.Appliance_Control_Screen_Brightness: (
            ".devices.thermostat",
            "ScreenBrightnessNamespaceHandler",
        ),
        mn.Appliance_Control_Sensor_Latest: (
            ".devices.misc",
            "SensorLatestNamespaceHandler",
        ),
        mn.Appliance_Control_Sensor_LatestX: (
            ".devices.misc",
            "namespace_init_sensor_latestx",
        ),
        mn_t.Appliance_Control_Thermostat_ModeC: (
            ".devices.thermostat.mts300",
            "Mts300Climate",
        ),
        mn.Appliance_Mcu_Firmware: (
            ".helpers.namespaces",
            "NamespaceHandler",  # handler in Device._handle_XXX
        ),
        mn.Appliance_Mcu_Hp110_Firmware: (
            ".helpers.namespaces",
            "NamespaceHandler",  # handler in Device._handle_XXX
        ),
        mn.Appliance_RollerShutter_Position: (
            ".devices.rollershutter",
            "MLRollerShutter",
        ),
        mn.Appliance_System_DNDMode: (".light", "MLDNDLightEntity"),
        mn.Appliance_System_Runtime: (".sensor", "MLSignalStrengthSensor"),
    } | {_ns: _IGNORED_NAMESPACES_CFG for _ns in _IGNORED_NAMESPACES}

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

    DEFAULT_PLATFORMS = mlm.ConfigEntryManager.DEFAULT_PLATFORMS | {
        MLUpdate.PLATFORM: None,
    }

    __slots__ = device.Device._calc_slots(
        "conf_protocol",
        "host",
        "_device_entries",
        "_async_entry_update_unsub",
        "device_debug",
        "device_timestamp",
        "device_timedelta",
        "device_timedelta_log_epoch",
        "device_timedelta_config_epoch",
        "_profile",
        "digest_parsers",
        "digest_pollers",
        "_check_device_timerules_unsub",
        "_trace_ability_callback_unsub",
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

        super().__init__(
            device_id,
            api,
            config_entry,
            device_entry=api.device_registry.async_get_or_create(
                config_entry_id=config_entry.entry_id,
                connections={(dr.CONNECTION_NETWORK_MAC, descriptor.macAddress)},
                manufacturer=mc.MANUFACTURER,
                name=descriptor.productname,
                model=descriptor.productmodel,
                hw_version=descriptor.hardwareVersion,
                sw_version=descriptor.firmwareVersion,
                identifiers={(mlc.DOMAIN, device_id)},
            ),
            # configure AbstractClient
            key=config_entry.data.get(mlc.CONF_KEY) or "",  # type: ignore[argument]
            descriptor=descriptor,  # type: ignore[argument],
        )
        self._async_entry_update_unsub = None
        self.device_debug = None
        self.device_timestamp = 0
        self.device_timedelta = 0
        self.device_timedelta_log_epoch = 0
        self.device_timedelta_config_epoch = 0
        self.profile = None
        self.digest_parsers = {}
        self.digest_pollers = set()
        if mn.Appliance_System_Time in descriptor.ability:
            self._check_device_timerules_unsub = self.schedule_async_callback(
                60, self.check_device_timerules
            )
        self._trace_ability_callback_unsub = None

        self.sensor_protocol = ProtocolSensor(self)
        MLPersistentButton(
            None,
            self,
            "button_refresh",
            self._async_button_refresh_press,
            name="Refresh",
            device_class=MLPersistentButton.DeviceClass.RESTART,
            entity_category=MLPersistentButton.EntityCategory.DIAGNOSTIC,
        )
        MLPersistentButton(
            None,
            self,
            "button_reload",
            self._async_button_reload_press,
            name="Reload",
            device_class=MLPersistentButton.DeviceClass.RESTART,
            entity_category=MLPersistentButton.EntityCategory.DIAGNOSTIC,
        )

    async def async_init(self):

        await self._async_update_config()

        descriptor = self.descriptor
        if tzname := descriptor.timezone:
            # self.tz defaults to UTC on init
            with self.exception_warning(
                "loading timezone(%s) - check your python environment",
                tzname,
                timeout=14400,
            ):
                self.tz = await self.api.async_load_zoneinfo(tzname)

        for key_digest, _digest in (
            descriptor.digest.items() or descriptor.control.items()
        ):
            # older firmwares (MSS110 with 1.1.28) look like
            # carrying 'control' instead of 'digest'
            try:
                try:
                    self.digest_parsers[key_digest], _digest_pollers = (
                        Device.DIGEST_INIT[key_digest](self, _digest)
                    )
                except (KeyError, TypeError):
                    # KeyError: key is unknown to our code (fallback to lookup ".devices.{key_digest}")
                    # TypeError: key is a string containing the module path
                    key_slug = slugify(key_digest)
                    _module_path = Device.DIGEST_INIT.get(
                        key_digest, f".devices.{key_slug}"
                    )
                    if type(_module_path) is not str:
                        # This means we catched an error inside the digest init func
                        raise
                    try:
                        digest_init_func: "DigestInitFunc" = getattr(
                            await self.api.async_import_module(_module_path),
                            f"digest_init_{key_slug}",
                        )
                    except Exception as exception:
                        self.log_exception(
                            self.WARNING,
                            exception,
                            "loading digest initializer for key '%s'",
                            key_digest,
                        )
                        digest_init_func = Device.digest_init_empty
                    Device.DIGEST_INIT[key_digest] = digest_init_func
                    self.digest_parsers[key_digest], _digest_pollers = digest_init_func(
                        self, _digest
                    )
                self.digest_pollers.update(_digest_pollers)

            except Exception as exception:
                self.log_exception(
                    self.WARNING, exception, "initializing digest key '%s'", key_digest
                )
                self.digest_parsers[key_digest] = Device.digest_parse_empty

        ability = descriptor.ability
        for ns, ns_init_func in self.NAMESPACE_INIT.items():
            if ns not in ability:
                continue
            try:
                try:
                    ns_init_func(self, ns)
                except TypeError:
                    try:
                        ns_init_func = getattr(
                            await self.api.async_import_module(ns_init_func[0]),
                            ns_init_func[1],
                        )
                    except Exception as exception:
                        self.log_exception(
                            self.WARNING,
                            exception,
                            "loading namespace initializer for %s",
                            ns,
                        )
                        Device.NAMESPACE_INIT[ns] = Device.namespace_init_empty
                    else:
                        try:
                            ns_init_func = ns_init_func.namespace_init
                        except AttributeError:
                            pass
                        Device.NAMESPACE_INIT[ns] = ns_init_func
                        ns_init_func(self, ns)

            except Exception as exception:
                self.log_exception(
                    self.WARNING, exception, "initializing namespace %s", ns
                )

    def start(self):
        # called by async_setup_entry after the entities have been registered
        # here we'll register mqtt listening (in case) and start polling after
        # the states have been eventually restored (some entities need this)
        self._check_protocol_ext()
        self._polling_unsub = self.schedule_callback(0, self._poll, None)

    async def async_shutdown(self):
        self.remove_issue(mlc.ISSUE_DEVICE_TIMEZONE)
        try:
            self._check_device_timerules_unsub.cancel()
        except AttributeError:
            pass
        if self._async_entry_update_unsub:
            self._async_entry_update_unsub.cancel()
            self._async_entry_update_unsub = None
        if self.bluetooth:
            # bluetooth client is managed by ComponentApi so we dont shutdown it
            # (super().async_shutdown will also shutdown clients) but just unlink it from the device
            self.remove_client(self.bluetooth)
        await super().async_shutdown()
        if self.profile:
            self.profile.unlink(self)
        self.digest_parsers.clear()
        self.digest_pollers.clear()
        del self.sensor_protocol
        self.api.devices[self.id] = None
        self.log(  # REMOVE
            self.DEBUG, "Device.async_shutdown complete (object: %s)", self.objects
        )

    # miscellaneous internals to prepare/refresh internal config
    async def _async_update_config(self):
        """
        common properties caches, read from ConfigEntry on __init__ or when a configentry updates
        """
        config = self.config
        # map CONF_PROTOCOL value to a const symbol in order to use 'is' in Device code checks
        try:
            conf_protocol = Transport.from_str(config[mlc.CONF_PROTOCOL])  # type: ignore
        except KeyError:
            conf_protocol = T_AUTO
        self.conf_protocol = conf_protocol
        self.polling_period = (
            config.get(mlc.CONF_POLLING_PERIOD) or mlc.CONF_POLLING_PERIOD_DEFAULT
        )
        if self.polling_period < mlc.CONF_POLLING_PERIOD_MIN:
            self.polling_period = mlc.CONF_POLLING_PERIOD_MIN
        self._polling_delay = self.polling_period

        self.enable_multiple(not config.get(mlc.CONF_DISABLE_MULTIPLE))

        await self._async_update_host()
        if self.mqtt:  # just to be sure key is sync'd
            self.mqtt.key = self.key

        if conf_protocol is T_BLUETOOTH:
            if not self.bluetooth:
                if _bluetooth := self.api.get_bt_client(self.id):
                    self.add_client(_bluetooth)
                    self.api.device_registry.async_update_device(
                        self.device_entry.id,
                        new_connections={
                            (dr.CONNECTION_NETWORK_MAC, self.descriptor.macAddress),
                            (dr.CONNECTION_BLUETOOTH, _bluetooth.address),
                        },
                    )

        elif self.bluetooth:
            self.remove_client(self.bluetooth)
            self.api.device_registry.async_update_device(
                self.device_entry.id,
                new_connections={
                    (dr.CONNECTION_NETWORK_MAC, self.descriptor.macAddress)
                },
            )

    async def _async_update_host(self):
        host = self.config.get(CONF_HOST)
        if not host:
            host = self.descriptor.innerIp
            if host == "0.0.0.0":  # unbinded device
                host = None

        self.host = host
        http = self.http
        if host and (self.conf_protocol in (T_AUTO, T_HTTP)):
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

    def _check_protocol_ext(self):
        api = self.api
        try:
            profile = api.profiles[self.descriptor.userId]
            if profile and (profile.key != self.key):
                profile = api
        except KeyError:
            profile = api
        if self.profile != profile:
            if self.profile:
                self.profile.unlink(self)
            if profile:
                profile.link(self)
                # _check_protocol already called
                return
        self._check_protocol()

    def _check_protocol(self):
        """called whenever the configuration or the profile linking changes to fix transports"""

        conf_protocol = self.conf_protocol
        if conf_protocol in (T_BLUETOOTH, T_HTTP):
            self.preferred_transport = conf_protocol
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

            if conf_protocol is T_AUTO:
                # When using Transport.AUTO we try to use our 'preferred' transport.
                # When binded to a cloud_profile always prefer http since it will avoid excessive
                # MQTT traffic and related cloud 'issues' like rate-limiting
                if self.config.get(CONF_HOST) or (
                    self.mqtt and self.mqtt.connection.is_cloud
                ):
                    self.preferred_transport = T_HTTP
                else:
                    self.preferred_transport = T_MQTT
            else:  # T_MQTT
                self.preferred_transport = conf_protocol

        if self.transport is not self.preferred_transport:
            try:
                self._switch_client(self._clients_connected[self.preferred_transport])
            except KeyError:
                self.log(
                    self.WARNING,
                    "Preferred transport {%s} not available, current transport is {%s}",
                    self.preferred_transport,
                    self.transport,
                )

    # interface: EntityManager
    @override
    def get_device_entry(self, channel, /):
        if (not channel) or (len(self.descriptor.channels) <= 1):
            return self.device_entry

        try:
            return self._device_entries[channel]
        except AttributeError:
            self._device_entries = {}
        except KeyError:
            pass

        self._device_entries[channel] = device_entry = (
            self.api.device_registry.async_get_or_create(
                config_entry_id=self.config_entry.entry_id,
                manufacturer=mc.MANUFACTURER,
                name=f"{self.device_entry.name} Channel {channel}",
                model=self.device_entry.model,
                via_device=next(iter(self.device_entry.identifiers)),
                identifiers={(mlc.DOMAIN, f"{self.id}_{channel}")},
            )
        )
        return device_entry

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

        await self.async_poll_stop()
        await super().entry_update_listener(hass, config_entry)
        await self._async_update_config()
        self.start()

    async def async_create_diagnostic_entities(self):
        await super().async_create_diagnostic_entities()

    async def async_destroy_diagnostic_entities(self, remove: bool = False):
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
                is NamespaceHandler.async_poll_diagnostic
            ):
                namespace_handler.polling_strategy = None
        await super().async_destroy_diagnostic_entities(remove)

    async def _async_create_diagnostic_entities(self):
        # when create_diagnostic_entities is True, we'll schedule this task
        # that will try at its best to stay alive and finish the abilities scan
        # pausing now and then when offline and/or to not interleave with polling.
        self.log(self.DEBUG, "Diagnostic entities scan begin")
        try:
            abilities = iter(self.descriptor.ability)
            while self.is_connected:
                if (
                    ns_handler := self._trace_ability_next(abilities)
                ) and not ns_handler.polling_strategy:
                    while self.is_connected:
                        # synchronize to polling loop
                        if self._polling_unsub:
                            # not polling now
                            await ns_handler.async_get_safe()
                            break
                        elif self._polling_task:
                            # polling right now...await ends
                            try:
                                await self._polling_task
                            except asyncio.CancelledError:
                                pass
                        else:
                            # not polling but no schedule either (maybe shutdown?)...just wait a bit and retry
                            await asyncio.sleep(0)
            raise Exception("Device disconnected")
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
        self._trace_ability_callback_unsub = self.schedule_async_callback(
            mlc.PARAM_TRACING_ABILITY_POLL_TIMEOUT,
            self._async_trace_ability,
            iter(self.descriptor.ability),
        )

    def trace_close(
        self, exception: Exception | None = None, error_context: str | None = None
    ):
        if self._trace_ability_callback_unsub:
            self._trace_ability_callback_unsub.cancel()
            self._trace_ability_callback_unsub = None
        super().trace_close(exception, error_context)

    def _trace_ability_next(self, abilities: "Iterator[str]", /):
        ability = next(abilities)
        if ability in self.TRACE_ABILITY_EXCLUDE:
            return None
        ns = self.NAMESPACES.get(ability)
        if not ns:  # unknown namespace..setup generic handler
            return self.get_handler_by_name(ability)
        if ns.can_query:
            return self.get_handler(ns)
        return None

    async def _async_trace_ability(self, abilities: "Iterator[str]"):
        self._trace_ability_callback_unsub = None
        try:
            # avoid interleave tracing ability with polling loop
            # also, since we could trigger this at early stages
            # in device init, this check will prevent iterating
            # at least until the device fully initialize through
            # self.start()
            if self.is_connected and not self._polling_task:
                while not (ns_handler := self._trace_ability_next(abilities)):
                    continue
                self.log(self.DEBUG, "Tracing %s ability", ns_handler.ns)
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
                mlc.PARAM_TRACING_ABILITY_POLL_TIMEOUT
                + self.mqtt.connection.get_rl_safe_delay(self.id)
            )
        else:
            timeout = mlc.PARAM_TRACING_ABILITY_POLL_TIMEOUT
        self._trace_ability_callback_unsub = self.schedule_async_callback(
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
            for _profile in self.api.active_profiles():
                if _profile is profile:
                    continue
                if latest_version := _profile.get_latest_version(
                    *self.descriptor.type_subtype
                ):
                    break
        return {
            "class": type(self).__name__,
            "conf_protocol": self.conf_protocol,
            "preferred_transport": self.preferred_transport,
            "transport": self.transport,
            "polling_period": self.polling_period,
            "device_response_size_min": self.device_response_size_min,
            "device_response_size_max": self.device_response_size_max,
            "BLUETOOTH": {
                "bluetooth": T_BLUETOOTH in self._clients,
                "bluetooth_active": T_BLUETOOTH in self._clients_connected,
            },
            "HTTP": {
                "http": T_HTTP in self._clients,
                "http_active": T_HTTP in self._clients_connected,
            },
            "MQTT": {
                "cloud_profile": profile and profile.is_cloud_profile,
                "mqtt_connection": bool(self.mqtt),
                "mqtt_connected": self.mqtt and self.mqtt.connection.is_connected,
                "mqtt_publish": self.mqtt and self.mqtt.connection.can_publish,
                "mqtt_active": self.mqtt_active,
            },
            "namespace_handlers": {
                handler.ns: {
                    "last_poll_epoch": handler.last_poll_epoch,
                    "last_rx_epoch": handler.last_rx_epoch,
                    "lastpush": (
                        OBFUSCATE_DICT(handler.last_rx_push)
                        if (handler.last_rx_push and self.obfuscate)
                        else handler.last_rx_push
                    ),
                    "polling_epoch_next": handler.polling_epoch_next,
                    "polling_strategy": (
                        handler.polling_strategy.__name__
                        if handler.polling_strategy
                        else None
                    ),
                }
                for handler in self.ns_handlers.values()
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
            data = await super().async_get_diagnostics()
            data["trace"] = await self._async_get_diagnostics_trace()
            return data
        else:
            return await super().async_get_diagnostics()

    # interface: AbstractClient
    @override
    def on_connect(self, /):
        super().on_connect()
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
        self.device_debug = None

    @override
    def on_tx(self, message: "MerossMessage", client: "AbstractClient", /):
        self.last_tx_message = message
        self.last_tx_epoch = client.last_tx_epoch
        self.log_message(message, Direction.TX, client.last_tx_epoch, client.TRANSPORT)

    @override
    def on_rx(self, message: "MerossMessage", client: "AbstractClient", /):
        self.last_rx_message = message
        self.last_rx_epoch = epoch = client.last_rx_epoch
        message_size = len(message.json)
        if message_size > self.device_response_size_min:
            self.device_response_size_min = message_size
            if message_size > self.device_response_size_max:
                self.device_response_size_max = message_size
        transport = client.TRANSPORT
        self.log_message(message, Direction.RX, epoch, transport)
        message.check()
        if self.transport is not transport:
            if (self.preferred_transport is transport) or (
                len(self._clients_connected) == 1
            ):
                self._switch_client(client)

        header = message.header
        # we'll use the device timestamp to 'align' our time to the device one
        # this is useful for metered plugs reporting timestamped energy consumption
        # and we want to 'translate' this timings in our (local) time.
        # We ignore delays below PARAM_TIMESTAMP_TOLERANCE since
        # we'll always be a bit late in processing
        self.device_timestamp = header[mc.KEY_TIMESTAMP]
        self.device_timedelta = (
            9 * self.device_timedelta + (epoch - self.device_timestamp)
        ) / 10
        # TODO: move this check to only relevant devices (i.e. metering plugs)
        if abs(self.device_timedelta) > PARAM_TIMESTAMP_TOLERANCE:
            _log_this = True
            if (  # TODO: refine this check
                (mqtt := self.mqtt)
                and (not mqtt.connection.is_cloud)
                and (mn.Appliance_System_Clock in self.descriptor.ability)
            ):
                # only deal with time related settings when devices are un-paired
                # from the meross cloud
                last_config_delay = epoch - self.device_timedelta_config_epoch
                if last_config_delay > 1800:
                    # 30 minutes 'cooldown' in order to avoid restarting
                    # the procedure too often
                    self.create_task(
                        mqtt.async_request(*mn.Appliance_System_Clock.request_default),
                        "._config_device_timestamp",
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
                # log this only if we're not in the cooldown period and we haven't
                # logged about this recently (i.e. in the last week)
                self.device_timedelta_log_epoch = epoch
                self.log(
                    self.WARNING,
                    "Incorrect timestamp: %d seconds behind HA (%d on average)",
                    int(epoch - self.device_timestamp),
                    int(self.device_timedelta),
                )

        if self.isEnabledFor(self.DEBUG):
            # it appears sometimes the devices
            # send an incorrect signature hash
            # but at the moment this is unlikely to be critical
            sign = message.compute_signature(self.key)
            if sign != header[mc.KEY_SIGN]:
                self.log(
                    self.DEBUG,
                    "Received signature error: computed=%s, header=%s",
                    sign,
                    _header=header,
                )

    @override
    def log_message(
        self,
        message: MerossMessage,
        direction: str,
        epoch: float,
        transport: Transport,
        /,
    ):
        if self.is_tracing:
            self.trace(
                epoch,
                message.payload,
                message.namespace,
                message.method,
                transport,
                direction,
            )
        # here we avoid using self.log since it would
        # log to the trace file too but we've already 'traced' the
        # message if that's the case
        logger = self.logger
        if logger.isEnabledFor(self.VERBOSE):
            logger._log(
                self.VERBOSE,
                "%s: %s(%s) %s %s %s",
                (
                    transport.upper(),
                    direction,
                    message.messageid,
                    message.method,
                    message.namespace,
                ),
                _message=message,
                obfuscate=self.obfuscate,
            )
        elif logger.isEnabledFor(self.DEBUG):
            logger._log(
                self.DEBUG,
                "%s: %s(%s) %s %s",
                (
                    transport.upper(),
                    direction,
                    message.messageid,
                    message.method,
                    message.namespace,
                ),
            )

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

    @override
    async def async_poll_full(self):
        # TODO: we need to override here because we still don't have a clear way to avoid
        # re-entrance issues in polling management
        await self.async_poll_stop()
        # before retriggering ensure we're not overlapping with device shutdown
        if self.config_entry.state is ConfigEntryState.LOADED:
            self.device_debug = None
            for handler in self.ns_handlers.values():
                handler.polling_epoch_next = 0.0
            # this will also restart/schedule the cycle
            await self._poll()

    # interface: self
    def register_parser_entity(self, entity: "MLEntity", /):
        self.get_handler(entity.ns).register_parser(entity)

    def register_togglex_channel(self, entity: "MLEntity", active: bool, /):
        """
        Checks if entity has an associated ToggleX behavior and eventually
        registers it
        """
        try:
            for togglex_digest in self.descriptor.digest[mc.KEY_TOGGLEX]:
                if togglex_digest[mc.KEY_CHANNEL] == entity.channel:
                    # TODO: _parse_togglex need to be automatically bound
                    # here but this code might be improved in some way
                    # without needing to allocate new lambdas for each entity
                    if active:
                        entity._parse_togglex = (
                            lambda payload: entity.update_native_value(
                                payload[mc.KEY_ONOFF]
                            )
                        )
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
        if self._async_entry_update_unsub:
            self._async_entry_update_unsub.cancel()
        self._async_entry_update_unsub = self.schedule_async_callback(
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
        self._async_entry_update_unsub = None

        with self.exception_warning("_async_entry_update"):
            data = dict(self.config_entry.data)
            data[mlc.CONF_TIMESTAMP] = time()  # force ConfigEntry update..
            data[CONF_PAYLOAD][mc.KEY_ALL] = self.descriptor.all
            if query_abilities:
                # fw update or whatever might have modified the device abilities.
                # we refresh the abilities list before saving the new config_entry
                data[CONF_PAYLOAD][mc.KEY_ABILITY] = (
                    await self.async_request(
                        *mn.Appliance_System_Ability.request_default
                    )
                ).payload[mc.KEY_ABILITY]
            self.api.config_entries.async_update_entry(self.config_entry, data=data)

        # we also take the time to sync our tz to the device timezone
        tzname = self.descriptor.timezone
        if tzname:
            with self.exception_warning(
                "loading timezone(%s) - check your python environment",
                tzname,
                timeout=14400,
            ):
                self.tz = await self.api.async_load_zoneinfo(tzname)
        else:
            self.tz = UTC

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
            handler = self.ns_handlers[message.namespace]
        except KeyError as key_error:
            namespace = key_error.args[0]
            # we don't have an handler in place and this is typically due to
            # PUSHES of unknown/unmanaged namespaces
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
            handler = self._create_handler(
                self.NAMESPACES.get(namespace)
                or mn.Namespace.from_message(
                    namespace, method, message.payload, self.NAMESPACES
                )
            )

        if method == mc.METHOD_PUSH:
            # we're saving for diagnostic purposes so we have knowledge of
            # which data the device pushes asynchronously
            handler.last_rx_push = message.payload

        handler.handle_response(message)

    @override
    def _create_handler(self, ns: "mn.Namespace", /):
        """Called by the base device message parsing chain when a new
        NamespaceHandler need to be defined (This happens the first time
        the namespace enters the message handling flow)"""
        return NamespaceHandler(self, ns)

    def _handle_Appliance_Mcu_Firmware(self, message: MerossMessage, /):
        self.descriptor.mcu = message.payload[mc.KEY_FIRMWARE]
        if self.update_firmware:
            self.update_firmware.update_info()

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
        descr.update(message.payload)

        if oldfirmware != descr.firmware:
            self.schedule_entry_update(True)
            if self.update_firmware:
                self.update_firmware.update_info()
            if not self.config.get(CONF_HOST):
                self.create_task(
                    self._async_update_host(),
                    "_handle_Appliance_System_All._async_update_host",
                    eager_start=True,
                )
        elif oldtimezone != descr.timezone:
            self.schedule_entry_update(False)

        if self.conf_protocol is T_AUTO:
            if (mqtt := self.mqtt) and mqtt.is_connected:
                if not is_device_online(descr.system):
                    mqtt.on_disconnect()
            elif is_device_online(descr.system):
                if not self.device_debug:
                    self.get_handler(mn.Appliance_System_Debug).schedule_get()
            else:
                self.device_debug = None

        for key_digest, _digest in descr.digest.items() or descr.control.items():
            try:
                self.digest_parsers[key_digest](_digest)
            except Exception as e:
                self.log_exception(
                    self.WARNING,
                    e,
                    "parsing digest '%s' with parser '%r'",
                    key_digest,
                    self.digest_parsers.get(key_digest),
                )

    def _handle_Appliance_System_Debug(self, message: MerossMessage, /):
        # this ns is queried when we're HTTP connected and the device reports it is
        # also MQTT connected but meross_lan has no confirmation (_mqtt_active == None)
        # we're then going to inspect the device reported broker and see if
        # our config allow to connect
        self.device_debug = message.payload[mc.KEY_DEBUG]
        if mqtt := self.mqtt:
            broker = get_active_broker(self.device_debug)
            if mqtt.id.host == broker.host:
                if mqtt.connection.is_connected and not mqtt.is_connected:
                    mqtt.on_connect()
                    if self.transport is not self.preferred_transport:
                        try:
                            self._switch_client(
                                self._clients_connected[self.preferred_transport]
                            )
                        except KeyError:
                            pass
            elif mqtt.connection.is_cloud:
                self.remove_client(mqtt)

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
            timestamp_future = timestamp + mlc.PARAM_TIMEZONE_CHECK_OK_PERIOD
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

    async def check_device_timerules(self, /):
        # when on local mqtt we have the responsibility for
        # setting the device timezone/dst transition times
        # but this is a process potentially consuming a lot
        # (checking future DST) so we'll be lazy on this by
        # scheduling not so often and depending on a bunch of
        # side conditions (like the device being time-aligned)
        delay = mlc.PARAM_TIMEZONE_CHECK_NOTOK_PERIOD

        if abs(self.device_timedelta) < PARAM_TIMESTAMP_TOLERANCE:
            with self.exception_warning("check_device_timerules"):
                if self._check_device_timerules():
                    # timezone trans not good..fix and check again soon
                    await self.async_config_device_timezone(self.descriptor.timezone)
                else:  # timezone trans good..check again in more time
                    delay = mlc.PARAM_TIMEZONE_CHECK_OK_PERIOD

        self._check_device_timerules_unsub = self.schedule_async_callback(
            delay, self.check_device_timerules
        )

    def check_device_timezone(self, /):
        """
        Verifies the device timezone has the same utc offset as HA local timezone.
        This is expecially sensible when the device has 'Consumption' or
        schedules (calendar entities) in order to align device local time to
        what is expected in HA.
        """
        ha_now = dt_util.now()
        device_now = ha_now.astimezone(self.tz)
        if ha_now.utcoffset() == device_now.utcoffset():
            self.remove_issue(mlc.ISSUE_DEVICE_TIMEZONE)
            return
        self.create_issue(
            mlc.ISSUE_DEVICE_TIMEZONE,
            severity=self.IssueSeverity.WARNING,
            translation_placeholders={"device_name": self.display_name},
        )

    async def async_config_device_timezone(self, tzname: str | None):
        if not self.mqtt or self.mqtt.connection.is_cloud:
            # TODO: This check is just a safety feature to not
            # pollute devices which might in general be Meross cloud account bound.
            # We need to finally fix the DST table lookup so that we provide a reliable api
            return False

        timerules: list[list[int]]
        if tzname:
            # we'll look through the list of transition times for current tz
            # and provide the actual (last past daylight) and the next to the
            # appliance so it knows how and when to offset utc to localtime

            # brutal patch for missing tz names (AEST #402)
            tzname = {"AEST": "Australia/Brisbane"}.get(tzname, tzname)

            try:
                tz = await self.api.async_load_zoneinfo(tzname)
            except Exception as e:
                self.log_exception(
                    self.WARNING,
                    e,
                    "loading timezone(%s) - check your python environment",
                    tzname,
                    timeout=14400,
                )
                return False

            timestamp = self.device_timestamp

            try:

                def _build_timerules():
                    try:
                        import pytz

                        tz_pytz = pytz.timezone(tzname)
                        if isinstance(tz_pytz, pytz.tzinfo.DstTzInfo):
                            timerules = []
                            # _utc_transition_times are naive UTC datetimes
                            idx = bisect_right(
                                tz_pytz._utc_transition_times,  # type: ignore
                                datetime_from_epoch(timestamp, None),
                            )
                            # idx would be the next transition offset index
                            _transition_info = tz_pytz._transition_info[idx - 1]  # type: ignore
                            timerules.append(
                                [
                                    int(tz_pytz._utc_transition_times[idx - 1].timestamp()),  # type: ignore
                                    int(_transition_info[0].total_seconds()),
                                    1 if _transition_info[1].total_seconds() else 0,
                                ]
                            )
                            # check the _transition_info has data beyond idx else
                            # the timezone has likely stopped 'transitioning'
                            if idx < len(tz_pytz._transition_info):  # type: ignore
                                _transition_info = tz_pytz._transition_info[idx]  # type: ignore
                                timerules.append(
                                    [
                                        int(tz_pytz._utc_transition_times[idx].timestamp()),  # type: ignore
                                        int(_transition_info[0].total_seconds()),
                                        1 if _transition_info[1].total_seconds() else 0,
                                    ]
                                )
                            return timerules
                        elif isinstance(tz_pytz, pytz.tzinfo.StaticTzInfo):
                            utcoffset = tz_pytz.utcoffset(None)
                            utcoffset = utcoffset.seconds if utcoffset else 0
                            return [[timestamp, utcoffset, 0]]

                    except Exception as exception:
                        self.log_exception(
                            self.WARNING,
                            exception,
                            "using pytz to build timezone(%s) ",
                            tzname,
                            timeout=14400,
                        )

                    # if pytz fails we'll fall-back to some euristics
                    device_datetime = datetime_from_epoch(timestamp, tz)
                    utcoffset = tz.utcoffset(device_datetime)
                    utcoffset = utcoffset.seconds if utcoffset else 0
                    return [[timestamp, utcoffset, 1 if tz.dst(device_datetime) else 0]]

                timerules = await self.api.hass.async_add_executor_job(_build_timerules)

            except Exception as exception:
                self.log_exception(
                    self.WARNING,
                    exception,
                    "building timezone(%s) info for %s",
                    tzname,
                    mn.Appliance_System_Time,
                )
                timerules = [
                    [0, 0, 0],
                    [timestamp + mlc.PARAM_TIMEZONE_CHECK_OK_PERIOD, 0, 1],
                ]

            p_time = {
                mc.KEY_TIMEZONE: tzname,
                mc.KEY_TIMERULE: timerules,
            }
        else:
            p_time = {
                mc.KEY_TIMEZONE: "",
                mc.KEY_TIMERULE: [],
            }

        await self.async_request(*mn.Appliance_System_Time.request_set(p_time))
        self.descriptor.update_time(p_time)
        self.schedule_entry_update(False)
        self.remove_issue(mlc.ISSUE_DEVICE_TIMEZONE)

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
        name = device_info.get(mc.KEY_DEVNAME) or self.descriptor.productname
        if name != self.device_entry.name:
            self.api.device_registry.async_update_device(
                self.device_entry.id, name=name
            )
        channel = -1
        async_update_entity = self.api.entity_registry.async_update_entity
        for device_info_channel in device_info.get("channels", []):
            # we assume the device_info.channels struct are mapped
            # to what we consider 'default' entities for the device
            # (i.e. MLGarage for garageDoor devices, MLToggle for
            # plain toggle devices, and so on).
            # also, the list looks like eventually containing empty dicts
            # for non-existent channel ids
            channel += 1
            try:
                if name := device_info_channel.get(mc.KEY_DEVNAME):
                    entity = self.entities[channel]
                    if (registry_entry := entity.registry_entry) and (
                        name != registry_entry.original_name
                    ):
                        async_update_entity(
                            registry_entry.entity_id, original_name=name
                        )
            except Exception:
                pass

        # check for firmware updates too
        if latest_version := profile.get_latest_version(*self.descriptor.type_subtype):
            self.update_latest_version(latest_version)

    async def _async_button_refresh_press(self):
        """Forces a full poll."""
        await self.async_poll_full()

    async def _async_button_reload_press(self):
        """Reload the config_entry."""
        self.schedule_reload()
