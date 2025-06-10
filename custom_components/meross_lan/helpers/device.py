import abc
import asyncio
import bisect
from datetime import UTC, tzinfo
from json import JSONDecodeError
from time import time
from typing import TYPE_CHECKING
from uuid import uuid4
import zoneinfo

import aiohttp
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import callback
from homeassistant.helpers import device_registry as dr
from homeassistant.util import dt as dt_util
import voluptuous as vol

from . import datetime_from_epoch
from .. import const as mlc
from ..button import MLPersistentButton

# only import those 'often used' symbols to get a tiny bit of speed improvement
from ..const import (
    CONF_HOST,
    CONF_PAYLOAD,
    CONF_PROTOCOL_AUTO,
    CONF_PROTOCOL_HTTP,
    CONF_PROTOCOL_MQTT,
    PARAM_HEADER_SIZE,
    PARAM_HEARTBEAT_PERIOD,
    PARAM_TIMESTAMP_TOLERANCE,
)
from ..merossclient import (
    HostAddress,
    get_active_broker,
    is_device_online,
    json_dumps,
)
from ..merossclient.httpclient import MerossHttpClient, TerminatedException
from ..merossclient.protocol.message import (
    MerossRequest,
    MerossResponse,
    compute_message_encryption_key,
    compute_message_signature,
    get_message_uuid,
)
from ..sensor import ProtocolSensor
from ..update import MLUpdate
from .manager import ConfigEntryManager, EntityManager
from .namespaces import NamespaceHandler, mc, mn

if TYPE_CHECKING:
    from types import CoroutineType
    from typing import (
        Any,
        Callable,
        ClassVar,
        Collection,
        Final,
        Iterable,
        Iterator,
        NotRequired,
        Unpack,
    )

    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

    from ..devices.hub import SubDevice
    from ..merossclient import MerossDeviceDescriptor, MerossRequestType
    from ..merossclient.cloudapi import DeviceInfoType, LatestVersionType
    from ..merossclient.protocol.message import MerossMessage
    from ..merossclient.protocol.types import (
        MerossHeaderType,
        MerossMessageType,
        MerossPayloadType,
    )
    from .component_api import ComponentApi
    from .entity import MLEntity
    from .mqtt_profile import MQTTConnection, MQTTProfile
    from .namespaces import NamespaceParser

    DigestParseFunc = Callable[[dict], None] | Callable[[list], None]
    DigestInitReturnType = tuple[DigestParseFunc, Iterable[NamespaceHandler]]
    DigestInitFunc = Callable[["Device", Any], DigestInitReturnType]
    NamespaceInitFunc = Callable[["Device"], None]
    AsyncRequestFunc = Callable[
        [str, str, MerossPayloadType], CoroutineType[Any, Any, MerossResponse | None]
    ]


# when tracing we enumerate appliance abilities to get insights on payload structures
# this list will be excluded from enumeration since it's redundant/exposing sensitive info
# or simply crashes/hangs the device
TRACE_ABILITY_EXCLUDE = (
    mn.Appliance_System_Ability.name,
    mn.Appliance_System_All.name,
    mn.Appliance_System_Clock.name,
    mn.Appliance_System_DNDMode.name,
    mn.Appliance_System_Firmware.name,
    mn.Appliance_System_Hardware.name,
    mn.Appliance_System_Online.name,
    mn.Appliance_System_Position.name,
    # mn.Appliance_System_Report.name,
    mn.Appliance_System_Time.name,
    # mn.Appliance_Config_Key.name,
    # mn.Appliance_Config_Trace.name,
    # mn.Appliance_Config_Wifi.name,
    # mn.Appliance_Config_WifiList.name,
    # mn.Appliance_Config_WifiX.name,
    # mn.Appliance_Control_Bind.name,
    # mn.Appliance_Control_Multiple.name,
    # mn.Appliance_Control_TimerX.name,
    mn.Appliance_Control_TriggerX.name,
    mn.Appliance_Control_Unbind.name,
    # mn.Appliance_Control_Upgrade.name,  # disconnects
    # mn.Appliance_Digest_TimerX.name,
    # mn.Appliance_Digest_TriggerX.name,
    "Appliance.Hub.Exception",  # disconnects
    "Appliance.Hub.Report",  # disconnects
    "Appliance.Hub.SubdeviceList",  # disconnects
    "Appliance.Hub.PairSubDev",  # disconnects
    "Appliance.Hub.SubDevice.Beep",  # protocol replies with error code: 5000
    "Appliance.Hub.SubDevice.MotorAdjust",  # protocol replies with error code: 5000
    mn.Appliance_Mcu_Firmware.name,  # disconnects
    mn.Appliance_Mcu_Upgrade.name,  # disconnects
    mn.Appliance_Mcu_Hp110_Preview.name,  # disconnects
    *(
        name
        for name, ns in mn.NAMESPACES.items()
        if (ns.has_get is False) and (ns.has_push_query is False)
    ),
    *(
        name
        for name, ns in mn.HUB_NAMESPACES.items()
        if (ns.has_get is False) and (ns.has_push_query is False)
    ),
)

TIMEZONES_SET = None


class BaseDevice(EntityManager):
    """
    Abstract base class for Device and SubDevice (from hub)
    giving common behaviors like device_registry interface
    """

    if TYPE_CHECKING:
        NAMESPACES: ClassVar[mn.NamespacesMapType]
        # override some nullable since we're pretty sure they're none
        config_entry: Final[ConfigEntry]  # type: ignore
        deviceentry_id: Final[EntityManager.DeviceEntryIdType]  # type: ignore

        online: Final[bool]
        device_registry_entry: Final[dr.DeviceEntry]

        class Args(EntityManager.Args):
            config_entry: ConfigEntry
            name: str
            model: str
            hw_version: NotRequired[str]
            sw_version: NotRequired[str]
            connections: NotRequired[set[tuple[str, str]]]
            via_device: NotRequired[tuple[str, str]]

    NAMESPACES = mn.NAMESPACES

    __slots__ = (
        "online",
        "device_registry_entry",
    )

    def __init__(self, id: str, **kwargs: "Unpack[Args]"):
        identifiers = {(mlc.DOMAIN, id)}
        kwargs["deviceentry_id"] = {"identifiers": identifiers}
        super().__init__(
            id,
            **kwargs,
        )
        self.online = False
        self.device_registry_entry = self.api.device_registry.async_get_or_create(
            config_entry_id=self.config_entry.entry_id,
            connections=kwargs.get("connections"),
            manufacturer=mc.MANUFACTURER,
            name=kwargs.get("name"),
            model=kwargs.get("model"),
            hw_version=kwargs.get("hw_version"),
            sw_version=kwargs.get("sw_version"),
            via_device=kwargs.get("via_device"),
            identifiers=identifiers,
        )

    # interface: EntityManager
    @property
    def name(self) -> str:
        """
        returns a proper (friendly) device name for logging purposes
        """
        if _device_registry_entry := self.device_registry_entry:
            return (
                _device_registry_entry.name_by_user
                or _device_registry_entry.name
                or self._get_internal_name()
            )
        return self._get_internal_name()

    # interface: self
    async def async_request(
        self,
        namespace: str,
        method: str,
        payload: "MerossPayloadType",
    ) -> MerossResponse | None:
        raise NotImplementedError("async_request")

    async def async_request_ack(
        self,
        namespace: str,
        method: str,
        payload: "MerossPayloadType",
    ) -> MerossResponse | None:
        response = await self.async_request(namespace, method, payload)
        return (
            response
            if response and response[mc.KEY_HEADER][mc.KEY_METHOD] != mc.METHOD_ERROR
            else None
        )

    def request(self, request_tuple: "MerossRequestType"):
        return self.async_create_task(
            self.async_request(*request_tuple), f".request({request_tuple})"
        )

    def check_device_timezone(self):
        raise NotImplementedError("check_device_timezone")

    def _set_online(self):
        self.log(self.DEBUG, "Back online!")
        self.online = True  # type: ignore
        for entity in self.entities.values():
            entity.set_available()

    def _set_offline(self):
        self.log(self.DEBUG, "Going offline!")
        self.online = False  # type: ignore
        for entity in self.entities.values():
            entity.set_unavailable()

    @property
    @abc.abstractmethod
    def tz(self) -> tzinfo:
        raise NotImplementedError("tz")

    @abc.abstractmethod
    def get_type(self) -> mlc.DeviceType:
        raise NotImplementedError("get_type")

    @abc.abstractmethod
    def _get_internal_name(self) -> str:
        raise NotImplementedError("_get_internal_name")


