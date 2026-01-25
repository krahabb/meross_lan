import abc
import asyncio
import bisect
from datetime import UTC, tzinfo
from json import JSONDecodeError
from time import time
from typing import TYPE_CHECKING, override

import aiohttp
from homeassistant.config_entries import ConfigEntryState
from homeassistant.helpers import device_registry as dr
from homeassistant.util import dt as dt_util, slugify

# import core modules instead of symbols to ease patching in a single place
from . import datetime_from_epoch, manager as mlm
from .. import const as mlc
from ..button import MLPersistentButton

# only import those 'often used' symbols to get a tiny bit of speed improvement
from ..const import (
    CONF_HOST,
    CONF_PAYLOAD,
    CONF_PROTOCOL_AUTO,
    CONF_PROTOCOL_BLUETOOTH,
    CONF_PROTOCOL_HTTP,
    CONF_PROTOCOL_MQTT,
    PARAM_HEADER_SIZE,
    PARAM_HEARTBEAT_PERIOD,
    PARAM_TIMESTAMP_TOLERANCE,
)
from ..helpers.obfuscate import obfuscated_dict
from ..merossclient import get_active_broker, is_device_online
from ..merossclient.httpclient import MerossHttpClient, TerminatedException
from ..merossclient.protocol import MerossError
from ..merossclient.protocol.message import (
    MerossMessage,
    MerossRequest,
    MerossResponse,
    json_dumps,
)
from ..merossclient.protocol.namespaces import thermostat as mn_t
from ..sensor import ProtocolSensor
from ..update import MLUpdate
from .namespaces import NamespaceHandler, mc, mn

if TYPE_CHECKING:
    from asyncio import Future, Task, TimerHandle
    from types import CoroutineType
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

    from ..merossclient import MerossDeviceDescriptor
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
    from .entity import MLEntity
    from .meross_profile import DeviceInfoType, LatestVersionType
    from .mqtt_profile import MQTTConnection, MQTTProfile
    from .namespaces import NamespaceParser

    type DigestParseFunc = Callable[[JsonDict], None] | Callable[[JsonList], None]
    type DigestInitReturnType = tuple[DigestParseFunc, Iterable[NamespaceHandler]]
    type DigestInitFunc = Callable[[Device, Any], DigestInitReturnType]
    type NamespaceInitFunc = Callable[[Device, mn.Namespace], None]
    type AsyncRequestFunc = Callable[
        [str, str, MerossPayloadType], CoroutineType[Any, Any, MerossResponse]
    ]


class BaseDevice(mlm.EntityManager):
    """
    Abstract base class for Device and SubDevice (from hub)
    giving common behaviors like device_registry interface
    """

    if TYPE_CHECKING:
        DEVICE_TYPE: ClassVar[mlc.DeviceType]

        latest_version: LatestVersionType
        update_firmware: MLUpdate | None
        # Overrides
        device_entry_ids: Final[mlm.EntityManager.DeviceEntryIdType]  # type: ignore
        device_entry: Final[dr.DeviceEntry]  # type: ignore

        class Args(mlm.EntityManager.Args):
            device_entry: dr.DeviceEntry

    _attr_online = False

    """TODO
    __slots__ = (
        "latest_version",
        "update_firmware",
    )
    """

    def __init__(self, parent: mlm.EntityManager, id: str, **kwargs: "Unpack[Args]"):
        self.update_firmware = None
        super().__init__(parent, id, **kwargs)

    async def async_shutdown(self):
        await super().async_shutdown()
        del self.update_firmware

    # interface: self
    def update_latest_version(self, latest_version: "LatestVersionType"):
        # TODO: add the update invocation path for Hub Subdevices.
        self.latest_version = latest_version
        if self.update_firmware:
            self.update_firmware.update_info()
        else:
            self.update_firmware = MLUpdate(self)

    async def async_request(self, *args: "Unpack[MerossRequestType]") -> MerossResponse:
        raise NotImplementedError("async_request")

    @abc.abstractmethod
    def get_upgrade_payload(self, /) -> "mt_c.Upgrade":
        """Builds and returns the correct upgrade payload if an upgrade is available, otherwise returns None/empty dict."""
        raise NotImplementedError("get_upgrade_payload")

    @abc.abstractmethod
    def get_upgrade_info(self, /) -> tuple[str | None, ...]:
        """If an update is available returns a tuple of (installed_version, latest_version, release_summary)"""
        raise NotImplementedError("get_upgrade_info")

    @property
    @abc.abstractmethod
    def tz(self, /) -> tzinfo:
        raise NotImplementedError("tz")

    @property
    def ns_handlers(self, /) -> "Mapping[str, NamespaceHandler]":
        raise NotImplementedError("ns_handlers")