class Device(BaseDevice, ConfigEntryManager):
    """
    Generic protocol handler class managing the physical device stack/state
    """

    if TYPE_CHECKING:
        DIGEST_INIT: Final[dict[str, Any]]
        NAMESPACE_INIT: Final[dict[str, Any]]

        # these are set from ConfigEntry
        config: mlc.DeviceConfigType
        polling_period: int
        _polling_delay: int
        conf_protocol: str
        pref_protocol: str
        curr_protocol: str
        # other default property values
        tz: tzinfo

        device_timestamp: int

        _profile: MQTTProfile | None
        _mqtt_connection: MQTTConnection | None
        _mqtt_connected: MQTTConnection | None
        _mqtt_publish: MQTTConnection | None
        _mqtt_active: MQTTConnection | None
        _mqtt_lastrequest: float
        _mqtt_lastresponse: float
        _http: MerossHttpClient | None
        _http_active: MerossHttpClient | None
        _http_lastrequest: float
        _http_lastresponse: float
        namespace_handlers: dict[str, NamespaceHandler]
        namespace_pushes: dict[str, dict]
        digest_handlers: dict[str, DigestParseFunc]
        digest_pollers: set[NamespaceHandler]
        _lazypoll_requests: list[NamespaceHandler]
        _polling_epoch: float
        _polling_callback_unsub: asyncio.TimerHandle | None
        _polling_callback_shutdown: asyncio.Future | None
        _queued_cloudpoll_requests: int
        multiple_max: int
        _timezone_next_check: float
        _trace_ability_callback_unsub: asyncio.TimerHandle | None
        _diagnostics_build: bool

        # entities
        sensor_protocol: ProtocolSensor
        update_firmware: MLUpdate | None

        # HubMixin attributes: beware these are only
        # initialized in HubMixin(s) and not set/available in standard Device(s)
        subdevices: dict[object, "SubDevice"]

    @staticmethod
    def digest_parse_empty(digest: dict | list):
        pass

    @staticmethod
    def digest_init_empty(
        device: "Device", digest: dict | list
    ) -> "DigestInitReturnType":
        return Device.digest_parse_empty, ()

    @staticmethod
    def namespace_init_empty(device: "Device"):
        pass

    DIGEST_INIT = {
        mc.KEY_FAN: ".fan",
        mc.KEY_LIGHT: ".light",
        mc.KEY_TIMER: digest_init_empty,
        mc.KEY_TIMERX: digest_init_empty,
        mc.KEY_TOGGLE: ".switch",
        mc.KEY_TOGGLEX: ".switch",
        mc.KEY_TRIGGER: digest_init_empty,
        mc.KEY_TRIGGERX: digest_init_empty,
    }
    """
    Static dict of 'digest initialization function(s)'.
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
    init/parsing will not harm.
    """

    NAMESPACE_INIT = {
        mn.Appliance_Config_OverTemp.name: (".devices.mss", "OverTempEnableSwitch"),
        mn.Appliance_Control_ConsumptionConfig.name: (
            ".devices.mss",
            "ConsumptionConfigNamespaceHandler",
        ),
        mn.Appliance_Control_Electricity.name: (
            ".devices.mss",
            "namespace_init_electricity",
        ),
        mn.Appliance_Control_ElectricityX.name: (
            ".devices.mss",
            "ElectricityXNamespaceHandler",
        ),
        mn.Appliance_Control_ConsumptionH.name: (
            ".sensor",
            "ConsumptionHNamespaceHandler",
        ),
        mn.Appliance_Control_ConsumptionX.name: (".devices.mss", "ConsumptionXSensor"),
        mn.Appliance_Control_Fan.name: (".fan", "namespace_init_fan"),
        mn.Appliance_Control_FilterMaintenance.name: (
            ".sensor",
            "FilterMaintenanceNamespaceHandler",
        ),
        mn.Appliance_Control_Mp3.name: (".media_player", "MLMp3Player"),
        mn.Appliance_Control_PhysicalLock.name: (".switch", "PhysicalLockSwitch"),
        mn.Appliance_Control_Presence_Config.name: (
            ".devices.ms600",
            "namespace_init_presence_config",
        ),
        mn.Appliance_Control_Screen_Brightness.name: (
            ".devices.thermostat",
            "ScreenBrightnessNamespaceHandler",
        ),
        mn.Appliance_Control_Sensor_Latest.name: (
            ".devices.misc",
            "SensorLatestNamespaceHandler",
        ),
        mn.Appliance_Control_Sensor_LatestX.name: (
            ".devices.misc",
            "namespace_init_sensor_latestx",
        ),
        "Appliance.Control.Thermostat.ModeC": (
            ".devices.mts300",
            "Mts300Climate",
        ),
        mn.Appliance_RollerShutter_State.name: (".cover", "MLRollerShutter"),
        mn.Appliance_System_DNDMode.name: (".light", "MLDNDLightEntity"),
        mn.Appliance_System_Runtime.name: (".sensor", "MLSignalStrengthSensor"),
    }
    """
    Static dict of namespace initialization functions. This will be looked up
    and matched against the current device abilities (at device init time) and
    usually setups a dedicated namespace handler and/or a dedicated entity.
    As far as the initialization functions are looked up in related modules,
    they'll be cached in the dict.
    Namespace handlers will be initialized in the order as they appear in the dict
    and this could have consequences in the order of polls
    """

    DEFAULT_PLATFORMS = ConfigEntryManager.DEFAULT_PLATFORMS | {
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
        "needsave",
        "_async_entry_update_unsub",
        "device_debug",
        "device_info",
        "device_timestamp",
        "device_timedelta",
        "device_timedelta_log_epoch",
        "device_timedelta_config_epoch",
        "device_response_size_min",
        "device_response_size_max",
        "lastrequest",
        "lastresponse",
        "_topic_response",  # sets the "from" field in request messages
        "_profile",
        "_mqtt_connection",  # we're binded to an MQTT profile/broker
        "_mqtt_connected",  # the broker is online/connected
        "_mqtt_publish",  # the broker accepts 'publish' (cloud broker conf might disable publishing)
        "_mqtt_active",  # the broker receives valid traffic i.e. the device is 'mqtt' reachable
        "_mqtt_lastrequest",
        "_mqtt_lastresponse",
        "_http",  # cached MerossHttpClient
        "_http_active",  # HTTP is 'online' i.e. reachable
        "_http_lastrequest",
        "_http_lastresponse",
        "namespace_handlers",
        "namespace_pushes",
        "digest_handlers",
        "digest_pollers",
        "_lazypoll_requests",
        "_polling_epoch",
        "_polling_callback_unsub",
        "_polling_callback_shutdown",
        "_queued_cloudpoll_requests",
        "multiple_max",
        "_multiple_requests",
        "_multiple_response_size",
        "_timezone_next_check",
        "_trace_ability_callback_unsub",
        "_diagnostics_build",
        "sensor_protocol",
        "update_firmware",
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
        self.needsave = False
        self._async_entry_update_unsub = None
        self.curr_protocol = CONF_PROTOCOL_AUTO
        self.device_debug = None
        self.device_info = None
        self.device_timestamp = 0
        self.device_timedelta = 0
        self.device_timedelta_log_epoch = 0
        self.device_timedelta_config_epoch = 0
        self.device_response_size_min = 1000
        self.device_response_size_max = (
            descriptor.ability.get(mn.Appliance_Control_Multiple.name, {}).get(
                "maxCmdNum", 0
            )
            * 800
        )
        self.lastrequest = 0.0
        self.lastresponse = 0.0
        self._topic_response = mc.MANUFACTURER
        self._profile = None
        self._mqtt_connection = None
        self._mqtt_connected = None
        self._mqtt_publish = None
        self._mqtt_active = None
        self._mqtt_lastrequest = 0
        self._mqtt_lastresponse = 0
        self._http = None
        self._http_active = None
        self._http_lastrequest = 0
        self._http_lastresponse = 0
        self.namespace_handlers = {}
        self.namespace_pushes = {}
        self.digest_handlers = {}
        self.digest_pollers = set()
        self._lazypoll_requests = []
        NamespaceHandler(self, mn.Appliance_System_All)
        self._polling_epoch = 0.0  # when 0 we're not in the polling callback loop
        self._polling_callback_unsub = None
        self._polling_callback_shutdown = None
        self._queued_cloudpoll_requests = 0
        self.multiple_max = 0
        self._timezone_next_check = (
            0
            if mn.Appliance_System_Time.name in descriptor.ability
            else mlc.PARAM_INFINITE_TIMEOUT
        )
        self._trace_ability_callback_unsub = None
        self._diagnostics_build = False

        super().__init__(
            config_entry.data[mlc.CONF_DEVICE_ID],
            api=api,
            hass=api.hass,
            config_entry=config_entry,
            name=descriptor.productname,
            model=descriptor.productmodel,
            hw_version=descriptor.hardwareVersion,
            sw_version=descriptor.firmwareVersion,
            connections={(dr.CONNECTION_NETWORK_MAC, descriptor.macAddress)},
        )

        self.sensor_protocol = ProtocolSensor(self)
        self.update_firmware = None
        MLPersistentButton(
            self,
            None,
            "button_refresh",
            self._async_button_refresh_press,
            MLPersistentButton.DeviceClass.RESTART,
            name="Refresh",
            entity_category=MLPersistentButton.EntityCategory.DIAGNOSTIC,
        )
        MLPersistentButton(
            self,
            None,
            "button_reload",
            self._async_button_reload_press,
            MLPersistentButton.DeviceClass.RESTART,
            name="Reload",
            entity_category=MLPersistentButton.EntityCategory.DIAGNOSTIC,
        )

        self._update_config()

        # the update entity will only be instantiated 'on demand' since
        # we might not have this for devices not related to a cloud profile
        # This cleanup code is to ease the transition out of the registry
        # when previous version polluted it
        ent_reg = self.api.entity_registry
        update_firmware_entity_id = ent_reg.async_get_entity_id(
            MLUpdate.PLATFORM, mlc.DOMAIN, f"{self.id}_update_firmware"
        )
        if update_firmware_entity_id:
            ent_reg.async_remove(update_firmware_entity_id)

    async def async_init(self):
        api = self.api
        descriptor = self.descriptor

        if tzname := descriptor.timezone:
            # self.tz defaults to UTC on init
            with self.exception_warning(
                "loading timezone(%s) - check your python environment",
                tzname,
                timeout=14400,
            ):
                self.tz = await api.async_load_zoneinfo(tzname)

        for namespace, ns_init_func in Device.NAMESPACE_INIT.items():
            if namespace not in descriptor.ability:
                continue
            try:
                try:
                    ns_init_func(self)
                except TypeError:
                    try:
                        ns_init_func = getattr(
                            await api.async_import_module(ns_init_func[0]),
                            ns_init_func[1],
                        )
                    except Exception as exception:
                        self.log_exception(
                            self.WARNING,
                            exception,
                            "loading namespace initializer for %s",
                            namespace,
                        )
                        ns_init_func = Device.namespace_init_empty
                    Device.NAMESPACE_INIT[namespace] = ns_init_func
                    ns_init_func(self)

            except Exception as exception:
                self.log_exception(
                    self.WARNING, exception, "initializing namespace %s", namespace
                )

        for key_digest, _digest in (
            descriptor.digest.items() or descriptor.control.items()
        ):
            # older firmwares (MSS110 with 1.1.28) look like
            # carrying 'control' instead of 'digest'
            try:
                try:
                    self.digest_handlers[key_digest], _digest_pollers = (
                        Device.DIGEST_INIT[key_digest](self, _digest)
                    )
                except (KeyError, TypeError):
                    # KeyError: key is unknown to our code (fallback to lookup ".devices.{key_digest}")
                    # TypeError: key is a string containing the module path
                    try:
                        _module_path = Device.DIGEST_INIT.get(
                            key_digest, f".devices.{key_digest}"
                        )
                        digest_init_func: "DigestInitFunc" = getattr(
                            await api.async_import_module(_module_path),
                            f"digest_init_{key_digest}",
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
                    self.digest_handlers[key_digest], _digest_pollers = (
                        digest_init_func(self, _digest)
                    )
                self.digest_pollers.update(_digest_pollers)

            except Exception as exception:
                self.log_exception(
                    self.WARNING, exception, "initializing digest key '%s'", key_digest
                )
                self.digest_handlers[key_digest] = Device.digest_parse_empty

    def start(self):
        # called by async_setup_entry after the entities have been registered
        # here we'll register mqtt listening (in case) and start polling after
        # the states have been eventually restored (some entities need this)
        self._check_protocol_ext()
        self._polling_callback_unsub = self.schedule_async_callback(
            0, self._async_polling_callback, None
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
        self._update_config()
        self._check_protocol_ext()

        # config_entry update might come from DHCP or OptionsFlowHandler address update
        # so we'll eventually retry querying the device
        if not self.online:
            self.request(mn.Appliance_System_All.request_get)

    async def async_create_diagnostic_entities(self):
        self._diagnostics_build = True  # set a flag cause we'll lazy scan/build
        await super().async_create_diagnostic_entities()

    async def async_destroy_diagnostic_entities(self, remove: bool = False):
        self._diagnostics_build = False
        for namespace_handler in self.namespace_handlers.values():
            if (
                namespace_handler.polling_strategy
                is NamespaceHandler.async_poll_diagnostic
            ):
                namespace_handler.polling_strategy = None
        await super().async_destroy_diagnostic_entities(remove)

    def get_logger_name(self) -> str:
        return f"{self.descriptor.type}_{self.loggable_device_id(self.id)}"

    def _trace_opened(self, epoch: float):
        descr = self.descriptor
        # set the scheduled callback first so it gets (eventually) cleaned
        # should the following self.trace close the file due to an error
        self._trace_ability_callback_unsub = self.schedule_async_callback(
            mlc.PARAM_TRACING_ABILITY_POLL_TIMEOUT,
            self._async_trace_ability,
            iter(descr.ability),
        )
        self.trace(epoch, descr.all, mn.Appliance_System_All.name)
        self.trace(epoch, descr.ability, mn.Appliance_System_Ability.name)

    def trace_close(
        self, exception: Exception | None = None, error_context: str | None = None
    ):
        if self._trace_ability_callback_unsub:
            self._trace_ability_callback_unsub.cancel()
            self._trace_ability_callback_unsub = None
        super().trace_close(exception, error_context)

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
        if self._http:
            # to be called before stopping polling so that it breaks http timeouts
            await self._http.async_terminate()
            self._http = None

        await self._async_polling_stop()
        await super().async_shutdown()
        self.namespace_handlers = None  # type: ignore
        self.digest_handlers = None  # type: ignore
        self.digest_pollers = None  # type: ignore
        self._lazypoll_requests = None  # type: ignore
        self.sensor_protocol = None  # type: ignore
        self.update_firmware = None
        self.api.devices[self.id] = None

    async def async_request_raw(
        self,
        request: MerossRequest,
    ) -> MerossResponse | None:
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
                if response := await self.async_mqtt_request_raw(request):
                    return response

                mqttfailed = True

            # MQTT not connected or not allowing publishing
            if self.conf_protocol is CONF_PROTOCOL_MQTT:
                return None

        # curr_protocol is HTTP
        if response := await self.async_http_request_raw(request):
            return response

        if (
            self._mqtt_active  # device is connected to broker
            and self._mqtt_publish  # profile allows publishing
            and not mqttfailed  # we've already tried mqtt
        ):
            return await self.async_mqtt_request_raw(request)

        return None

    async def async_request(
        self,
        namespace: str,
        method: str,
        payload: "MerossPayloadType",
    ) -> MerossResponse | None:
        """
        route the request through MQTT or HTTP to the physical device according to
        current protocol. When switching transport the message is recomputed to
        avoid reusing the same (old) timestamps and messageids
        """
        self.lastrequest = time()
        mqttfailed = False
        if self.curr_protocol is CONF_PROTOCOL_MQTT:
            if self._mqtt_publish:
                if response := await self.async_mqtt_request(
                    namespace, method, payload
                ):
                    return response
                mqttfailed = True
            # MQTT not connected or not allowing publishing
            if self.conf_protocol is CONF_PROTOCOL_MQTT:
                return None

        # curr_protocol is HTTP or mqtt failed somehow
        if response := await self.async_http_request(namespace, method, payload):
            return response

        if (
            self._mqtt_active  # device is connected to broker
            and self._mqtt_publish  # profile allows publishing
            and not mqttfailed  # we've already tried mqtt
        ):
            return await self.async_mqtt_request(namespace, method, payload)

        return None

    def check_device_timezone(self):
        """
        Verifies the device timezone has the same utc offset as HA local timezone.
        This is expecially sensible when the device has 'Consumption' or
        schedules (calendar entities) in order to align device local time to
        what is expected in HA.
        """
        # TODO: check why the emulator keeps raising the issue (at boot) when the TZ is ok
        ha_now = dt_util.now()
        device_now = ha_now.astimezone(self.tz)
        if ha_now.utcoffset() == device_now.utcoffset():
            self.remove_issue(mlc.ISSUE_DEVICE_TIMEZONE)
            return
        self.create_issue(
            mlc.ISSUE_DEVICE_TIMEZONE,
            severity=self.IssueSeverity.WARNING,
            translation_placeholders={"device_name": self.name},
        )

    def _set_offline(self):
        super()._set_offline()
        self._polling_delay = self.polling_period
        self._mqtt_active = self._http_active = None
        self.device_debug = None
        for handler in self.namespace_handlers.values():
            handler.polling_epoch_next = 0.0

    def get_type(self) -> mlc.DeviceType:
        return mlc.DeviceType.DEVICE

    def _get_internal_name(self) -> str:
        return self.descriptor.productname

    # interface: self
    @property
    def host(self):
        return self.config.get(CONF_HOST) or self.descriptor.innerIp

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

    def get_device_datetime(self, epoch):
        """
        given the epoch (utc timestamp) returns the datetime
        in device local timezone
        """
        return datetime_from_epoch(epoch, self.tz)

    def get_handler(self, ns: "mn.Namespace"):
        try:
            return self.namespace_handlers[ns.name]
        except KeyError:
            return self._create_handler(ns)

    def get_handler_by_name(self, namespace: str):
        try:
            return self.namespace_handlers[namespace]
        except KeyError:
            return self._create_handler(self.NAMESPACES[namespace])

    def register_parser(
        self,
        parser: "NamespaceParser",
        ns: "mn.Namespace",
    ):
        self.get_handler(ns).register_parser(parser)

    def register_parser_entity(
        self,
        entity: "MLEntity",
    ):
        self.get_handler(entity.ns).register_parser(entity)

    def register_togglex_channel(self, entity: "MLEntity"):
        """
        Checks if entity has an associated ToggleX behavior and eventually
        registers it
        """
        try:
            for togglex_digest in self.descriptor.digest[mc.KEY_TOGGLEX]:
                if togglex_digest[mc.KEY_CHANNEL] == entity.channel:
                    self.register_parser(entity, mn.Appliance_Control_ToggleX)
                    return True
        except KeyError:
            # no "togglex" in digest ?
            pass
        return False

    def schedule_entry_update(self, query_abilities: bool):
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

    async def _async_entry_update(self, query_abilities: bool):
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
        self.needsave = False

        with self.exception_warning("_async_entry_update"):
            data = dict(self.config_entry.data)
            data[mlc.CONF_TIMESTAMP] = time()  # force ConfigEntry update..
            data[CONF_PAYLOAD][mc.KEY_ALL] = self.descriptor.all
            if query_abilities and (
                response := await self.async_request(
                    *mn.Appliance_System_Ability.request_default
                )
            ):
                # fw update or whatever might have modified the device abilities.
                # we refresh the abilities list before saving the new config_entry
                data[CONF_PAYLOAD][mc.KEY_ABILITY] = response[mc.KEY_PAYLOAD][
                    mc.KEY_ABILITY
                ]
            self.hass.config_entries.async_update_entry(self.config_entry, data=data)

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

    async def async_entry_option_setup(self, config_schema: dict):
        """
        called when setting up an OptionsFlowHandler to expose
        configurable device preoperties which are stored at the device level
        and not at the configuration/option level
        see derived implementations
        """
        if mn.Appliance_Control_Multiple.name in self.descriptor.ability:
            config_schema[
                vol.Optional(
                    mlc.CONF_DISABLE_MULTIPLE,
                    default=False,
                    description={
                        "suggested_value": self.config.get(mlc.CONF_DISABLE_MULTIPLE)
                    },
                )
            ] = bool

        if mn.Appliance_System_Time.name in self.descriptor.ability:
            global TIMEZONES_SET
            if TIMEZONES_SET is None:

                def _load():
                    """
                    These functions will use low levels imports and HA core 2024.5
                    complains about executing it in the main loop thread. We'll
                    so run these in an executor
                    """
                    return vol.In(sorted(zoneinfo.available_timezones()))

                try:
                    TIMEZONES_SET = await self.hass.async_add_executor_job(_load)
                except Exception as exception:
                    self.log_exception(
                        self.WARNING, exception, "building list of available timezones"
                    )
                    TIMEZONES_SET = str

            config_schema[
                vol.Optional(
                    mc.KEY_TIMEZONE,
                    description={"suggested_value": self.descriptor.timezone},
                )
            ] = TIMEZONES_SET

    async def async_entry_option_update(self, user_input: mlc.DeviceConfigType):
        """
        called when the user 'SUBMIT' an OptionsFlowHandler: here we'll
        receive the full user_input so to update device config properties
        (this is actually called in sequence with entry_update_listener
        just the latter is async)
        """
        if mn.Appliance_System_Time.name in self.descriptor.ability:
            timezone = user_input.get(mc.KEY_TIMEZONE)
            if timezone != self.descriptor.timezone:
                if await self.async_config_device_timezone(timezone):
                    # if there's a pending issue, the user might still
                    # use the OptionsFlow to fix stuff so we'll
                    # shut this down anyway..it will reappear in case
                    self.remove_issue(mlc.ISSUE_DEVICE_TIMEZONE)

    async def async_bind(
        self, broker: HostAddress, *, key: str | None = None, userid: str | None = None
    ):
        if key is None:
            key = self.key
        if userid is None:
            userid = self.descriptor.userId or ""
        bind = (
            mn.Appliance_Config_Key.name,
            mc.METHOD_SET,
            {
                mn.Appliance_Config_Key.key: {
                    mc.KEY_GATEWAY: {
                        mc.KEY_HOST: broker.host,
                        mc.KEY_PORT: broker.port,
                        mc.KEY_SECONDHOST: broker.host,
                        mc.KEY_SECONDPORT: broker.port,
                        mc.KEY_REDIRECT: 1,
                    },
                    mc.KEY_KEY: key,
                    mc.KEY_USERID: userid,
                }
            },
        )
        # we don't have a clue if it works or not..just go over http
        return await self.async_http_request(*bind)

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

    def disable_multiple(self):
        self.multiple_max = 0
        self._multiple_requests = None
        self._multiple_response_size = 0

    def enable_multiple(self):
        if not self.multiple_max:
            self.multiple_max: int = self.descriptor.ability.get(
                mn.Appliance_Control_Multiple.name, {}
            ).get("maxCmdNum", 0)
            self._multiple_requests = []
            self._multiple_response_size = PARAM_HEADER_SIZE

    async def async_multiple_requests_ack(
        self, requests: "Collection[MerossRequestType]", auto_handle: bool = True
    ) -> list["MerossMessageType"] | None:
        """Send requests in a single NS_APPLIANCE_CONTROL_MULTIPLE message.
        If the whole request is succesful (might be partial if the device response
        overflown somehow (see JSON patching in HTTP request api)
        returns the unpacked reponses in a list.
        auto_handle will instruct this api to forward the responses to the
        namespace handling before returning.
        Contrary to async_multiple_requests_flush this doesn't recover from
        partial message responses so it doesn't resend missed requests/responses
        """
        if multiple_response := await self.async_request_ack(
            mn.Appliance_Control_Multiple.name,
            mc.METHOD_SET,
            {
                mn.Appliance_Control_Multiple.key: [
                    {
                        mc.KEY_HEADER: {
                            mc.KEY_MESSAGEID: uuid4().hex,
                            mc.KEY_METHOD: request[1],
                            mc.KEY_NAMESPACE: request[0],
                        },
                        mc.KEY_PAYLOAD: request[2],
                    }
                    for request in requests
                ]
            },
        ):
            if auto_handle:
                multiple_responses = multiple_response[mc.KEY_PAYLOAD][mc.KEY_MULTIPLE]
                for message in multiple_responses:
                    self._handle(
                        message[mc.KEY_HEADER],
                        message[mc.KEY_PAYLOAD],
                    )
                return multiple_responses
            return multiple_response[mc.KEY_PAYLOAD][mc.KEY_MULTIPLE]

    async def _async_multiple_requests_flush(self):
        assert self._multiple_requests
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
                        handler.lastrequest = time()
                        handler.polling_epoch_next = (
                            handler.lastrequest + handler.polling_period
                        )
                        multiple_requests.append(handler.polling_request)
                        lazypoll_requests.remove(handler)
                        multiple_response_size += handler.polling_response_size
                        requests_len += 1
                        # check if we can add more
                        break  # for
                else:
                    # no lazy_poller could match..break out of while
                    break  # while

            if requests_len == 1:
                await self.async_request(*multiple_requests[0])
                return

            if not (
                response := await self.async_request_ack(
                    mn.Appliance_Control_Multiple.name,
                    mc.METHOD_SET,
                    {
                        mn.Appliance_Control_Multiple.key: [
                            {
                                mc.KEY_HEADER: {
                                    mc.KEY_MESSAGEID: uuid4().hex,
                                    mc.KEY_METHOD: request[1],
                                    mc.KEY_NAMESPACE: request[0],
                                },
                                mc.KEY_PAYLOAD: request[2],
                            }
                            for request in multiple_requests
                        ]
                    },
                )
            ):
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
                        "Appliance.Control.Multiple failed with no response: requests=%d expected size=%d",
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
                    for request in multiple_requests:
                        await self.async_request(*request)
                        if not self.online:
                            break
                return

            multiple_responses = response[mc.KEY_PAYLOAD][mc.KEY_MULTIPLE]
            responses_len = len(multiple_responses)
            if self.isEnabledFor(self.DEBUG):
                self.log(
                    self.DEBUG,
                    "Appliance.Control.Multiple requests=%d (responses=%d) expected size=%d (actual=%d)",
                    requests_len,
                    responses_len,
                    multiple_response_size,
                    len(response.json()),
                )
            message: "MerossMessageType"
            if responses_len == requests_len:
                # faster shortcut
                for message in multiple_responses:
                    self._handle(
                        message[mc.KEY_HEADER],
                        message[mc.KEY_PAYLOAD],
                    )
                return
            elif responses_len:
                # the requests payload was too big and the response was
                # truncated. the http client tried to 'recover' by discarding
                # the incomplete payloads so we'll check what's missing
                for message in multiple_responses:
                    m_header = message[mc.KEY_HEADER]
                    self._handle(
                        m_header,
                        message[mc.KEY_PAYLOAD],
                    )
                    namespace = m_header[mc.KEY_NAMESPACE]
                    for request in multiple_requests:
                        if request[0] == namespace:
                            multiple_requests.remove(request)
                            break
                # and re-issue the missing ones
                requests_len = len(multiple_requests)
                multiple_response_size = -1  # logging purpose
                continue
            else:
                # no response at all..this is pathological but we have
                # examples (#526) of this so we'll just try issue single requests
                self.log(
                    self.WARNING,
                    "Appliance.Control.Multiple empty response (requests=%d expected size=%d)",
                    requests_len,
                    multiple_response_size,
                    timeout=14400,
                )
                for request in multiple_requests:
                    await self.async_request(*request)
                    if not self.online:
                        break
                return

    async def async_mqtt_request_raw(
        self,
        request: "MerossMessage",
    ) -> MerossResponse | None:
        _mqtt_publish = self._mqtt_publish
        if not _mqtt_publish:
            # even if we're smart enough to not call async_mqtt_request when no mqtt
            # available, it could happen we loose that when asynchronously coming here
            self.log(
                self.DEBUG,
                "Attempting to use async_mqtt_request with no publishing profile",
            )
            return None
        self._mqtt_lastrequest = time()
        self._trace_or_log(
            self._mqtt_lastrequest,
            request,
            CONF_PROTOCOL_MQTT,
            ConfigEntryManager.TRACE_TX,
        )
        if _mqtt_publish.is_cloud_connection:
            self._queued_cloudpoll_requests += 1
        return await _mqtt_publish.async_mqtt_publish(self.id, request)

    async def async_mqtt_request(
        self,
        namespace: str,
        method: str,
        payload: "MerossPayloadType",
    ):
        return await self.async_mqtt_request_raw(
            MerossRequest(self.key, namespace, method, payload, self._topic_response)
        )

    def mqtt_request(
        self,
        namespace: str,
        method: str,
        payload: "MerossPayloadType",
    ):
        return self.async_create_task(
            self.async_mqtt_request(namespace, method, payload),
            f".mqtt_request({namespace},{method},{type(payload)})",
        )

    async def async_http_request_raw(
        self, request: MerossRequest
    ) -> MerossResponse | None:
        if not (http := self._http):
            # even if we're smart enough to not call async_http_request_raw when no http
            # available, it could happen we loose that when asynchronously coming here
            self.log(
                self.DEBUG,
                "Attempting to use async_http_request_raw with no http connection",
            )
            return None

        self._http_lastrequest = time()
        self._trace_or_log(
            self._http_lastrequest,
            request,
            CONF_PROTOCOL_HTTP,
            ConfigEntryManager.TRACE_TX,
        )
        try:
            response = await http.async_request_raw(request.json())
        except TerminatedException:
            return None
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
                return None
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
            if request.namespace is not mn.Appliance_Control_Multiple.name:
                return None
            # try to recover NS_MULTIPLE by discarding the incomplete
            # message at the end
            trunc_pos = response_text.rfind(',{"header":')
            if trunc_pos == -1:
                return None
            response_text = response_text[0:trunc_pos] + "]}}"
            response = MerossResponse(response_text)

        except Exception as exception:
            namespace = request.namespace
            self.log(
                self.DEBUG,
                "HTTP ERROR %s %s (messageId:%s %s:%s)",
                request.method,
                namespace,
                request.messageid,
                exception.__class__.__name__,
                str(exception),
            )
            if not self.online:
                return None

            if namespace is mn.Appliance_System_All.name:
                if self._http_active:
                    self._http_active = None
                    self.sensor_protocol.update_attr_inactive(ProtocolSensor.ATTR_HTTP)
            elif namespace is mn.Appliance_Control_Unbind.name:
                if isinstance(exception, aiohttp.ServerDisconnectedError):
                    # this is expected when issuing the UNBIND
                    # so this is an indication we're dead
                    self._set_offline()

            return None

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
        if self._check_uuid_mismatch(get_message_uuid(response[mc.KEY_HEADER])):
            return None

        self._http_lastresponse = epoch
        if not self._http_active:
            self._http_active = http
            self.sensor_protocol.update_attr_active(ProtocolSensor.ATTR_HTTP)
        if self.curr_protocol is not CONF_PROTOCOL_HTTP:
            if (self.pref_protocol is CONF_PROTOCOL_HTTP) or (not self._mqtt_active):
                self._switch_protocol(CONF_PROTOCOL_HTTP)
        self._receive(epoch, response)
        return response

    async def async_http_request(
        self,
        namespace: str,
        method: str,
        payload: "MerossPayloadType",
    ):
        return await self.async_http_request_raw(
            MerossRequest(self.key, namespace, method, payload, self._topic_response)
        )

    async def async_request_poll(self, handler: NamespaceHandler):
        handler.lastrequest = self._polling_epoch
        handler.polling_epoch_next = handler.lastrequest + handler.polling_period
        if (self._multiple_requests is None) or (
            handler.polling_response_size >= self.device_response_size_max
        ):
            # multiple requests are disabled
            # or this request alone would overflow the device response size limit
            await self.async_request(*handler.polling_request)
            return
        # estimate the size of the multiple response
        multiple_response_size = (
            self._multiple_response_size + handler.polling_response_size
        )
        if multiple_response_size > self.device_response_size_max:
            # this request (together with already previously packed)
            # would overflow the device response size limit
            if not self._multiple_requests:
                # again this request alone would overflow the device response size limit
                await self.async_request(*handler.polling_request)
                return
            # flush the pending multiple requests
            await self._async_multiple_requests_flush()
            multiple_response_size = (
                self._multiple_response_size + handler.polling_response_size
            )
        self._multiple_requests.append(handler.polling_request)
        self._multiple_response_size = multiple_response_size
        if len(self._multiple_requests) >= self.multiple_max:
            await self._async_multiple_requests_flush()

    async def async_request_smartpoll(
        self,
        handler: NamespaceHandler,
        *,
        cloud_queue_max: int = 1,
    ):
        if (
            (self.curr_protocol is CONF_PROTOCOL_MQTT)
            and (self._queued_cloudpoll_requests >= cloud_queue_max)
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

    def request_lazypoll(self, handler: NamespaceHandler):
        """Insert into the lazypoll_requests ordering by least recently polled"""

        def _lazypoll_key(_handler: NamespaceHandler):
            return _handler.lastrequest - self._polling_epoch

        bisect.insort(self._lazypoll_requests, handler, key=_lazypoll_key)

    async def _async_request_updates(self, namespace: str | None):
        """
        This is a 'versatile' polling strategy called on timer
        or when the device comes online (passing in the received namespace)
        'namespace' is 'None' when we're handling a scheduled polling when
        the device is online. When 'namespace' is not 'None' it represents the event
        of the device coming online following a succesful received message. This is
        likely to be 'NS_ALL', since it's the only message we request when offline.
        If we're connected to an MQTT broker anyway it could be any 'PUSH' message.
        We'll use _queued_smartpoll_requests to track how many polls went through
        over MQTT for this cycle in order to only send 1 for each if we're
        binded to a cloud MQTT broker (in order to reduce bursts).
        If a poll request is discarded because of this, it should go through
        on the next polling cycle. This will 'spread' smart requests over
        subsequent polls
        """
        self._lazypoll_requests.clear()
        self._queued_cloudpoll_requests = 0
        # self.namespace_handlers could change at any time due to async
        # message parsing (handlers might be dynamically created by then)
        for handler in [
            handler
            for handler in self.namespace_handlers.values()
            if (handler.ns.name != namespace)
        ]:
            if handler.polling_strategy:
                await handler.polling_strategy(handler)
                if not self.online or self._polling_callback_shutdown:
                    break  # do not return: do the flush first!

        # needed even if offline: it takes care of resetting the ns_multiple state
        if self._multiple_requests:
            await self._async_multiple_requests_flush()

        # when create_diagnostic_entities is True, after onlining we'll dynamically
        # scan the abilities to look for 'unknown' namespaces (kind of like tracing)
        # and try to build diagnostic entitities out of that
        if (
            self._diagnostics_build
            and self.online
            and not self._polling_callback_shutdown
        ):
            self.log(self.DEBUG, "Diagnostic scan begin")
            try:
                abilities = iter(self.descriptor.ability)
                while self.online and not self._polling_callback_shutdown:
                    ability = next(abilities)
                    if (ability in TRACE_ABILITY_EXCLUDE) or (
                        (handler := self.namespace_handlers.get(ability))
                        and (
                            handler.polling_strategy
                            or (
                                (handler.ns.has_get is False)
                                and (handler.ns.has_push_query is False)
                            )
                        )
                    ):
                        continue
                    await self.async_request(*self.NAMESPACES[ability].request_get)
            except StopIteration:
                self._diagnostics_build = False
                self.log(self.DEBUG, "Diagnostic scan end")
            except Exception as exception:
                self._diagnostics_build = False
                self.log_exception(self.WARNING, exception, "diagnostic scan")

    @callback
    async def _async_polling_callback(self, namespace: str | None):
        try:
            self._polling_callback_unsub = None
            self._polling_epoch = epoch = time()
            self.log(self.DEBUG, "Polling begin")
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
                    if await self.async_http_request(
                        *mn.Appliance_System_All.request_get
                    ):
                        namespace = mn.Appliance_System_All.name
                    # going on, should the http come online, the next
                    # async_request_updates will be 'smart' again, skipping
                    # state updates coming through mqtt (since we're still
                    # connected) but now requesting over http as preferred.
                    # Also, we're forcibly passing namespace = NS_ALL to
                    # tell the self._async_request_updates we've already polled that

                if self.mqtt_locallyactive:
                    # implement an heartbeat since mqtt might
                    # be unused for quite a bit
                    if (epoch - self._mqtt_lastresponse) > PARAM_HEARTBEAT_PERIOD:
                        if not await self.async_mqtt_request(
                            *mn.Appliance_System_All.request_get
                        ):
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

                await self._async_request_updates(namespace)

            else:  # offline or 'likely' offline (failed last request)
                ns_all_handler = self.namespace_handlers[mn.Appliance_System_All.name]
                ns_all_response = None
                if self.conf_protocol is CONF_PROTOCOL_AUTO:
                    if self._http:
                        ns_all_response = await self.async_http_request(
                            *ns_all_handler.polling_request
                        )
                    if self._mqtt_publish and not self.online:
                        ns_all_response = await self.async_mqtt_request(
                            *ns_all_handler.polling_request
                        )
                elif self.conf_protocol is CONF_PROTOCOL_MQTT:
                    if self._mqtt_publish:
                        ns_all_response = await self.async_mqtt_request(
                            *ns_all_handler.polling_request
                        )
                else:  # self.conf_protocol is CONF_PROTOCOL_HTTP:
                    if self._http:
                        ns_all_response = await self.async_http_request(
                            *ns_all_handler.polling_request
                        )

                if ns_all_response:
                    ns_all_handler.lastrequest = epoch
                    ns_all_handler.polling_epoch_next = (
                        epoch + ns_all_handler.polling_period
                    )
                    ns_all_handler.polling_response_size = len(ns_all_response.json())
                    await self._async_request_updates(ns_all_handler.ns.name)
                elif self.online:
                    self._set_offline()
                else:
                    if self._polling_delay < PARAM_HEARTBEAT_PERIOD:
                        self._polling_delay += self.polling_period
                    else:
                        self._polling_delay = PARAM_HEARTBEAT_PERIOD
        finally:
            self._polling_epoch = 0.0
            if self._polling_callback_shutdown:
                self._polling_callback_shutdown.set_result(True)
                self._polling_callback_shutdown = None
            else:
                self._polling_callback_unsub = self.schedule_async_callback(
                    self._polling_delay, self._async_polling_callback, None
                )
            self.log(self.DEBUG, "Polling end")

    async def _async_polling_stop(self):
        """Ensure we're not polling nor any schedule is in place."""
        if self._polling_callback_unsub:
            self._polling_callback_unsub.cancel()
            self._polling_callback_unsub = None
        elif self._polling_epoch:
            if not self._polling_callback_shutdown:
                self._polling_callback_shutdown = (
                    asyncio.get_running_loop().create_future()
                )
            await self._polling_callback_shutdown

    async def _async_poll(self):
        """Stops an ongoing poll if any and executes a full poll (like when onlining)."""
        await self._async_polling_stop()
        # before retriggering ensure we're not overlapping with device shutdown
        if self.config_entry.state is ConfigEntryState.LOADED:
            self.device_debug = None
            for handler in self.namespace_handlers.values():
                handler.polling_epoch_next = 0.0
            # this will also restart/schedule the cycle
            await self._async_polling_callback(None)

    def mqtt_receive(self, message: "MerossResponse"):
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

    def mqtt_attached(self, mqtt_connection: "MQTTConnection"):
        assert self.conf_protocol is not CONF_PROTOCOL_HTTP
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
        if _mqtt_connection.profile.allow_mqtt_publish:
            self._mqtt_publish = _mqtt_connection
            if not self.online and self._polling_callback_unsub:
                # reschedule immediately
                self._polling_callback_unsub.cancel()
                self._polling_callback_unsub = self.schedule_async_callback(
                    0, self._async_polling_callback, None
                )
        elif self.conf_protocol is CONF_PROTOCOL_MQTT:
            self.log(
                self.WARNING,
                "MQTT connection doesn't allow publishing - device will not be able send commands",
                timeout=14400,
            )
        self.sensor_protocol.update_attr_active(ProtocolSensor.ATTR_MQTT_BROKER)

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

    def profile_linked(self, profile: "MQTTProfile"):
        if self._profile is not profile:
            if self._profile:
                self._profile.unlink(self)
            self._profile = profile
            self.log(
                self.DEBUG,
                "linked to profile:%s",
                self.loggable_profile_id(profile.id),
            )
            self._check_protocol()

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

    def _check_protocol_ext(self):
        api = self.api
        userId = self.descriptor.userId
        if userId in api.profiles:
            profile = api.profiles[userId]
            if profile and (profile.key != self.key):
                profile = api
        else:
            profile = api
        _profile = self._profile
        if _profile != profile:
            if _profile:
                _profile.unlink(self)
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
        if conf_protocol is CONF_PROTOCOL_HTTP:
            # strictly HTTP so detach MQTT in case
            if _mqtt_connection:
                _mqtt_connection.detach(self)
        else:
            if _mqtt_connection:
                if _mqtt_connection.profile == _profile:
                    return
                _mqtt_connection.detach(self)

            if _profile:
                _profile.attach_mqtt(self)

    def _receive(self, epoch: float, message: MerossResponse):
        """
        default (received) message handling entry point
        """
        self.lastresponse = epoch
        message_size = len(message.json())
        if message_size > self.device_response_size_min:
            self.device_response_size_min = message_size
            if message_size > self.device_response_size_max:
                self.device_response_size_max = message_size

        header = message[mc.KEY_HEADER]
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
            sign = compute_message_signature(
                header[mc.KEY_MESSAGEID], self.key, header[mc.KEY_TIMESTAMP]
            )
            if sign != header[mc.KEY_SIGN]:
                self.log(
                    self.DEBUG,
                    "Received signature error: computed=%s, header=%s",
                    sign,
                    str(self.loggable_dict(header)),
                )

        if not self.online:
            self._set_online()
            self._polling_delay = self.polling_period
            # retrigger the polling loop in case it is scheduled/pending.
            # This could happen when we receive an MQTT message
            if self._polling_callback_unsub:
                self._polling_callback_unsub.cancel()
                self._polling_callback_unsub = self.schedule_async_callback(
                    0,
                    self._async_polling_callback,
                    header[mc.KEY_NAMESPACE],
                )

        return self._handle(header, message[mc.KEY_PAYLOAD])

    def _handle(
        self,
        header: "MerossHeaderType",
        payload: "MerossPayloadType",
    ):
        namespace = header[mc.KEY_NAMESPACE]
        method = header[mc.KEY_METHOD]
        if method == mc.METHOD_GETACK:
            pass
        elif method == mc.METHOD_SETACK:
            # SETACK generally doesn't carry any state/info so it is
            # no use parsing..moreover, our callbacks system is full
            # in place so we have no need to further process
            return
        elif method == mc.METHOD_PUSH:
            # we're saving for diagnostic purposes so we have knowledge of
            # which data the device pushes asynchronously
            self.namespace_pushes[namespace] = payload
        elif method == mc.METHOD_ERROR:
            if payload.get(mc.KEY_ERROR) == mc.ERROR_INVALIDKEY:
                self.log(
                    self.WARNING,
                    "Key error: the configured device key is wrong",
                    timeout=14400,
                )
            else:
                self.log(
                    self.WARNING,
                    "Protocol error: namespace:%s payload:%s",
                    namespace,
                    str(self.loggable_dict(payload)),
                    timeout=14400,
                )
            return

        try:
            handler = self.namespace_handlers[namespace]
        except KeyError:
            # we don't have an handler in place and this is typically due to
            # PUSHES of unknown/unmanaged namespaces
            if not namespace:
                # this weird error appears in an ns_multiple response missing
                # the expected namespace key for "Appliance.Control.Runtime"
                self.log(
                    self.WARNING,
                    "Protocol error: received empty namespace for payload:%s",
                    str(self.loggable_dict(payload)),
                    timeout=14400,
                )
                return
            # here the namespace might be unknown to our definitions (mn.Namespace)
            # so we try, in case, to build a new one with good presets
            handler = self._create_handler(
                self.NAMESPACES.get(namespace)
                or mn.ns_build_from_message(namespace, method, payload, self.NAMESPACES)
            )

        handler.lastresponse = self.lastresponse
        handler.polling_epoch_next = handler.lastresponse + handler.polling_period
        try:
            handler.handler(header, payload)  # type: ignore
        except Exception as exception:
            handler.handle_exception(exception, handler.handler.__name__, payload)

    def _create_handler(self, ns: "mn.Namespace"):
        """Called by the base device message parsing chain when a new
        NamespaceHandler need to be defined (This happens the first time
        the namespace enters the message handling flow)"""
        return NamespaceHandler(self, ns)

    def _handle_Appliance_Config_Info(self, header: dict, payload: dict):
        """{"info":{"homekit":{"model":"MSH300HK","sn":"#","category":2,"setupId":"#","setupCode":"#","uuid":"#","token":"#"}}}"""
        pass

    def _handle_Appliance_Control_Bind(self, header: dict, payload: dict):
        # already processed by the MQTTConnection session manager
        pass

    def _handle_Appliance_System_Ability(self, header: dict, payload: dict):
        pass

    def _handle_Appliance_System_All(self, header: dict, payload: dict):
        # see issue #341. In case we receive a formally correct response from a
        # mismatched device we should stop everything and obviously don't update our
        # ConfigEntry. Here we check first the identity of the device sending this payload
        # in order to not mess our configuration. All in all this check should be not
        # needed since the only reasonable source of 'device mismatch' is the HTTP protocol
        # which is already guarded in our async_http_request
        if self._check_uuid_mismatch(
            payload[mc.KEY_ALL][mc.KEY_SYSTEM][mc.KEY_HARDWARE][mc.KEY_UUID]
        ):
            return
        else:
            self.remove_issue(mlc.ISSUE_DEVICE_ID_MISMATCH)

        descr = self.descriptor
        oldfirmware = descr.firmware
        oldtimezone = descr.timezone
        descr.update(payload)

        if oldtimezone != descr.timezone:
            self.needsave = True

        if oldfirmware != descr.firmware:
            self.needsave = True
            query_abilities = True
            if update_firmware := self.update_firmware:
                # self.update_firmware is dynamically created only when the cloud api
                # reports a newer fw
                update_firmware.installed_version = descr.firmwareVersion
                update_firmware.flush_state()
            if (
                self.conf_protocol is not CONF_PROTOCOL_MQTT
                and not self.config.get(CONF_HOST)
                and (host := descr.innerIp)
            ):
                # dynamically adjust the http host in case our config misses it and
                # we're so depending on MQTT updates of descriptor.firmware to innerIp
                if _http := self._http:
                    _http.host = host
                else:
                    self._http = MerossHttpClient(host, self.key)
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
                    self.request(mn.Appliance_System_Debug.request_default)
            else:
                self.device_debug = None

        for key_digest, _digest in descr.digest.items() or descr.control.items():
            self.digest_handlers[key_digest](_digest)

        if self.needsave:
            self.schedule_entry_update(query_abilities)

    def _handle_Appliance_System_Clock(self, header: dict, payload: dict):
        # already processed by the MQTTConnection session manager
        pass

    def _handle_Appliance_System_Debug(self, header: dict, payload: dict):
        # this ns is queried when we're HTTP connected and the device reports it is
        # also MQTT connected but meross_lan has no confirmation (_mqtt_active == None)
        # we're then going to inspect the device reported broker and see if
        # our config allow to connect
        self.device_debug = p_debug = payload[mc.KEY_DEBUG]
        broker = get_active_broker(p_debug)
        mqtt_connection = self._mqtt_connection
        if mqtt_connection:
            if mqtt_connection.broker.host == broker.host:
                if self._mqtt_connected and not self._mqtt_active:
                    self._mqtt_active = mqtt_connection
                    self.sensor_protocol.update_attr_active(ProtocolSensor.ATTR_MQTT)
                    if self.curr_protocol is not self.pref_protocol:
                        self._switch_protocol(self.pref_protocol)
                return
            mqtt_connection.detach(self)

    def _handle_Appliance_System_Online(self, header: dict, payload: dict):
        # already processed by the MQTTConnection session manager
        pass

    def _handle_Appliance_System_Report(self, header: dict, payload: dict):
        # No clue: sent (MQTT PUSH) by the device on initial connection
        pass

    def _handle_Appliance_System_Time(self, header: dict, payload: dict):
        self.descriptor.update_time(payload[mc.KEY_TIME])
        self.schedule_entry_update(False)

    def _config_device_timestamp(self, epoch):
        if self.mqtt_locallyactive and (
            mn.Appliance_System_Clock.name in self.descriptor.ability
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

    async def async_config_device_timezone(self, tzname: str | None):
        # assert self.mqtt_locallyactive
        timerules: list[list[int]]
        if tzname:
            # we'll look through the list of transition times for current tz
            # and provide the actual (last past daylight) and the next to the
            # appliance so it knows how and when to offset utc to localtime

            # brutal patch for missing tz names (AEST #402)
            _TZ_PATCH = {
                "AEST": "Australia/Brisbane",
            }
            if tzname in _TZ_PATCH:
                tzname = _TZ_PATCH[tzname]

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

                timerules = await self.hass.async_add_executor_job(_build_timerules)

            except Exception as exception:
                self.log_exception(
                    self.WARNING,
                    exception,
                    "building timezone(%s) info for %s",
                    tzname,
                    mn.Appliance_System_Time.name,
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

        if await self.async_request_ack(
            mn.Appliance_System_Time.name,
            mc.METHOD_SET,
            payload={mn.Appliance_System_Time.key: p_time},
        ):
            self.descriptor.update_time(p_time)
            self.schedule_entry_update(False)
            return True

        return False

    def _switch_protocol(self, protocol):
        self.log(
            self.DEBUG,
            "Switching protocol to %s",
            protocol,
        )
        self.curr_protocol = protocol
        if self.online:
            self.sensor_protocol.set_available()

    def _update_config(self):
        """
        common properties caches, read from ConfigEntry on __init__ or when a configentry updates
        """
        config = self.config
        self.conf_protocol = mlc.CONF_PROTOCOL_OPTIONS.get(
            config.get(mlc.CONF_PROTOCOL), CONF_PROTOCOL_AUTO
        )
        self.polling_period = (
            config.get(mlc.CONF_POLLING_PERIOD) or mlc.CONF_POLLING_PERIOD_DEFAULT
        )
        if self.polling_period < mlc.CONF_POLLING_PERIOD_MIN:
            self.polling_period = mlc.CONF_POLLING_PERIOD_MIN
        self._polling_delay = self.polling_period

        if config.get(mlc.CONF_DISABLE_MULTIPLE):
            self.disable_multiple()
        else:
            self.enable_multiple()

        _http = self._http
        host = self.host
        if (self.conf_protocol is CONF_PROTOCOL_MQTT) or (not host):
            # no room for http transport...
            if _http:
                _http.terminate()
                self._http = self._http_active = None
                self.sensor_protocol.update_attr_inactive(ProtocolSensor.ATTR_HTTP)
        else:
            # we need http: setup/update
            if _http:
                _http.host = host
                _http.key = self.key
            else:
                _http = self._http = MerossHttpClient(host, self.key)
            _http.set_encryption(
                compute_message_encryption_key(
                    self.descriptor.uuid, self.key, self.descriptor.macAddress
                ).encode("utf-8")
                if mn.Appliance_Encrypt_ECDHE.name in self.descriptor.ability
                else None
            )

    def _check_uuid_mismatch(self, response_uuid: str):
        """when detecting a wrong uuid from a response we offline the device"""
        if response_uuid != self.id:
            # here we're not obfuscating device uuid since we might have an hard time identifying the bogus one
            self.log(
                self.CRITICAL,
                "Received a response from a mismatching device (received uuid:%s, configured uuid:%s)",
                response_uuid,
                self.id,
                timeout=900,
            )
            if self.online:
                self._set_offline()
            self.create_issue(
                mlc.ISSUE_DEVICE_ID_MISMATCH,
                severity=self.IssueSeverity.CRITICAL,
                translation_placeholders={"device_name": self.name},
            )
            return True
        return False

    def update_device_info(self, device_info: "DeviceInfoType"):
        api = self.api
        self.device_info = device_info
        name = device_info.get(mc.KEY_DEVNAME) or self._get_internal_name()
        if name != self.device_registry_entry.name:
            api.device_registry.async_update_device(
                self.device_registry_entry.id, name=name
            )
        channel = -1
        async_update_entity = api.entity_registry.async_update_entity
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

    def update_latest_version(self, latest_version: "LatestVersionType"):
        if update_firmware := self.update_firmware:
            update_firmware.installed_version = self.descriptor.firmwareVersion
            update_firmware.latest_version = latest_version.get(mc.KEY_VERSION)
            update_firmware.release_summary = latest_version.get(mc.KEY_DESCRIPTION)
            update_firmware.flush_state()
        else:
            self.update_firmware = MLUpdate(self, latest_version)

    async def async_get_diagnostics_trace(self) -> list:
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

        if self._http_active and self.conf_protocol is not CONF_PROTOCOL_MQTT:
            # shortcut with fast HTTP querying
            epoch = time()
            descr = self.descriptor
            # setting _trace_data will already activate tracing (kind of)
            self._trace_data = trace_data = [
                ["time", "rxtx", "protocol", "method", "namespace", "data"]
            ]
            # TODO: remove these from diagnostic since they're present in header data
            # before that we need to update the emulator code to extract those
            self.trace(epoch, descr.all, mn.Appliance_System_All.name)
            self.trace(epoch, descr.ability, mn.Appliance_System_Ability.name)
            await self._async_poll()
            try:
                abilities = iter(descr.ability)
                while self.online and self.is_tracing:
                    ability = next(abilities)
                    if ability not in TRACE_ABILITY_EXCLUDE:
                        await self.get_handler_by_name(ability).async_trace(
                            self.async_http_request
                        )

                return trace_data  # might be truncated because offlining or async shutting trace
            except StopIteration:
                return trace_data
            except Exception as exception:
                self.log_exception(self.DEBUG, exception, "async_get_diagnostics_trace")
                # in case of error we're going to try the legacy approach
            finally:
                self._trace_data = None

        self._trace_data = [["time", "rxtx", "protocol", "method", "namespace", "data"]]
        self._trace_future = future = asyncio.get_running_loop().create_future()
        await self.async_trace_open()
        return await future

    async def _async_trace_ability(self, abilities_iterator: "Iterator[str]"):
        try:
            # avoid interleave tracing ability with polling loop
            # also, since we could trigger this at early stages
            # in device init, this check will prevent iterating
            # at least until the device fully initialize through
            # self.start()
            if self._polling_callback_unsub and self.online:
                while (ability := next(abilities_iterator)) in TRACE_ABILITY_EXCLUDE:
                    continue
                self.log(self.DEBUG, "Tracing %s ability", ability)
                await self.get_handler_by_name(ability).async_trace(self.async_request)

        except StopIteration:
            self._trace_ability_callback_unsub = None
            self.log(self.DEBUG, "Tracing abilities end")
            return
        except Exception as exception:
            self.log_exception(self.WARNING, exception, "_async_trace_ability")

        self._trace_ability_callback_unsub = None
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
        self,
        epoch: float,
        message: "MerossMessage",
        protocol: str,
        rxtx: str,
    ):
        if self.is_tracing:
            header = message[mc.KEY_HEADER]
            self.trace(
                epoch,
                message[mc.KEY_PAYLOAD],
                header[mc.KEY_NAMESPACE],
                header[mc.KEY_METHOD],
                protocol,
                rxtx,
            )
        # here we avoid using self.log since it would
        # log to the trace file too but we've already 'traced' the
        # message if that's the case
        logger = self.logger
        if logger.isEnabledFor(self.VERBOSE):
            header = message[mc.KEY_HEADER]
            logger._log(
                self.VERBOSE,
                "%s(%s) %s %s (messageId:%s) %s",
                (
                    rxtx,
                    protocol,
                    header[mc.KEY_METHOD],
                    header[mc.KEY_NAMESPACE],
                    header[mc.KEY_MESSAGEID],
                    json_dumps(self.loggable_dict(message)),
                ),
            )
        elif logger.isEnabledFor(self.DEBUG):
            header = message[mc.KEY_HEADER]
            logger._log(
                self.DEBUG,
                "%s(%s) %s %s (messageId:%s)",
                (
                    rxtx,
                    protocol,
                    header[mc.KEY_METHOD],
                    header[mc.KEY_NAMESPACE],
                    header[mc.KEY_MESSAGEID],
                ),
            )

    async def _async_button_refresh_press(self):
        """Forces a full poll."""
        await self._async_poll()

    async def _async_button_reload_press(self):
        """Reload the config_entry."""
        self.schedule_reload()