class Device(mlm.ConfigEntryManager, BaseDevice):
    """
    Generic protocol handler class managing the physical device stack/state
    """

    if TYPE_CHECKING:
        NAMESPACES: ClassVar[mn.NamespacesMapType]
        """ Accesses the namespaces definitions for this Device. This could be overriden
        when needed to extend with other namespaces (this is actually true for Hub). This
        way, when we're working only with standard devices we don't need to import the namespaces
        only relevant to hubs."""
        DIGEST_INIT: Final[dict[str, Any]]
        """ Static dict of 'digest initialization function(s)'.
        This is built on demand during Device init whenever a new digest key
        is encountered. This static dict in turn is used to setup the Device instance
        'digest_handlers' dict which contains a lookup to the digest parsing function when
        an NS_ALL message is received/parsed.
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

        descriptor: Final[MerossDeviceDescriptor]
        tz: tzinfo

        # these are set from ConfigEntry
        polling_period: int
        _polling_delay: int
        conf_protocol: str
        pref_protocol: str
        curr_protocol: str
        host: str | None

        device_timestamp: int

        _bluetooth: ComponentApi.BTDevice | None
        _bluetooth_active: ComponentApi.BTDevice | None
        _http: MerossHttpClient | None
        _http_active: MerossHttpClient | None
        _http_lastrequest: float
        _mqtt_connection: MQTTConnection | None
        _mqtt_connected: MQTTConnection | None
        _mqtt_publish: MQTTConnection | None
        _mqtt_active: MQTTConnection | None
        _mqtt_lastresponse: float
        _profile: MQTTProfile | None
        ns_handlers: Final[dict[str, NamespaceHandler]]
        digest_parsers: Final[dict[str, DigestParseFunc]]
        digest_pollers: Final[set[NamespaceHandler]]
        _lazypoll_requests: list[NamespaceHandler]
        _polling_epoch: float
        _polling_unsub: TimerHandle | None
        _polling_task: Task | None
        cloudpoll_requests: Final[int]  # externally read-only
        multiple_max: int
        _multiple_requests: list[NamespaceHandler]
        _multiple_response_size: int
        _timezone_next_check: float
        _trace_ability_callback_unsub: TimerHandle | None
        _diagnostics_build: bool

        # entities
        sensor_protocol: ProtocolSensor

        # Overrides
        config_entry: Final[ConfigEntry]  # type: ignore
        config: mlc.DeviceConfigType

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
    NAMESPACES = mn.NAMESPACES

    DIGEST_INIT = {
        mc.KEY_FAN: ".fan",
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
        mn.Appliance_Control_ConsumptionConfig: (
            ".helpers.namespaces",
            "VoidNamespaceHandler",
        ),
        mn.Appliance_Control_Electricity: (
            ".devices.mss",
            "namespace_init_electricity",
        ),
        mn.Appliance_Control_ElectricityX: (
            ".devices.mss",
            "ElectricityXSensor",
        ),
        mn.Appliance_Control_ConsumptionH: (
            ".devices.mss",
            "ConsumptionHSensor",
        ),
        mn.Appliance_Control_ConsumptionX: (".devices.mss", "ConsumptionXSensor"),
        mn.Appliance_Control_Fan: (".fan", "namespace_init_fan"),
        mn.Appliance_Control_FilterMaintenance: (
            ".sensor",
            "MLFilterMaintenanceSensor",
        ),
        mn.Appliance_Control_Mp3: (
            ".media_player",
            "MLMp3Player",
        ),
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
    }

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
        *(ns for ns in mn.NAMESPACES.values() if not ns.can_query),
    )

    DEFAULT_PLATFORMS = mlm.ConfigEntryManager.DEFAULT_PLATFORMS | {
        MLUpdate.PLATFORM: None,
    }

    __slots__ = (
        "descriptor",
        "tz",
        "polling_period",
        "_polling_delay",
        "conf_protocol",
        "pref_protocol",
        "curr_protocol",
        "host",
        "_async_entry_update_unsub",
        "device_debug",
        "device_timestamp",
        "device_timedelta",
        "device_timedelta_log_epoch",
        "device_timedelta_config_epoch",
        "device_response_size_min",
        "device_response_size_max",
        "lastrequest",
        "lastresponse",
        "_topic_response",  # sets the "from" field in request messages
        "_bluetooth",
        "_bluetooth_active",
        "_http",
        "_http_active",  # HTTP is 'online' i.e. reachable
        "_http_lastrequest",
        "_mqtt_connection",  # we're binded to an MQTT profile/broker
        "_mqtt_connected",  # the broker is online/connected
        "_mqtt_publish",  # the broker accepts 'publish' (cloud broker conf might disable publishing)
        "_mqtt_active",  # the broker receives valid traffic i.e. the device is 'mqtt' reachable
        "_mqtt_lastresponse",
        "_profile",
        "ns_handlers",
        "handler_all",
        "digest_parsers",
        "digest_pollers",
        "_lazypoll_requests",
        "_polling_epoch",
        "_polling_unsub",
        "_polling_task",
        "cloudpoll_requests",
        "multiple_max",
        "_multiple_requests",
        "_multiple_response_size",
        "_timezone_next_check",
        "_trace_ability_callback_unsub",
        "_diagnostics_build",
        "sensor_protocol",
        # Hub slots
        "subdevices",
    )

    def __init__(
        self,
        api: "ComponentApi",
        config_entry: "ConfigEntry",
        descriptor: "MerossDeviceDescriptor",
    ):
        self.descriptor = descriptor
        self.tz = UTC
        self._async_entry_update_unsub = None
        self.curr_protocol = CONF_PROTOCOL_AUTO
        self.device_debug = None
        self.device_timestamp = 0
        self.device_timedelta = 0
        self.device_timedelta_log_epoch = 0
        self.device_timedelta_config_epoch = 0
        self.device_response_size_min = 1000
        self.device_response_size_max = (
            descriptor.ability.get(mn.Appliance_Control_Multiple, {}).get(
                "maxCmdNum", 0
            )
            * 800
        )
        if self.device_response_size_max < self.device_response_size_min:
            self.device_response_size_max = self.device_response_size_min
        self.lastrequest = 0.0
        self.lastresponse = 0.0
        self._topic_response = mc.HEADER_FROM_DEFAULT
        self._bluetooth = None
        self._bluetooth_active = None
        self._http = None
        self._http_active = None
        self._http_lastrequest = 0
        self._mqtt_connection = None
        self._mqtt_connected = None
        self._mqtt_publish = None
        self._mqtt_active = None
        self._mqtt_lastresponse = 0
        self._profile = None
        self.ns_handlers = {}
        self.handler_all = NamespaceHandler(self, mn.Appliance_System_All)
        self.digest_parsers = {}
        self.digest_pollers = set()
        self._lazypoll_requests = []
        self._polling_epoch = 0.0
        self._polling_unsub = None
        self._polling_task = None
        self.cloudpoll_requests = 0
        self.multiple_max = 0
        self._multiple_requests = []
        self._multiple_response_size = PARAM_HEADER_SIZE
        self._timezone_next_check = (
            0
            if mn.Appliance_System_Time in descriptor.ability
            else mlc.PARAM_INFINITE_TIMEOUT
        )
        self._trace_ability_callback_unsub = None
        self._diagnostics_build = False

        uuid = config_entry.data[mlc.CONF_DEVICE_ID]
        super().__init__(
            api,
            uuid,
            config_entry,
            device_entry=api.device_registry.async_get_or_create(
                config_entry_id=config_entry.entry_id,
                connections={(dr.CONNECTION_NETWORK_MAC, descriptor.macAddress)},
                manufacturer=mc.MANUFACTURER,
                name=descriptor.productname,
                model=descriptor.productmodel,
                hw_version=descriptor.hardwareVersion,
                sw_version=descriptor.firmwareVersion,
                identifiers={(mlc.DOMAIN, uuid)},
            ),
        )

        self.sensor_protocol = ProtocolSensor(self)
        MLPersistentButton(
            self,
            None,
            "button_refresh",
            self._async_button_refresh_press,
            name="Refresh",
            device_class=MLPersistentButton.DeviceClass.RESTART,
            entity_category=MLPersistentButton.EntityCategory.DIAGNOSTIC,
        )
        MLPersistentButton(
            self,
            None,
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

        ability = descriptor.ability
        for ns, ns_init_func in Device.NAMESPACE_INIT.items():
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

    def start(self):
        # called by async_setup_entry after the entities have been registered
        # here we'll register mqtt listening (in case) and start polling after
        # the states have been eventually restored (some entities need this)
        self._check_protocol_ext()
        self._polling_unsub = self.schedule_callback(0, self._poll, None)

    # miscellaneous internals to prepare/refresh internal config
    async def _async_update_config(self):
        """
        common properties caches, read from ConfigEntry on __init__ or when a configentry updates
        """
        if self._polling_task:
            await self._polling_task

        config = self.config
        # map CONF_PROTOCOL value to a const symbol in order to use 'is' in Device code checks
        _protocols = [
            CONF_PROTOCOL_AUTO,
            CONF_PROTOCOL_BLUETOOTH,
            CONF_PROTOCOL_MQTT,
            CONF_PROTOCOL_HTTP,
        ]
        try:
            conf_protocol = _protocols[_protocols.index(config[mlc.CONF_PROTOCOL])]  # type: ignore
        except (KeyError, ValueError):
            conf_protocol = CONF_PROTOCOL_AUTO
        self.conf_protocol = conf_protocol
        self.polling_period = (
            config.get(mlc.CONF_POLLING_PERIOD) or mlc.CONF_POLLING_PERIOD_DEFAULT
        )
        if self.polling_period < mlc.CONF_POLLING_PERIOD_MIN:
            self.polling_period = mlc.CONF_POLLING_PERIOD_MIN
        self._polling_delay = self.polling_period

        self.enable_multiple(not config.get(mlc.CONF_DISABLE_MULTIPLE))

        self._update_host()

        if conf_protocol is CONF_PROTOCOL_BLUETOOTH:
            if not self._bluetooth:
                if _bluetooth := self.api.get_bt_device(self.id):
                    _bluetooth.attach(self)
        elif _bluetooth := self._bluetooth:
            _bluetooth.detach()
            self.api.device_registry.async_update_device(
                self.device_entry.id,
                new_connections={
                    (dr.CONNECTION_NETWORK_MAC, self.descriptor.macAddress)
                },
            )

    def _update_host(self):
        host = self.config.get(CONF_HOST)
        if not host:
            host = self.descriptor.innerIp
            if host == "0.0.0.0":  # unbinded device
                host = None

        self.host = host
        if host and (self.conf_protocol in (CONF_PROTOCOL_AUTO, CONF_PROTOCOL_HTTP)):
            # we need http: setup/update
            if self._http:
                self._http.host = host
                self._http.key = self.key
            else:
                self._http = MerossHttpClient(
                    host,
                    key=self.key,
                    from_=mlc.DOMAIN,
                    trigger_src=self.__class__.__name__,
                    loop=self.api.hass.loop,
                )

            if mn.Appliance_Encrypt_ECDHE in self.descriptor.ability:
                self._http.enable_encryption(
                    self.id, self.key, self.descriptor.macAddress
                )
            else:
                self._http.disable_encryption()

        elif self._http:
            self._http = self._http_active = None
            self.sensor_protocol.update_attr_inactive(ProtocolSensor.ATTR_HTTP)

    def _check_protocol_ext(self):
        api = self.api
        try:
            profile = api.profiles[self.descriptor.userId]
            if profile and (profile.key != self.key):
                profile = api
        except KeyError:
            profile = api
        if self._profile != profile:
            if self._profile:
                self._profile.unlink(self)
            if profile:
                profile.link(self)
                # _check_protocol already called
                return
        self._check_protocol()

    def _check_protocol(self):
        """called whenever the configuration or the profile linking changes to fix protocol transports"""
        _profile = self._profile
        conf_protocol = self.conf_protocol
        if conf_protocol is CONF_PROTOCOL_AUTO:
            # When using CONF_PROTOCOL_AUTO we try to use our 'preferred' (pref_protocol)
            # and eventually fallback (curr_protocol) until some good news allow us
            # to retry pref_protocol. When binded to a cloud_profile always prefer
            # 'local' http since it should be faster and less prone to cloud 'issues'
            if self.config.get(CONF_HOST) or (_profile and _profile.id):
                self.pref_protocol = CONF_PROTOCOL_HTTP
                if self.curr_protocol is not CONF_PROTOCOL_HTTP and self._http_active:
                    self._switch_protocol(CONF_PROTOCOL_HTTP)
            else:
                self.pref_protocol = CONF_PROTOCOL_MQTT
                if self.curr_protocol is not CONF_PROTOCOL_MQTT and self._mqtt_active:
                    self._switch_protocol(CONF_PROTOCOL_MQTT)
        else:
            self.pref_protocol = conf_protocol
            if self.curr_protocol is not conf_protocol:
                self._switch_protocol(conf_protocol)

        _mqtt_connection = self._mqtt_connection
        if conf_protocol in (CONF_PROTOCOL_BLUETOOTH, CONF_PROTOCOL_HTTP):
            if _mqtt_connection:
                _mqtt_connection.detach(self)
        else:
            if _mqtt_connection:
                if _mqtt_connection.profile == _profile:
                    return
                _mqtt_connection.detach(self)

            if _profile:
                _profile.attach_mqtt(self)

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

        await super().entry_update_listener(hass, config_entry)
        await self._async_update_config()
        self._check_protocol_ext()

        # config_entry update might come from DHCP or OptionsFlowHandler address update
        # so we'll eventually retry querying the device
        if not self.online:
            self.schedule_poll("entry_update_listener")

    async def async_create_diagnostic_entities(self):
        self._diagnostics_build = True  # set a flag cause we'll lazy scan/build
        await super().async_create_diagnostic_entities()

    async def async_destroy_diagnostic_entities(self, remove: bool = False):
        self._diagnostics_build = False
        for namespace_handler in self.ns_handlers.values():
            if (
                namespace_handler.polling_strategy
                is NamespaceHandler.async_poll_diagnostic
            ):
                namespace_handler.polling_strategy = None
        await super().async_destroy_diagnostic_entities(remove)

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

    async def _async_trace_ability(self, abilities_iterator: "Iterator[str]"):
        self._trace_ability_callback_unsub = None
        try:
            # avoid interleave tracing ability with polling loop
            # also, since we could trigger this at early stages
            # in device init, this check will prevent iterating
            # at least until the device fully initialize through
            # self.start()
            if self.online and not self._polling_task:
                while (
                    ability := next(abilities_iterator)
                ) in self.TRACE_ABILITY_EXCLUDE:
                    continue
                self.log(self.DEBUG, "Tracing %s ability", ability)
                await self.get_handler_by_name(ability).async_trace(self.async_request)
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

        if (self.curr_protocol is CONF_PROTOCOL_MQTT) and self._mqtt_publish:
            timeout = (
                mlc.PARAM_TRACING_ABILITY_POLL_TIMEOUT
                + self._mqtt_publish.get_rl_safe_delay(self.id)
            )
        else:
            timeout = mlc.PARAM_TRACING_ABILITY_POLL_TIMEOUT
        self._trace_ability_callback_unsub = self.schedule_async_callback(
            timeout,
            self._async_trace_ability,
            abilities_iterator,
        )

    def _trace_or_log(
        self, epoch: float, message: MerossMessage, protocol: str, rxtx: str
    ):
        if self.is_tracing:
            self.trace(
                epoch,
                message.payload,
                message.namespace,
                message.method,
                protocol,
                rxtx,
            )
        # here we avoid using self.log since it would
        # log to the trace file too but we've already 'traced' the
        # message if that's the case
        logger = self.logger
        if logger.isEnabledFor(self.VERBOSE):
            logger._log(
                self.VERBOSE,
                "%s(%s) %s %s (messageId:%s) %s",
                (
                    rxtx,
                    protocol,
                    message.method,
                    message.namespace,
                    message.messageid,
                    json_dumps(self.loggable_dict(message)),
                ),
            )
        elif logger.isEnabledFor(self.DEBUG):
            logger._log(
                self.DEBUG,
                "%s(%s) %s %s (messageId:%s)",
                (
                    rxtx,
                    protocol,
                    message.method,
                    message.namespace,
                    message.messageid,
                ),
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

        if self._http_active:
            # shortcut with fast HTTP querying
            self._trace_data = trace_data = [mlc.CONF_TRACE_COLUMNS]
            await self.async_poll_full()
            try:
                abilities = iter(self.descriptor.ability)
                while self.online and self.is_tracing:
                    ability = next(abilities)
                    if ability not in self.TRACE_ABILITY_EXCLUDE:
                        await self.get_handler_by_name(ability).async_trace(
                            self.async_http_request
                        )
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
        self._trace_future = future = self.api.hass.loop.create_future()
        await self.async_trace_open()
        return await future

    @override
    def loggable_diagnostic_state(self):
        """Return a 'loggable' version of the entry state (for diagnostic/logging purposes)"""
        profile = self._profile
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
            "pref_protocol": self.pref_protocol,
            "curr_protocol": self.curr_protocol,
            "polling_period": self.polling_period,
            "device_response_size_min": self.device_response_size_min,
            "device_response_size_max": self.device_response_size_max,
            "BLUETOOTH": {
                "bluetooth": bool(self._bluetooth),
                "bluetooth_active": bool(self._bluetooth_active),
            },
            "MQTT": {
                "cloud_profile": (profile.is_cloud_profile if profile else None),
                "locally_active": bool(self.mqtt_locallyactive),
                "mqtt_connection": bool(self._mqtt_connection),
                "mqtt_connected": bool(self._mqtt_connected),
                "mqtt_publish": bool(self._mqtt_publish),
                "mqtt_active": bool(self._mqtt_active),
            },
            "HTTP": {
                "http": bool(self._http),
                "http_active": bool(self._http_active),
            },
            "namespace_handlers": {
                handler.ns: {
                    "lastrequest": handler.lastrequest,
                    "lastresponse": handler.lastresponse,
                    "lastpush": (
                        obfuscated_dict(handler.lastpush)
                        if (handler.lastpush and self.obfuscate)
                        else handler.lastpush
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
                obfuscated_dict(device_info)
                if self.obfuscate and device_info
                else device_info
            ),
            "latest_version": latest_version,
            "latest_versions": latest_versions,
        }

    async def async_get_diagnostics(self):
        if self.online:
            data = await super().async_get_diagnostics()
            data["trace"] = await self._async_get_diagnostics_trace()
            return data
        else:
            return await super().async_get_diagnostics()

    # interface: BaseDevice
    async def async_shutdown(self):
        self.remove_issue(mlc.ISSUE_DEVICE_TIMEZONE)
        if self._async_entry_update_unsub:
            self._async_entry_update_unsub.cancel()
            self._async_entry_update_unsub = None
        # disconnect transports first so that any pending request
        # is invalidated and this shortens the eventual polling loop
        if self._profile:
            self._profile.unlink(self)
        if self._bluetooth:
            self._bluetooth.detach()
        await self.async_poll_stop()
        await super().async_shutdown()
        for handler in self.ns_handlers.values():
            handler.shutdown()
        del self.ns_handlers  # type: ignore
        del self.digest_parsers  # type: ignore
        del self.digest_pollers  # type: ignore
        del self._lazypoll_requests
        del self.sensor_protocol
        self.api.devices[self.id] = None
        self.log(  # REMOVE
            self.DEBUG, "Device.async_shutdown complete (object: %s)", self.objects
        )

    async def async_request_raw(self, request: MerossRequest) -> MerossResponse:
        """
        route the request through MQTT or HTTP to the physical device.
        callback will be called on successful replies and actually implemented
        only when HTTPing SET requests. On MQTT we rely on async PUSH and SETACK to manage
        confirmation/status updates
        TODO: remove this. This is a 'legacy' api superseeded by async_request to better manage message
        signature. It is left for meross_lan.request service implementation but should be removed
        since very 'fragile'
        """
        self.lastrequest = time()
        mqttfailed = False
        if self.curr_protocol is CONF_PROTOCOL_MQTT:
            if self._mqtt_publish:
                try:
                    return await self.async_mqtt_request_raw(request)
                except Exception:
                    if self.conf_protocol is CONF_PROTOCOL_MQTT:
                        raise
                    mqttfailed = True

            # MQTT not connected or not allowing publishing
            if self.conf_protocol is CONF_PROTOCOL_MQTT:
                raise MerossError("No MQTT transport available to send the request")

        # curr_protocol is HTTP
        try:
            return await self.async_http_request_raw(request)
        except Exception:
            if (
                self._mqtt_active  # device is connected to broker
                and self._mqtt_publish  # profile allows publishing
                and not mqttfailed  # we've already tried mqtt
            ):
                return await self.async_mqtt_request_raw(request)
            raise

    @override
    async def async_request(self, *args: "Unpack[MerossRequestType]") -> MerossResponse:
        """
        route the request through MQTT or HTTP to the physical device according to
        current protocol. When switching transport the message is recomputed to
        avoid reusing the same (old) timestamps and messageids.
        """
        self.lastrequest = time()
        if self._bluetooth:
            # BL should be active alone when the device is unbound so no other transport is available
            return await self.async_bluetooth_request(*args)

        mqttfailed = False
        if self.curr_protocol is CONF_PROTOCOL_MQTT:
            if self._mqtt_publish:
                try:
                    return await self.async_mqtt_request_raw(
                        MerossRequest(
                            *args,
                            self.key,
                            self._topic_response,
                            self.__class__.__name__,
                            self.id,
                        )
                    )
                except Exception:
                    if self.conf_protocol is CONF_PROTOCOL_MQTT:
                        raise
                    mqttfailed = True

            # MQTT not connected or not allowing publishing
            if self.conf_protocol is CONF_PROTOCOL_MQTT:
                raise MerossError("No MQTT transport available to send the request")

        try:
            return await self.async_http_request_raw(
                MerossRequest(
                    *args,
                    self.key,
                    self._topic_response,
                    self.__class__.__name__,
                )
            )
        except Exception:
            if (
                self._mqtt_active  # device is connected to broker
                and self._mqtt_publish  # profile allows publishing
                and not mqttfailed  # we've already tried mqtt
            ):
                return await self.async_mqtt_request_raw(
                    MerossRequest(
                        *args,
                        self.key,
                        self._topic_response,
                        self.__class__.__name__,
                        self.id,
                    )
                )
            raise

    @override
    def _set_offline(self, /):
        super()._set_offline()
        self._polling_delay = self.polling_period
        self._bluetooth_active = self._http_active = self._mqtt_active = None
        self.device_debug = None
        for handler in self.ns_handlers.values():
            handler.polling_epoch_next = 0.0

    @override
    def get_upgrade_payload(self, /) -> "mt_c.Upgrade":
        return self.descriptor.build_upgrade_payload(self.latest_version)

    @override
    def get_upgrade_info(self, /):
        # assert self.latest_version
        latest_version = self.latest_version
        try:
            descriptor = self.descriptor
            upgrade_payload = descriptor.build_upgrade_payload(latest_version)
            if upgrade_payload and mc.KEY_MCU in upgrade_payload:
                assert descriptor.mcu
                return (
                    descriptor.mcu[mc.KEY_VERSION],
                    latest_version[mc.KEY_MCU][0][mc.KEY_VERSION],
                    latest_version.get(mc.KEY_DESCRIPTION),
                )
            else:
                return (
                    descriptor.firmwareVersion,
                    latest_version[mc.KEY_VERSION],
                    latest_version.get(mc.KEY_DESCRIPTION),
                )
        except Exception as e:
            self.log_exception(
                self.WARNING,
                e,
                "get_upgrade_info (latest_version:%s mcu:%s)",
                str(latest_version),
                str(descriptor.mcu),
            )
            return None, None, None

    # interface: self
    @property
    def mqtt_cloudactive(self):
        """
        Reports if the device is actively paired to a Meross MQTT broker
        """
        return self._mqtt_active and self._mqtt_active.is_cloud_connection

    @property
    def mqtt_locallyactive(self):
        """
        Reports if the device is actively paired to a private (non-meross) MQTT
        in order to decide if we can/should send over a local MQTT with good
        chances of success.
        we should also check if the _mqtt_connection is 'publishable' but
        at the moment the ComponentApi MQTTConnection doesn't allow disabling it
        """
        return self._mqtt_active and not self._mqtt_active.is_cloud_connection

    @property
    def meross_binded(self):
        """
        Reports if the device own MQTT connection is active and likely Meross
        account binded.
        """
        if self._mqtt_active:
            return self._mqtt_active.is_cloud_connection
        # if we're not connected (either reason) check the internal
        # device state connection
        descriptor = self.descriptor
        if not is_device_online(descriptor.system):
            return False
        # the device is connected to its own broker..assume
        # it is a Meross cloud one
        return True

    def get_device_datetime(self, epoch, /):
        """
        given the epoch (utc timestamp) returns the datetime
        in device local timezone
        """
        return datetime_from_epoch(epoch, self.tz)

    def get_handler(self, ns: "mn.Namespace", /):
        try:
            return self.ns_handlers[ns]
        except KeyError:
            return self._create_handler(ns)

    def get_handler_by_name(self, namespace: str, /):
        try:
            return self.ns_handlers[namespace]
        except KeyError:
            return self._create_handler(self.NAMESPACES[namespace])

    def register_parser(self, parser: "NamespaceParser", ns: "mn.Namespace", /):
        self.get_handler(ns).register_parser(parser)

    def register_parser_entity(self, entity: "MLEntity", /):
        self.get_handler(entity.ns).register_parser(entity)

    def register_parser_ex(
        self,
        parser: "NamespaceParser",
        *nss: "mn.Namespace",
    ):
        """Register a parser for multiple namespaces. Abilities are checked for namespaces availability."""
        ability = self.descriptor.ability
        for ns in (_ns for _ns in nss if _ns in ability):
            self.get_handler(ns).register_parser(parser)

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
            self.api.hass.config_entries.async_update_entry(
                self.config_entry, data=data
            )

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

    async def async_unbind(self):
        """
        WARNING!!!
        Hardware reset to factory default: the device will unpair itself from
        the (cloud) broker and then reboot, ready to be initialized/paired
        """
        # in case we're connected to a cloud broker we'll use that since
        # it appears the broker session level will take care of also removing
        # the device from its list, thus totally cancelling it from the Meross account
        if self._mqtt_publish and self._mqtt_publish.is_cloud_connection:
            return await self.async_mqtt_request(
                *mn.Appliance_Control_Unbind.request_default
            )
        # else go with whatever transport: the device will reset it's configuration
        return await self.async_request(*mn.Appliance_Control_Unbind.request_default)

    def enable_multiple(self, enable: bool, /):
        self.multiple_max = (
            self.descriptor.ability.get(mn.Appliance_Control_Multiple, {}).get(
                "maxCmdNum", 0
            )
            if enable
            else 0
        )
        self._multiple_requests.clear()
        self._multiple_response_size = PARAM_HEADER_SIZE

    async def async_request_multiple(
        self, requests: "Iterable[MerossRequestType]", auto_handle: bool = True
    ) -> MerossResponse:
        """Send requests in a single NS_APPLIANCE_CONTROL_MULTIPLE message.
        If the whole request is succesful (might be partial if the device response
        overflown somehow (see JSON patching in HTTP request api)
        returns the unpacked reponses in a list.
        auto_handle will instruct this api to forward the responses to the
        namespace handling before returning.
        Contrary to async_multiple_requests_flush this doesn't recover from
        partial message responses so it doesn't resend missed requests/responses
        """
        response = await self.async_request(
            mn.Appliance_Control_Multiple,
            mc.METHOD_SET,
            {
                mn.Appliance_Control_Multiple.key: [
                    {
                        mc.KEY_HEADER: {
                            mc.KEY_MESSAGEID: MerossRequest.generate_id(),
                            mc.KEY_METHOD: request[1],
                            mc.KEY_NAMESPACE: request[0],
                        },
                        mc.KEY_PAYLOAD: request[2],
                    }
                    for request in requests
                ]
            },
        )
        if auto_handle:
            for message in response.payload[mc.KEY_MULTIPLE]:
                self._handle(MerossMessage(message))

        return response

    async def async_bluetooth_request(
        self, *request_args: "Unpack[MerossRequestType]"
    ) -> MerossResponse:
        request = MerossRequest(*request_args, "", mlc.DOMAIN, self.__class__.__name__)
        self._trace_or_log(time(), request, CONF_PROTOCOL_BLUETOOTH, Device.TRACE_TX)
        try:
            response = await self._bluetooth.async_request_raw(request)  # type: ignore
            epoch = time()
            self._trace_or_log(epoch, response, CONF_PROTOCOL_BLUETOOTH, self.TRACE_RX)
            if self.curr_protocol is not CONF_PROTOCOL_BLUETOOTH:
                self._switch_protocol(CONF_PROTOCOL_BLUETOOTH)
            self._receive(epoch, response)
            return response
        except Exception as e:
            if self._bluetooth:
                self.log_exception(self.WARNING, e, "async_bluetooth_request")
                await self._bluetooth.disconnect()
            else:
                self.log(
                    self.DEBUG,
                    "Attempting to use async_bluetooth_request with no bluetooth client",
                )
            raise

    async def async_mqtt_request_raw(self, request: MerossRequest, /) -> MerossResponse:
        self._trace_or_log(time(), request, CONF_PROTOCOL_MQTT, Device.TRACE_TX)
        try:
            assert self._mqtt_publish
            if self._mqtt_publish.is_cloud_connection:
                self.cloudpoll_requests += 1  # type: ignore
            response = await self._mqtt_publish.async_mqtt_request(request)
            self._mqtt_lastresponse = epoch = time()
            self._trace_or_log(epoch, response, CONF_PROTOCOL_MQTT, self.TRACE_RX)
            if not self._mqtt_active:
                self._mqtt_active = self._mqtt_connected
                if self.online:
                    self.sensor_protocol.update_attr_active(ProtocolSensor.ATTR_MQTT)
            if self.curr_protocol is not CONF_PROTOCOL_MQTT:
                if (self.pref_protocol is CONF_PROTOCOL_MQTT) or (
                    not self._http_active
                ):
                    self._switch_protocol(CONF_PROTOCOL_MQTT)
            self._receive(epoch, response)
            return response
        except Exception as e:
            if self._mqtt_publish:
                self.log_exception(self.WARNING, e, "async_mqtt_request_raw")
            else:
                self.log(
                    self.DEBUG,
                    "Attempting to use async_mqtt_request_raw with no mqtt connection",
                )
            raise

    async def async_mqtt_request(
        self, namespace: str, method: str, payload: "MerossPayloadType", /
    ):
        return await self.async_mqtt_request_raw(
            MerossRequest(
                namespace,
                method,
                payload,
                self.key,
                self._topic_response,
                self.__class__.__name__,
                self.id,
            )
        )

    def mqtt_request(
        self, namespace: str, method: str, payload: "MerossPayloadType", /
    ):
        # TODO: silence exception in task coroutine?
        return self.async_create_task(
            self.async_mqtt_request(namespace, method, payload),
            f".mqtt_request({namespace},{method},{type(payload)})",
        )

    async def async_http_request_raw(self, request: MerossRequest, /) -> MerossResponse:
        self._http_lastrequest = time()
        self._trace_or_log(
            self._http_lastrequest, request, CONF_PROTOCOL_HTTP, self.TRACE_TX
        )
        try:
            assert self._http
            response = await self._http.async_request_raw(request)
        except AssertionError:
            self.log(
                self.DEBUG,
                "Attempting to use async_http_request_raw with no http connection (AssertionError)",
            )
            raise
        except TerminatedException:
            raise
        except JSONDecodeError as jsonerror:
            # this could happen when the response carries a truncated payload
            # and might be due to an 'hard' limit in the capacity of the
            # device http output buffer (when the response is too long)
            self.log(
                self.DEBUG,
                "HTTP ERROR %s %s (messageId:%s JSONDecodeError:%s)",
                request.method,
                request.namespace,
                request.messageid,
                str(jsonerror),
            )
            response_text = jsonerror.doc
            response_text_len_safe = int(len(response_text) * 0.9)
            if jsonerror.pos < response_text_len_safe:
                # if the error is too early in the payload...
                raise
            # the error happened because of truncated json payload
            self.device_response_size_max = response_text_len_safe
            if self.device_response_size_min > response_text_len_safe:
                self.device_response_size_min = response_text_len_safe
            self.log(
                self.DEBUG,
                "Updating device_response_size_min:%d device_response_size_max:%d",
                self.device_response_size_min,
                self.device_response_size_max,
            )
            # try to recover the message by truncating the payload
            # with various heuristics
            namespace = request.namespace
            if not type(namespace) is mn.Namespace:
                namespace = self.NAMESPACES[namespace]
            match namespace:
                case mn.Appliance_Control_Multiple:
                    list_break_matcher = '},{"header":'
                case _:
                    if not namespace.key_channel:
                        raise
                    list_break_matcher = f'}},{{"{namespace.key_channel}":'

            trunc_pos = response_text.rfind(list_break_matcher)
            if trunc_pos == -1:
                raise
            response_text = response_text[0:trunc_pos] + "}]}}"
            response = MerossResponse(response_text)
        except Exception as exception:
            self.log(
                self.DEBUG,
                "HTTP ERROR %s %s (messageId:%s %s:%s)",
                request.method,
                request.namespace,
                request.messageid,
                exception.__class__.__name__,
                str(exception),
            )
            if not self.online:
                raise
            if request.namespace is mn.Appliance_System_All:
                if self._http_active:
                    self._http_active = None
                    self.sensor_protocol.update_attr_inactive(ProtocolSensor.ATTR_HTTP)
            elif request.namespace is mn.Appliance_Control_Unbind:
                if isinstance(exception, aiohttp.ServerDisconnectedError):
                    # this is expected when issuing the UNBIND
                    # so this is an indication we're dead
                    self._set_offline()
            raise

        epoch = time()
        self._trace_or_log(epoch, response, CONF_PROTOCOL_HTTP, self.TRACE_RX)
        # add a sanity check here since we have some issues (#341)
        # that might be related to misconfigured devices where the
        # host address points to a different device than configured.
        # Our current device.id in fact points (or should) to the uuid discovered
        # in configuration but if by chance the device changes ip and we miss
        # the dynamic change (eitehr dhcp not working or HA down while dhcp updating)
        # we might end up with our configured host pointing to a different device
        # and this might (unluckily) be another Meross with the same key
        # so it could rightly respond here. This shouldnt happen over MQTT
        # since the device.id is being taken care of by the routing mechanism
        if self.id != response.uuid:
            try:
                assert self._http
                mismatched_payload_all = await self._http.async_request(
                    *mn.Appliance_System_All.request_default
                )
            except Exception:
                mismatched_payload_all = None
            self._process_uuid_mismatch(response.uuid, mismatched_payload_all)
            raise MerossError(
                f"Device UUID mismatch over HTTP (expected:{self.id} got:{response.uuid})"
            )

        if not self._http_active and self._http:
            self._http_active = self._http
            self.sensor_protocol.update_attr_active(ProtocolSensor.ATTR_HTTP)
        if self.curr_protocol is not CONF_PROTOCOL_HTTP:
            if (self.pref_protocol is CONF_PROTOCOL_HTTP) or (not self._mqtt_active):
                self._switch_protocol(CONF_PROTOCOL_HTTP)
        self._receive(epoch, response)
        return response

    async def async_http_request(
        self, namespace: str, method: str, payload: "MerossPayloadType", /
    ):
        return await self.async_http_request_raw(
            MerossRequest(
                namespace,
                method,
                payload,
                self.key,
                self._topic_response,
                self.__class__.__name__,
            )
        )

    @property
    def polling_response_size_available(self):
        """Returns the expected maximum allowed request response size in the current
        multiple request poll. If multiple polling is disabled this works too."""
        return (
            self.device_response_size_max - self._multiple_response_size
            if self.multiple_max
            else self.device_response_size_max
        )

    async def _async_poll_multiple_flush(self):
        multiple_requests = self._multiple_requests
        multiple_response_size = self._multiple_response_size
        self._multiple_requests = []
        self._multiple_response_size = PARAM_HEADER_SIZE

        requests_len = len(multiple_requests)
        while self.online and requests_len:
            lazypoll_requests = self._lazypoll_requests
            while (requests_len < self.multiple_max) and lazypoll_requests:
                # we have space available in current ns_multiple and lazy pollers are waiting
                for handler in lazypoll_requests:
                    # lazy pollers are ordered by 'oldest polled first' so
                    # the first is the one which hasn't been polled since longer
                    # we then decide to add to the current ns_multiple the first that would fit in
                    if (
                        handler.polling_response_size + multiple_response_size
                    ) < self.device_response_size_max:
                        handler.lastrequest = self._polling_epoch
                        handler.polling_epoch_next = (
                            handler.lastrequest + handler.polling_period
                        )
                        multiple_requests.append(handler)
                        lazypoll_requests.remove(handler)
                        multiple_response_size += handler.polling_response_size
                        requests_len += 1
                        # check if we can add more
                        break  # for
                else:
                    # no lazy_poller could match..break out of while
                    break  # while

            if requests_len == 1:
                await multiple_requests[0].async_get_safe()
                return

            try:
                response = await self.async_request_multiple(
                    (handler.polling_request for handler in multiple_requests),
                    auto_handle=False,
                )
            except Exception as e:
                # the ns_multiple failed but the reason could be the device
                # did overflow somehow. I've seen 2 kind of errors so far on the
                # HTTP client: typically the device returns an incomplete json
                # and this is partly recovered in our http interface. One(old)
                # bulb (msl120) instead completely disconnects (ServerDisconnectedException
                # in http client) and so we get here with no response. The same
                # msl bulb timeouts completely on MQTT, so the response to our mqtt requests
                # is None again. At this point, if the device is still online we're
                # trying a last resort issue of single requests
                if self.online:
                    self.log(
                        self.DEBUG,
                        "Appliance.Control.Multiple failed with '%s' (requests=%d expected size=%d)",
                        str(e) or e.__class__.__name__,
                        requests_len,
                        multiple_response_size,
                    )
                    # Here we reduce the device_response_size_max so that
                    # next ns_multiple will be less demanding. device_response_size_min
                    # is another dynamic param representing the biggest payload ever received
                    self.device_response_size_max = (
                        self.device_response_size_max + self.device_response_size_min
                    ) / 2
                    self.log(
                        self.DEBUG,
                        "Updating device_response_size_max:%d",
                        self.device_response_size_max,
                    )
                    for handler in multiple_requests:
                        if not self.online:  # TODO: remove these online checks
                            break
                        await handler.async_get_safe()
                return

            multiple_responses = response[mc.KEY_PAYLOAD][mc.KEY_MULTIPLE]
            if not multiple_responses:
                # no response at all..this is pathological but we have
                # examples (#526) of this so we'll just try issue single requests
                self.log(
                    self.WARNING,
                    "Appliance.Control.Multiple empty response (requests=%d expected size=%d)",
                    requests_len,
                    multiple_response_size,
                    timeout=14400,
                )
                for handler in multiple_requests:
                    if not self.online:
                        break
                    await handler.async_get_safe()
                return

            responses_len = len(multiple_responses)
            if self.isEnabledFor(self.DEBUG):
                self.log(
                    self.DEBUG,
                    "Appliance.Control.Multiple requests=%d (responses=%d) expected size=%d (actual=%d)",
                    requests_len,
                    responses_len,
                    multiple_response_size,
                    len(response.json),
                )

            message: "MerossMessageType"
            for message in multiple_responses:
                _response = MerossMessage(message)
                for handler in multiple_requests:
                    if handler.ns != _response.namespace:
                        continue
                    multiple_requests.remove(handler)
                    handler.handle_response(_response)
                    break
                else:
                    # not found..something is wrong!! TODO: log a DEBUG/WARNING here?
                    pass

            # and re-issue the missing ones
            requests_len = len(multiple_requests)
            multiple_response_size = -1  # logging purpose

    async def async_request_poll(self, handler: NamespaceHandler, /):
        handler.lastrequest = self._polling_epoch
        handler.polling_epoch_next = handler.lastrequest + handler.polling_period
        if (not self.multiple_max) or (
            handler.polling_response_size >= self.device_response_size_max
        ):
            # multiple requests are disabled
            # or this request alone would overflow the device response size limit
            await handler.async_get_safe()
            return
        # estimate the size of the multiple response
        multiple_response_size = (
            self._multiple_response_size + handler.polling_response_size
        )
        if multiple_response_size >= self.device_response_size_max:
            # this request (together with already previously packed)
            # would overflow the device response size limit
            if not self._multiple_requests:
                # again this request alone would overflow the device response size limit
                await handler.async_get_safe()
                return
            # flush the pending multiple requests
            await self._async_poll_multiple_flush()
            multiple_response_size = (
                self._multiple_response_size + handler.polling_response_size
            )
        self._multiple_requests.append(handler)
        self._multiple_response_size = multiple_response_size
        if len(self._multiple_requests) >= self.multiple_max:
            await self._async_poll_multiple_flush()

    async def async_request_smartpoll(
        self,
        handler: NamespaceHandler,
        *,
        cloud_queue_max: int = 1,
    ):
        if (
            (self.curr_protocol is CONF_PROTOCOL_MQTT)
            and (self.cloudpoll_requests >= cloud_queue_max)
            and (
                (self._polling_epoch - handler.lastrequest)
                < handler.polling_period_cloud
            )
        ):
            # the request would go over cloud mqtt but we've already queued some
            # and we could wait up to handler.polling_period_cloud
            return False
        await self.async_request_poll(handler)
        return True

    def _poll(self, namespace: str | None = None):
        self._polling_unsub = None
        self._polling_task = task = self.async_create_task(
            self._async_poll(namespace),
            f"._async_poll({namespace})",
            False,
        )
        return task

    async def _async_poll(self, namespace: str | None):
        self._polling_epoch = epoch = time()
        self.log(self.DEBUG, "Polling begin")
        try:
            # We're 'strictly' online when the device 'was' online and last request
            # got succesfully replied.
            # When last request(s) somewhat failed we'll probe NS_ALL before stating it is really
            # unreachable. This kind of probing is the same done when the device is (definitely)
            # offline.
            if self.online and (
                (self.lastresponse > self.lastrequest)
                or ((epoch - self.lastrequest) < (self.polling_period - 2))
            ):
                # when mqtt is working as a fallback for HTTP
                # we should periodically check if http comes back
                # in case our self.pref_protocol is HTTP.
                # when self.pref_protocol is MQTT we don't care
                # since we'll just try the switch when mqtt fails
                if (
                    (self.curr_protocol is CONF_PROTOCOL_MQTT)
                    and (self.pref_protocol is CONF_PROTOCOL_HTTP)
                    and ((epoch - self._http_lastrequest) > PARAM_HEARTBEAT_PERIOD)
                ):
                    try:
                        self.handler_all.handle_response(
                            await self.async_http_request(
                                *self.handler_all.polling_request
                            )
                        )
                        namespace = self.handler_all.ns
                        # going on, should the http come online, the next
                        # poll cycle will be 'smart' again, skipping
                        # state updates coming through mqtt (since we're still
                        # connected) but now requesting over http as preferred.
                        # Also, we're forcibly passing namespace = NS_ALL to
                        # tell the self._async_request_updates we've already polled that
                    except Exception:
                        pass

                if self.mqtt_locallyactive:
                    # implement an heartbeat since mqtt might
                    # be unused for quite a bit
                    if (epoch - self._mqtt_lastresponse) > PARAM_HEARTBEAT_PERIOD:
                        try:
                            self.handler_all.handle_response(
                                await self.async_mqtt_request(
                                    *self.handler_all.polling_request
                                )
                            )
                            namespace = self.handler_all.ns
                        except Exception:
                            self._mqtt_active = None
                            self.device_debug = None
                            self.sensor_protocol.update_attr_inactive(
                                ProtocolSensor.ATTR_MQTT
                            )
                        # going on could eventually try/switch to HTTP
                    elif epoch > self._timezone_next_check:
                        # when on local mqtt we have the responsibility for
                        # setting the device timezone/dst transition times
                        # but this is a process potentially consuming a lot
                        # (checking future DST) so we'll be lazy on this by
                        # scheduling not so often and depending on a bunch of
                        # side conditions (like the device being time-aligned)
                        self._timezone_next_check = (
                            epoch + mlc.PARAM_TIMEZONE_CHECK_NOTOK_PERIOD
                        )
                        if abs(self.device_timedelta) < PARAM_TIMESTAMP_TOLERANCE:
                            with self.exception_warning("_check_device_timerules"):
                                if self._check_device_timerules():
                                    # timezone trans not good..fix and check again soon
                                    await self.async_config_device_timezone(
                                        self.descriptor.timezone
                                    )
                                else:  # timezone trans good..check again in more time
                                    self._timezone_next_check = (
                                        epoch + mlc.PARAM_TIMEZONE_CHECK_OK_PERIOD
                                    )

            else:  # offline or 'likely' offline (failed last request)
                handler_all = self.handler_all
                coro_func: list["AsyncRequestFunc"] = []
                if self._http:
                    coro_func.append(self.async_http_request)
                if self._mqtt_publish:
                    coro_func.append(self.async_mqtt_request)
                if self._bluetooth:
                    coro_func.append(self.async_bluetooth_request)

                if len(coro_func) > 1:
                    tasks = {
                        self.async_create_task(
                            coro(*handler_all.polling_request),
                            f".async_poll_{coro.__name__}_task",
                        )
                        for coro in coro_func
                    }
                    # use pre 3.13 compatible syntax/semantics
                    for earliest_connect in asyncio.as_completed(tasks, timeout=5):
                        try:
                            ns_all_response = await earliest_connect
                            # TODO:
                            # we should leave'em so that transports can come online later
                            # this code instead cancels the 'losers' immediately
                            # since it actually doesn't work with tests (mqtt publish mocking is missing)
                            # for task in tasks:
                            #    task.cancel()
                            break
                        except Exception:
                            ns_all_response = None
                    else:  # shouldnt be needed: just silences type-checker
                        ns_all_response = None
                elif coro_func:
                    try:
                        ns_all_response = await coro_func[0](
                            *handler_all.polling_request
                        )
                    except Exception:
                        ns_all_response = None
                else:
                    raise asyncio.TimeoutError("No transport available for polling")

                if not ns_all_response:
                    raise asyncio.TimeoutError("No response for NS_ALL polling")

                handler_all.handle_response(ns_all_response)
                handler_all.polling_response_size = len(ns_all_response.json)
                namespace = handler_all.ns

            """
            When 'namespace' is not 'None' it represents the device coming online
            following a succesful received message. This is likely to be 'NS_ALL'.
            If we're connected to an MQTT broker anyway it could be any 'PUSH' message.
            We'll use _queued_smartpoll_requests to track how many polls went through
            over MQTT for this cycle in order to only send 1 for each if we're
            binded to a cloud MQTT broker (in order to reduce bursts).
            If a poll request is discarded because of this, it should go through
            on the next polling cycle. This will 'spread' smart requests over
            subsequent polls
            """
            self._lazypoll_requests.clear()
            self.cloudpoll_requests = 0  # type: ignore
            # self.ns_handlers could change at any time due to async
            # message parsing (handlers might be dynamically created by then)
            for handler in [
                handler
                for handler in self.ns_handlers.values()
                if (handler.ns != namespace)
            ]:
                if handler.polling_strategy:
                    await handler.polling_strategy(handler)
                    if not self.online:
                        break  # do not return: do the flush first!

            # needed even if offline: it takes care of resetting the ns_multiple state
            if self._multiple_requests:
                await self._async_poll_multiple_flush()

            # when create_diagnostic_entities is True, after onlining we'll dynamically
            # scan the abilities to look for 'unknown' namespaces (kind of like tracing)
            # and try to build diagnostic entitities out of that
            if self._diagnostics_build and self.online:
                self._diagnostics_build = False
                self.log(self.DEBUG, "Diagnostic scan begin")
                try:
                    abilities = iter(self.descriptor.ability)
                    while self.online:
                        ability = next(abilities)
                        if ability in self.TRACE_ABILITY_EXCLUDE:
                            continue
                        ns_handler = self.get_handler_by_name(ability)
                        if ns_handler.polling_strategy:
                            continue
                        await ns_handler.async_get_safe()
                except StopIteration:
                    self.log(self.DEBUG, "Diagnostic scan end")
                except Exception as e:
                    self.log_exception(self.WARNING, e, "diagnostic scan")

        except asyncio.CancelledError:
            self.log(self.DEBUG, "Polling cancelled")
            raise
        except asyncio.TimeoutError:
            if self.online:
                self._set_offline()
            elif self._polling_delay < PARAM_HEARTBEAT_PERIOD:
                self._polling_delay += self.polling_period
            else:
                self._polling_delay = PARAM_HEARTBEAT_PERIOD
        except Exception as e:
            self.log_exception(self.WARNING, e, "_async_poll")
        finally:
            self._polling_task = None

        self._polling_unsub = self.schedule_callback(
            self._polling_delay, self._poll, None
        )
        self.log(self.DEBUG, "Polling end")

    async def async_poll_stop(self):
        """Ensure we're not polling nor any schedule is in place."""
        if self._polling_unsub:
            self._polling_unsub.cancel()
            self._polling_unsub = None
        elif self._polling_task:
            self._polling_task.cancel()
            try:
                await self._polling_task
            except asyncio.CancelledError:
                pass

    async def async_poll_full(self):
        """Stops an ongoing poll if any and executes a full poll (like when onlining)."""
        await self.async_poll_stop()
        # before retriggering ensure we're not overlapping with device shutdown
        if self.config_entry.state is ConfigEntryState.LOADED:
            self.device_debug = None
            for handler in self.ns_handlers.values():
                handler.polling_epoch_next = 0.0
            # this will also restart/schedule the cycle
            await self._poll()

    def schedule_poll(self, task_name: str):
        self.async_create_task(self.async_poll_full(), task_name, False)

    def bt_attached(self, bt_device: "ComponentApi.BTDevice", /):
        if self._bluetooth:
            if self._bluetooth is bt_device:
                return
            self._bluetooth.detach()
        self.log(
            self.DEBUG,
            "bt_attached to %s",
            bt_device.address,
        )
        self._bluetooth = bt_device
        if bt_device.is_connected:
            self.bt_connected()

        self.api.device_registry.async_update_device(
            self.device_entry.id,
            new_connections={
                (dr.CONNECTION_NETWORK_MAC, self.descriptor.macAddress),
                (dr.CONNECTION_BLUETOOTH, bt_device.address),
            },
        )

    def bt_detached(self):
        assert self._bluetooth
        self.log(
            self.DEBUG,
            "bt_detached from %s",
            self._bluetooth.address,
        )
        if self._bluetooth_active:
            self.bt_disconnected()
        self._bluetooth = None

    def bt_connected(self):
        _bluetooth = self._bluetooth
        assert _bluetooth
        self.log(
            self.DEBUG,
            "bt_connected to %s",
            _bluetooth.address,
        )
        self._bluetooth_active = _bluetooth
        self.sensor_protocol.update_attr_active(ProtocolSensor.ATTR_BLUETOOTH)
        if not self.online:
            self.schedule_poll("bt_connected")

    def bt_disconnected(self):
        assert self._bluetooth
        self.log(
            self.DEBUG,
            "bt_disconnected from %s",
            self._bluetooth.address,
        )
        self._bluetooth_active = None
        if self.online:
            self._set_offline()
            return
        # run this at the end so it will not double flush
        self.sensor_protocol.update_attr_inactive(ProtocolSensor.ATTR_BLUETOOTH)

    def mqtt_receive(self, message: "MerossResponse", /):
        assert self._mqtt_connected
        self._mqtt_lastresponse = epoch = time()
        self._trace_or_log(epoch, message, CONF_PROTOCOL_MQTT, self.TRACE_RX)
        if not self._mqtt_active:
            self._mqtt_active = self._mqtt_connected
            if self.online:
                self.sensor_protocol.update_attr_active(ProtocolSensor.ATTR_MQTT)
        if self.curr_protocol is not CONF_PROTOCOL_MQTT:
            if (self.pref_protocol is CONF_PROTOCOL_MQTT) or (not self._http_active):
                self._switch_protocol(CONF_PROTOCOL_MQTT)
        self._receive(epoch, message)
        self._handle(message)

    def mqtt_attached(self, mqtt_connection: "MQTTConnection", /):
        if self._mqtt_connection:
            self._mqtt_connection.detach(self)
        self.log(
            self.DEBUG,
            "mqtt_attached to %s",
            self.loggable_broker(mqtt_connection.broker),
        )
        self._mqtt_connection = mqtt_connection
        self._topic_response = mqtt_connection.topic_response
        if mqtt_connection.mqtt_is_connected:
            self.mqtt_connected()

    def mqtt_detached(self):
        assert self._mqtt_connection
        self.log(
            self.DEBUG,
            "mqtt_detached from %s",
            self.loggable_broker(self._mqtt_connection.broker),
        )
        if self._mqtt_connected:
            self.mqtt_disconnected()
        self._mqtt_connection = None

    def mqtt_connected(self):
        _mqtt_connection = self._mqtt_connection
        assert _mqtt_connection
        self.log(
            self.DEBUG,
            "mqtt_connected to %s",
            self.loggable_broker(_mqtt_connection.broker),
        )
        self._mqtt_connected = _mqtt_connection
        self.sensor_protocol.update_attr_active(ProtocolSensor.ATTR_MQTT_BROKER)
        if _mqtt_connection.profile.allow_mqtt_publish:
            self._mqtt_publish = _mqtt_connection
            if not self.online:
                self.schedule_poll("mqtt_connected")
        elif self.conf_protocol is CONF_PROTOCOL_MQTT:
            self.log(
                self.WARNING,
                "MQTT connection doesn't allow publishing - device will not be able send commands",
                timeout=14400,
            )

    def mqtt_disconnected(self):
        assert self._mqtt_connection
        self.log(
            self.DEBUG,
            "mqtt_disconnected from %s",
            self.loggable_broker(self._mqtt_connection.broker),
        )
        self._mqtt_connected = self._mqtt_publish = self._mqtt_active = None
        self.device_debug = None
        if self.curr_protocol is CONF_PROTOCOL_MQTT:
            if self.conf_protocol is CONF_PROTOCOL_AUTO:
                self._switch_protocol(CONF_PROTOCOL_HTTP)
                return
            # conf_protocol should be CONF_PROTOCOL_MQTT:
            elif self.online:
                self._set_offline()
                return
        # run this at the end so it will not double flush
        self.sensor_protocol.update_attrs_inactive(
            ProtocolSensor.ATTR_MQTT_BROKER, ProtocolSensor.ATTR_MQTT
        )

    def profile_linked(self, profile: "MQTTProfile", /):
        assert self._profile is not profile
        if self._profile:
            self._profile.unlink(self)
        self._profile = profile
        self.log(
            self.DEBUG,
            "linked to profile:%s",
            self.loggable_profile_id(profile.id),
        )
        self._check_protocol()
        if device_info := profile.get_device_info(self.id):
            self.update_device_info(device_info, profile)

    def profile_unlinked(self):
        assert self._profile
        if self._mqtt_connection:
            self._mqtt_connection.detach(self)
        self.log(
            self.DEBUG,
            "unlinked from profile:%s",
            self.loggable_profile_id(self._profile.id),
        )
        self._profile = None

    def _receive(self, epoch: float, message: MerossResponse, /):
        """
        default (received) message handling entry point
        """
        self.lastresponse = epoch
        # TODO: check if this is ok at this level: we want to early detect malformed messages
        # but some operations like protocol switching might need further attention
        # As of now, they're performed before the check in the calling functions
        message.check()
        message_size = len(message.json)
        if message_size > self.device_response_size_min:
            self.device_response_size_min = message_size
            if message_size > self.device_response_size_max:
                self.device_response_size_max = message_size

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
        if abs(self.device_timedelta) > PARAM_TIMESTAMP_TOLERANCE:
            if not self._config_device_timestamp(epoch):
                if (epoch - self.device_timedelta_log_epoch) > 604800:  # 1 week lockout
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
                    self.loggable_dict_str(header),
                )

        if not self.online:
            self._set_online()
            self._polling_delay = self.polling_period
            # retrigger the polling loop in case it is scheduled/pending.
            # This could happen when we receive an MQTT message
            if self._polling_unsub:
                self._polling_unsub.cancel()
                self._polling_unsub = self.schedule_callback(
                    0, self._poll, message.namespace
                )

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
                    self.loggable_dict_str(message.payload),
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
                    self.loggable_dict_str(message),
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
            handler.lastpush = message.payload

        handler.handle_response(message)

    def _create_handler(self, ns: "mn.Namespace", /):
        """Called by the base device message parsing chain when a new
        NamespaceHandler need to be defined (This happens the first time
        the namespace enters the message handling flow)"""
        return NamespaceHandler(self, ns)

    def _handle_Appliance_Config_Info(self, message: MerossMessage, /):
        """{"info":{"homekit":{"model":"MSH300HK","sn":"#","category":2,"setupId":"#","setupCode":"#","uuid":"#","token":"#"}}}"""
        pass

    def _handle_Appliance_Control_Bind(self, message: MerossMessage, /):
        # already processed by the MQTTConnection session manager
        pass

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

        needsave = query_abilities = False
        descr = self.descriptor
        oldfirmware = descr.firmware
        oldtimezone = descr.timezone
        descr.update(message.payload)

        if oldtimezone != descr.timezone:
            needsave = True

        if oldfirmware != descr.firmware:
            needsave = True
            query_abilities = True
            if self.update_firmware:
                self.update_firmware.update_info()
            if not self.config.get(CONF_HOST):
                self._update_host()
        else:
            query_abilities = False

        if self.conf_protocol is CONF_PROTOCOL_AUTO:
            if self._mqtt_active:
                if not is_device_online(descr.system):
                    self.device_debug = None
                    self._mqtt_active = None
                    self.sensor_protocol.update_attr_inactive(ProtocolSensor.ATTR_MQTT)
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
                    self.digest_parsers[key_digest].__name__,
                )

        if needsave:
            self.schedule_entry_update(query_abilities)

    def _handle_Appliance_System_Clock(self, message: MerossMessage, /):
        # already processed by the MQTTConnection session manager
        pass

    def _handle_Appliance_System_Debug(self, message: MerossMessage, /):
        # this ns is queried when we're HTTP connected and the device reports it is
        # also MQTT connected but meross_lan has no confirmation (_mqtt_active == None)
        # we're then going to inspect the device reported broker and see if
        # our config allow to connect
        self.device_debug = p_debug = message.payload[mc.KEY_DEBUG]
        broker = get_active_broker(p_debug)
        mqtt_connection = self._mqtt_connection
        if mqtt_connection:
            if mqtt_connection.broker.host == broker.host:
                if self._mqtt_connected and not self._mqtt_active:
                    self._mqtt_active = mqtt_connection
                    self.sensor_protocol.update_attr_active(ProtocolSensor.ATTR_MQTT)
                    if self.curr_protocol is not self.pref_protocol:
                        self._switch_protocol(self.pref_protocol)
            elif mqtt_connection.is_cloud_connection:
                mqtt_connection.detach(self)

    def _handle_Appliance_System_Online(self, message: MerossMessage, /):
        # already processed by the MQTTConnection session manager
        pass

    def _handle_Appliance_System_Report(self, message: MerossMessage, /):
        # No clue: sent (MQTT PUSH) by the device on initial connection
        # TODO: move these empty stubs to VoidHandlers on a lazy basis
        pass

    def _handle_Appliance_System_Time(self, message: MerossMessage, /):
        self.descriptor.update_time(message.payload[mc.KEY_TIME])
        self.schedule_entry_update(False)

    def _config_device_timestamp(self, epoch: float, /):
        if self.mqtt_locallyactive and (
            mn.Appliance_System_Clock in self.descriptor.ability
        ):
            # only deal with time related settings when devices are un-paired
            # from the meross cloud
            last_config_delay = epoch - self.device_timedelta_config_epoch
            if last_config_delay > 1800:
                # 30 minutes 'cooldown' in order to avoid restarting
                # the procedure too often
                self.mqtt_request(*mn.Appliance_System_Clock.request_default)
                self.device_timedelta_config_epoch = epoch
                return True
            if last_config_delay < 30:
                # 30 sec 'deadzone' where we allow the timestamp
                # transaction to complete (should really be like few seconds)
                return True
        return False

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

            idx = bisect.bisect_right(timerules, timestamp, key=_get_epoch)
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
        # assert self.mqtt_locallyactive
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
                            idx = bisect.bisect_right(
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

    def _switch_protocol(self, protocol):
        self.log(
            self.DEBUG,
            "Switching protocol to %s",
            protocol,
        )
        self.curr_protocol = protocol
        if self.online:
            self.sensor_protocol.set_available()

    def _process_uuid_mismatch(
        self, response_uuid: str, payload_all: "MerossPayloadType | None"
    ):
        """When detecting a wrong uuid from a response we offline the device and setup an issue."""
        if self.online:
            self._set_offline()
        try:
            _remote_type = payload_all[mc.KEY_ALL][mc.KEY_SYSTEM][mc.KEY_HARDWARE][mc.KEY_TYPE]  # type: ignore
        except Exception:
            _remote_type = "<unknown>"
        self.log(
            self.CRITICAL,
            "Wrong device at configured address %s (own uuid:%s, remote uuid:%s, type:%s)",
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
