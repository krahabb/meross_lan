"""
Descriptors for namespaces management.
This file contains the knowledge about how namespaces work (their syntax and behaviors).
"""

import enum
from functools import cached_property
import re
import typing

from . import const as mc

if typing.TYPE_CHECKING:
    from typing import Final, Mapping, NotRequired, TypedDict, Unpack

    from . import MerossRequestType

    type NamespacesMapType = Mapping[str, "Namespace"]
    NAMESPACES: Final[NamespacesMapType]
    HUB_NAMESPACES: Final[NamespacesMapType]


class _NamespacesMap(dict):
    """
    Default general map of Namespace(s).
    This map is populated with a set of static (known) definitions but could also be
    updated at runtime when a new undefined namespace enter the device message pipe.
    """

    def __getitem__(self, namespace: str) -> "Namespace":
        try:
            return dict.__getitem__(self, namespace)
        except KeyError:
            return _ns_unknown(namespace)

    def get(self, namespace: str) -> "Namespace | None":
        try:
            return dict.__getitem__(self, namespace)
        except KeyError:
            return None


NAMESPACES = _NamespacesMap()


class _HubNamespacesMap(dict):
    """
    This map is specific for Hub devices so that we can 'override' some Namespace(s) when
    their default (standard device) based behavior could differ when managed in a Hub.
    Examples are Appliance.Control.Sensor.LatestX and HistoryX.
    If a namespace is not found here, it will be looked-up in the default NAMESPACES map
    and eventually created there. Beware this is not the same meaning of Namespace property 'is_hub'.
    TODO: for 'sensor' type namespaces we need to manage the case when they're dynamically built
    at runtime since, when this happens they would be placed in NAMESPACES even if the device is an Hub since
    we're not managing this kind of context when dynamically adding devices (see device message handling pipe).
    If they follow the same heuristics as those actually known, they should be placed in HUB_NAMESPACES and
    key_channel = mc.KEY_SUBID with payload type = LIST_C
    """

    def __getitem__(self, namespace: str) -> "Namespace":
        try:
            return dict.__getitem__(self, namespace)
        except KeyError:
            return NAMESPACES[namespace]

    def get(self, namespace: str) -> "Namespace | None":
        try:
            return dict.__getitem__(self, namespace)
        except KeyError:
            return NAMESPACES.get(namespace)


HUB_NAMESPACES = _HubNamespacesMap()


class PayloadType(enum.Enum):
    """Depicts the payload structure in GET queries (defaults to DICT in case)."""

    NONE = {}
    """No GET query supported."""
    DICT = {}
    """Command GET with an empty dict (should) return all the (channels) state.
    This is used as default unless some heuristic (in __init__) states something different."""
    DICT_C = {mc.KEY_CHANNEL: 0}
    """Command GET with channel index in dict returns the state requested."""
    LIST = []
    """Command GET with an empty list returns all the (channels) state."""
    LIST_C = [{mc.KEY_CHANNEL: 0}]
    """Command GET with channel index dicts in a list returns the states requested."""


class Grammar(enum.StrEnum):
    UNKNOWN = enum.auto()
    """Used to mark namespaces which still don't have a proper normalization (new/unknown ones)."""
    EXPERIMENTAL = enum.auto()
    """Used to mark namespaces for which our normalization is not 100% sure.
    During tracing we'll check every possible verb/payload format despite our knowledge."""
    STABLE = enum.auto()
    """Used to mark namespaces for which our normalization is about 100% correct and complete."""


class Namespace:
    """
    Namespace descriptor helper class. This is used to build a definition
    of namespace behaviors and syntax.
    """

    if typing.TYPE_CHECKING:

        class FactoryArgs(TypedDict):
            """FactoryArgs are often mututally exclusive and allow for a cascading of heuristics.
            For example, setting is_hub_id=True will automatically apply 'map' 'payload'
            'is_hub_namespace' 'key_channel'. The code in __init__ might seem a bit complex
            but tries to be as tidy (and efficient) as possible in order to be 'guided' by
            the type and value of parameters applied."""

            map: NotRequired[NamespacesMapType]
            grammar: NotRequired[Grammar]
            payload: NotRequired[PayloadType | None]
            is_hub_id: NotRequired[bool]
            is_hub_subid: NotRequired[bool]
            is_sensor: NotRequired[bool]
            is_thermostat: NotRequired[bool]

        class Args(FactoryArgs):
            """These Args are typically 'customized' in factory functions so to declare
            our well known namespaces in a possibly compact way."""

            has_get: NotRequired[bool | None]
            has_set: NotRequired[bool | None]
            has_push: NotRequired[bool | None]
            has_push_query: NotRequired[bool | None]

        DEFAULT_PUSH_PAYLOAD: Final
        name: Final[str]
        """The namespace name"""
        key: Final[str]
        """The root key of the payload"""
        key_channel: Final[str]
        """The key used to index items in list payloads"""
        request_payload_type: Final[PayloadType]
        grammar: Final[Grammar]

        # These indicate support for the corresponding verb. If None we have no clue
        has_get: Final[bool | None]  # type: ignore
        has_set: Final[bool | None]  # type: ignore
        has_push: Final[bool | None]  # type: ignore
        has_push_query: Final[bool | None]  # type: ignore

        is_hub_namespace: Final[bool]  # type: ignore
        """This is an indication the namespace is for subdevices (key_channel could be "id" or "subId")"""
        is_hub_id: Final[bool]  # type: ignore
        is_hub_subid: Final[bool]  # type: ignore
        is_sensor: Final[bool]  # type: ignore
        is_thermostat: Final[bool]  # type: ignore

    DEFAULT_PUSH_PAYLOAD = PayloadType.DICT.value

    __slots__ = (
        "name",
        "key",
        "key_channel",
        "has_get",
        "has_set",
        "has_push",
        "has_push_query",
        "payload_get_type",
        "is_hub_namespace",
        "is_hub_id",
        "is_hub_subid",
        "is_sensor",
        "is_thermostat",
        "grammar",
        "__dict__",
    )

    def __init__(
        self,
        name: str,
        key: str | None = None,
        /,
        **kwargs: "Unpack[Args]",
    ) -> None:
        self.name = name
        if key:
            self.key = key
        else:
            key = name.split(".")[-1]
            # mainly camelCasing the last split of the namespace
            # with special care for also the last char which looks
            # lowercase when it's a X (i.e. ToggleX -> togglex)
            if key[-1] == "X":
                self.key = "".join((key[0].lower(), key[1:-1], "x"))
            else:
                self.key = "".join((key[0].lower(), key[1:]))

        map = kwargs.get("map", NAMESPACES)
        payload = kwargs.get("payload")
        self.grammar = kwargs.get("grammar", Grammar.STABLE)
        for _attr in ("has_get", "has_set", "has_push", "has_push_query"):
            setattr(self, _attr, kwargs.get(_attr, None))
        for _attr in ("is_hub_id", "is_hub_subid", "is_thermostat", "is_sensor"):
            setattr(self, _attr, kwargs.get(_attr, False))

        if self.is_hub_id:
            self.key_channel = mc.KEY_ID
            payload = PayloadType.LIST
        elif self.is_hub_subid:
            self.key_channel = mc.KEY_SUBID
            payload = PayloadType.LIST_C
        elif self.is_thermostat:
            self.key_channel = mc.KEY_CHANNEL
            payload = PayloadType.LIST_C
        elif self.is_sensor:
            # we want to better investigate the behavior of this class of ns
            self.grammar = Grammar.EXPERIMENTAL
            if map is HUB_NAMESPACES:
                self.is_hub_subid = True
                self.key_channel = mc.KEY_SUBID
                payload = PayloadType.LIST_C
            else:
                # This is tricky since we still don't really know.
                # It seems *.History and Latest namespaces were queried by LIST_C
                # while *.HistoryX and LatestX don't work actually.
                self.key_channel = mc.KEY_CHANNEL
                payload = payload or PayloadType.DICT_C
        elif not payload:
            # Don't pass any payload arg if we want to automatically
            # apply heuristics to incoming new namespaces.
            # Forwarding a payload arg to the constructor is just an 'hint'
            # used by our factory functions ('_ns_xxx') to skip unneded name parsing
            match name.split("."):
                case (_, "Hub", *_):
                    self.is_hub_id = True
                    self.key_channel = mc.KEY_ID
                    payload = PayloadType.LIST
                case (_, "RollerShutter", *_):
                    self.key_channel = mc.KEY_CHANNEL
                    payload = PayloadType.LIST
                case (_, "Control", "Screen", *_):
                    self.key_channel = mc.KEY_CHANNEL
                    payload = PayloadType.LIST_C
                case (_, "Control", "Sensor", *_):
                    self.is_sensor = True
                    if map is HUB_NAMESPACES:
                        self.is_hub_subid = True
                        self.key_channel = mc.KEY_SUBID
                        payload = PayloadType.LIST_C
                    else:
                        self.key_channel = mc.KEY_CHANNEL
                        payload = PayloadType.DICT_C
                case (_, "Control", "Thermostat", *_):
                    self.is_thermostat = True
                    self.key_channel = mc.KEY_CHANNEL
                    payload = PayloadType.LIST_C
                case _:
                    self.key_channel = mc.KEY_CHANNEL
                    payload = PayloadType.DICT

        else:
            self.key_channel = mc.KEY_CHANNEL

        self.request_payload_type = payload

        if self.is_hub_id or self.is_hub_subid:
            self.is_hub_namespace = True
            HUB_NAMESPACES[name] = self  # type: ignore
        else:
            self.is_hub_namespace = False
            map[name] = self  # type: ignore


    """
    @cached_property
    def is_hub_id(self):
        return bool(re.match(r"Appliance\.Hub\.(.*)", self.name))

    @cached_property
    def is_sensor(self):
        return bool(re.match(r"Appliance\.Control\.Sensor\.(.*)", self.name))

    @cached_property
    def is_thermostat(self):
        return bool(re.match(r"Appliance\.Control\.Thermostat\.(.*)", self.name))
    """

    @cached_property
    def payload_get(self) -> dict[str, dict | list]:
        """
        Returns a default structured payload for method GET.
        When we query a device 'namespace' with a GET method the request payload
        is usually 'well structured' (more or less). We have a dictionary of
        well-known payloads else we'll use some heuristics
        """
        return {self.key: self.request_payload_type.value}

    @cached_property
    def request_default(self) -> "MerossRequestType":
        if self.has_push_query or (self.has_get is False):
            return self.request_push
        else:
            return self.request_get

    @cached_property
    def request_get(self) -> "MerossRequestType":
        return self.name, mc.METHOD_GET, self.payload_get

    @cached_property
    def request_push(self) -> "MerossRequestType":
        return self.name, mc.METHOD_PUSH, Namespace.DEFAULT_PUSH_PAYLOAD


def ns_build_from_message(
    namespace: str, method: str, payload: dict, map: "NamespacesMapType", /
):
    # we hope the first key in the payload is the 'namespace key'
    for ns_key in payload.keys():
        break
    else:
        ns_key = None

    return Namespace(
        namespace,
        ns_key,
        grammar=Grammar.UNKNOWN,
        has_get=(method == mc.METHOD_GETACK) or None,
        has_set=(method == mc.METHOD_SETACK) or None,
        has_push=(method == mc.METHOD_PUSH) or None,
        map=map,
    )


def _ns_unknown(name: str, /):
    """Builds a definition for a namespace without specific knowledge of supported methods"""
    return Namespace(name, None, grammar=Grammar.UNKNOWN)


def _ns_no_query(
    name: str,
    key: str | None = None,
    /,
    **kwargs: "Unpack[Namespace.Args]",
):
    """Builds a definition for a namespace not supporting GET,SET,PUSH"""
    kwargs["has_get"] = False
    kwargs["has_set"] = False
    kwargs["has_push"] = False
    kwargs["has_push_query"] = False
    kwargs["payload"] = PayloadType.NONE
    return Namespace(name, key, **kwargs)


def _ns_get(
    name: str,
    key: str,
    _payload: PayloadType | None = PayloadType.DICT,
    /,
    **kwargs: "Unpack[Namespace.Args]",
):
    """Builds a definition for a namespace supporting only GET queries (no PUSH)"""
    kwargs["has_get"] = True
    kwargs["has_set"] = False
    kwargs["has_push"] = False
    kwargs["has_push_query"] = False
    kwargs["payload"] = _payload
    return Namespace(name, key, **kwargs)


def _ns_get_set(
    name: str,
    key: str | None,
    _payload: PayloadType | None = PayloadType.DICT,
    /,
    **kwargs: "Unpack[Namespace.Args]",
):
    kwargs["has_get"] = True
    kwargs["has_set"] = True
    kwargs["has_push"] = False
    kwargs["has_push_query"] = False
    kwargs["payload"] = _payload
    return Namespace(name, key, **kwargs)


def _ns_get_set_push(
    name: str,
    key: str | None = None,
    _payload: PayloadType | None = PayloadType.DICT,
    /,
    **kwargs: "Unpack[Namespace.Args]",
):
    """Builds a definition for a namespace supporting only GET queries (no PUSH)"""
    kwargs["has_get"] = True
    kwargs["has_set"] = True
    kwargs["has_push"] = True
    kwargs["has_push_query"] = False
    kwargs["payload"] = _payload
    return Namespace(name, key, **kwargs)

def _ns_get_set_push_query(
    name: str,
    key: str | None = None,
    _payload: PayloadType | None = PayloadType.DICT,
    /,
    **kwargs: "Unpack[Namespace.Args]",
):
    """Builds a definition for a namespace supporting only GET queries (no PUSH)"""
    kwargs["has_get"] = True
    kwargs["has_set"] = True
    kwargs["has_push"] = True
    kwargs["has_push_query"] = True
    kwargs["payload"] = _payload
    return Namespace(name, key, **kwargs)

def _ns_get_push(
    name: str,
    key: str | None = None,
    _payload: PayloadType | None = PayloadType.DICT,
    /,
    **kwargs: "Unpack[Namespace.Args]",
):
    """Builds a definition for a namespace supporting GET queries (which also PUSHes updates)"""
    kwargs["has_get"] = True
    kwargs["has_set"] = False
    kwargs["has_push"] = True
    kwargs["has_push_query"] = False
    kwargs["payload"] = _payload
    return Namespace(name, key, **kwargs)


def _ns_set(
    name: str,
    key: str | None = None,
    /,
    **kwargs: "Unpack[Namespace.Args]",
):
    """Builds a definition for a namespace supporting only SET."""
    kwargs["has_get"] = False
    kwargs["has_set"] = True
    kwargs["has_push"] = False
    kwargs["has_push_query"] = False
    kwargs["payload"] = PayloadType.NONE
    return Namespace(name, key, **kwargs)


def _ns_set_push_query(
    name: str,
    key: str | None = None,
    /,
    **kwargs: "Unpack[Namespace.Args]",
):
    """Builds a definition for a namespace supporting only SET."""
    kwargs["has_get"] = False
    kwargs["has_set"] = True
    kwargs["has_push"] = True
    kwargs["has_push_query"] = True
    kwargs["payload"] = PayloadType.NONE
    return Namespace(name, key, **kwargs)


def _ns_push(name: str, key: str | None = None, /):
    """Builds a definition for a namespace only receiving PUSH."""
    return Namespace(
        name,
        key,
        payload=PayloadType.NONE,
        has_get=False,
        has_set=False,
        has_push=True,
        has_push_query=False,
    )


def _ns_push_query(name: str, key: str | None = None, /):
    """Builds a definition for a namespace supporting PUSH queries/replies (no GET)"""
    return Namespace(
        name,
        key,
        payload=PayloadType.NONE,
        has_get=False,
        has_set=False,
        has_push=True,
        has_push_query=True,
    )


# We predefine grammar for some widely used and well known namespaces either to skip 'euristics'
# and time consuming evaluation.
# Moreover, for some namespaces, the euristics about 'namespace key' and payload structure are not
# good so we must fix those beforehand.
Appliance_System_Ability = _ns_get("Appliance.System.Ability", mc.KEY_ABILITY)
Appliance_System_All = _ns_get("Appliance.System.All", mc.KEY_ALL)
Appliance_System_Clock = _ns_push_query("Appliance.System.Clock", mc.KEY_CLOCK)
Appliance_System_Debug = _ns_get("Appliance.System.Debug", mc.KEY_DEBUG)
Appliance_System_DNDMode = _ns_get("Appliance.System.DNDMode", mc.KEY_DNDMODE)
Appliance_System_Firmware = _ns_get("Appliance.System.Firmware", mc.KEY_FIRMWARE)
Appliance_System_Hardware = _ns_get("Appliance.System.Hardware", mc.KEY_HARDWARE)
Appliance_System_Online = _ns_get_push(
    "Appliance.System.Online", mc.KEY_ONLINE, PayloadType.DICT
)
Appliance_System_Report = _ns_push("Appliance.System.Report", mc.KEY_REPORT)
Appliance_System_Runtime = _ns_get("Appliance.System.Runtime", mc.KEY_RUNTIME)
Appliance_System_Time = _ns_get_push(
    "Appliance.System.Time", mc.KEY_TIME, PayloadType.DICT
)
Appliance_System_Position = _ns_get("Appliance.System.Position", mc.KEY_POSITION)

Appliance_Config_DeviceCfg = _ns_get_push(
    "Appliance.Config.DeviceCfg", mc.KEY_CONFIG, PayloadType.LIST_C
) # mts300
Hub_Config_DeviceCfg = _ns_get_set_push(
    "Appliance.Config.DeviceCfg", mc.KEY_CONFIG, is_hub_subid=True
) # ms130
Appliance_Config_Key = _ns_set("Appliance.Config.Key", mc.KEY_KEY)
Appliance_Config_OverTemp = _ns_get("Appliance.Config.OverTemp", mc.KEY_OVERTEMP)
Appliance_Config_Trace = _ns_no_query("Appliance.Config.Trace")
Appliance_Config_Wifi = _ns_no_query("Appliance.Config.Wifi")
Appliance_Config_WifiList = _ns_no_query("Appliance.Config.WifiList")
Appliance_Config_WifiX = _ns_no_query("Appliance.Config.WifiX")

Appliance_Config_Sensor_Association = _ns_get_set_push_query(
    "Appliance.Config.Sensor.Association",
    mc.KEY_CONFIG,
    PayloadType.LIST_C,
    is_sensor=True,
)
Hub_Config_Sensor_Association = _ns_get_set_push(
    "Appliance.Config.Sensor.Association",
    mc.KEY_CONFIG,
    is_sensor=True,
    map=HUB_NAMESPACES,
) # Not seen really..just an extrapolation for Hub(s)

Appliance_Control_AlertConfig = _ns_get_set_push_query(
    "Appliance.Control.AlertConfig", mc.KEY_CONFIG, PayloadType.LIST_C
) # mts300 support the full set of verbs - em06 also exposes it but that's likely different
Appliance_Control_AlertReport = _ns_get_set(
    "Appliance.Control.AlertReport", mc.KEY_REPORT, PayloadType.LIST_C
) # no PUSH seen..not correctly traced..though 'alertReport' as an ns key seems wrong
Appliance_Control_Bind = _ns_no_query("Appliance.Control.Bind", mc.KEY_BIND)
Appliance_Control_ConsumptionConfig = _ns_get(
    "Appliance.Control.ConsumptionConfig", mc.KEY_CONFIG
)
Appliance_Control_ConsumptionH = _ns_get(
    "Appliance.Control.ConsumptionH", mc.KEY_CONSUMPTIONH, PayloadType.LIST_C
)
Appliance_Control_ConsumptionX = _ns_get_push(
    "Appliance.Control.ConsumptionX", mc.KEY_CONSUMPTIONX, PayloadType.LIST
)
Appliance_Control_Diffuser_Light = _ns_get_set_push(
    "Appliance.Control.Diffuser.Light", mc.KEY_LIGHT
)
Appliance_Control_Diffuser_Sensor = _ns_get_push(
    "Appliance.Control.Diffuser.Sensor", mc.KEY_SENSOR, PayloadType.DICT
)
Appliance_Control_Diffuser_Spray = _ns_get_set_push(
    "Appliance.Control.Diffuser.Spray", mc.KEY_SPRAY
)
Appliance_Control_Electricity = _ns_get_push(
    "Appliance.Control.Electricity", mc.KEY_ELECTRICITY, PayloadType.DICT
)
Appliance_Control_ElectricityX = _ns_get_push(
    "Appliance.Control.ElectricityX",
    mc.KEY_ELECTRICITY,
    PayloadType.LIST_C,
    grammar=Grammar.EXPERIMENTAL,
)
Appliance_Control_Fan = _ns_get_set(
    "Appliance.Control.Fan", mc.KEY_FAN, PayloadType.DICT
)
Appliance_Control_FilterMaintenance = _ns_push_query(
    "Appliance.Control.FilterMaintenance", mc.KEY_FILTER
)
Appliance_Control_Light = _ns_get_set_push("Appliance.Control.Light", mc.KEY_LIGHT)
Appliance_Control_Light_Effect = _ns_get_set(
    "Appliance.Control.Light.Effect", mc.KEY_EFFECT, PayloadType.LIST
)
Appliance_Control_Mp3 = _ns_get_set_push(
    "Appliance.Control.Mp3", mc.KEY_MP3, PayloadType.DICT
)
Appliance_Control_Multiple = _ns_set("Appliance.Control.Multiple", mc.KEY_MULTIPLE)
Appliance_Control_OverTemp = _ns_get(
    "Appliance.Control.OverTemp", mc.KEY_OVERTEMP, PayloadType.LIST
)
Appliance_Control_PhysicalLock = _ns_set_push_query(
    "Appliance.Control.PhysicalLock", mc.KEY_LOCK
)
Appliance_Control_Presence_Config = _ns_get(
    "Appliance.Control.Presence.Config", mc.KEY_CONFIG, PayloadType.LIST_C
)
Appliance_Control_Presence_Study = _ns_push_query(
    "Appliance.Control.Presence.Study", mc.KEY_CONFIG
)
Appliance_Control_Screen_Brightness = _ns_get_set_push(
    "Appliance.Control.Screen.Brightness", mc.KEY_BRIGHTNESS, PayloadType.LIST_C
)
Appliance_Control_Sensor_Association = _ns_get(
    "Appliance.Control.Sensor.Association",
    mc.KEY_CONTROL,
    PayloadType.LIST,
    is_sensor=True,
)  # history of sensor values
Appliance_Control_Sensor_History = _ns_get(
    "Appliance.Control.Sensor.History",
    mc.KEY_HISTORY,
    PayloadType.LIST_C,
    is_sensor=True,
)  # history of sensor values
Appliance_Control_Sensor_Latest = _ns_get_push(
    "Appliance.Control.Sensor.Latest", mc.KEY_LATEST, PayloadType.LIST_C, is_sensor=True
)  # carrying miscellaneous sensor values (temp/humi)
# Appliance.Control.Sensor.* appear on both regular devices (ms600) and hub/subdevices (ms130)
# To distinguish the grammar between regular devices and hubs we save different definitions
# in NAMESPACES (for regular devices) and in HUB_NAMESPACES (for hubs).
# For regular devices, even if traces show presence of values at channel 0,
# the 'LIST_C' query format doesn't work
# We so try introduce a new payload type 'DICT_C'. PUSH query too seems to not work.
# See _ns_get_sensor to get some clues.
Appliance_Control_Sensor_HistoryX = _ns_get(
    "Appliance.Control.Sensor.HistoryX",
    mc.KEY_HISTORY,
    PayloadType.DICT_C,
    is_sensor=True,
)
Hub_Control_Sensor_HistoryX = _ns_get(
    "Appliance.Control.Sensor.HistoryX",
    mc.KEY_HISTORY,
    is_sensor=True,
    map=HUB_NAMESPACES,
)
Appliance_Control_Sensor_LatestX = _ns_get_push(
    "Appliance.Control.Sensor.LatestX",
    mc.KEY_LATEST,
    PayloadType.DICT_C,
    is_sensor=True,
)
Hub_Control_Sensor_LatestX = _ns_get_push(
    "Appliance.Control.Sensor.LatestX",
    mc.KEY_LATEST,
    is_sensor=True,
    map=HUB_NAMESPACES,
)
Appliance_Control_Spray = _ns_get_set_push(
    "Appliance.Control.Spray", mc.KEY_SPRAY, PayloadType.DICT
)
Appliance_Control_TempUnit = _ns_get(
    "Appliance.Control.TempUnit", mc.KEY_TEMPUNIT, PayloadType.LIST_C
)
Appliance_Control_Thermostat_Alarm = _ns_get_push(
    "Appliance.Control.Thermostat.Alarm", mc.KEY_ALARM, is_thermostat=True
)
Appliance_Control_Thermostat_AlarmConfig = _ns_get_set(
    "Appliance.Control.Thermostat.AlarmConfig", mc.KEY_ALARMCONFIG, is_thermostat=True
)
Appliance_Control_Thermostat_Calibration = _ns_get_set(
    "Appliance.Control.Thermostat.Calibration", mc.KEY_CALIBRATION, is_thermostat=True
)
Appliance_Control_Thermostat_CompressorDelay = _ns_get_set(
    "Appliance.Control.Thermostat.CompressorDelay", mc.KEY_DELAY, is_thermostat=True
)
Appliance_Control_Thermostat_CtlRange = _ns_get_set(
    "Appliance.Control.Thermostat.CtlRange", mc.KEY_CTLRANGE, is_thermostat=True
)
Appliance_Control_Thermostat_DeadZone = _ns_get_set(
    "Appliance.Control.Thermostat.DeadZone", mc.KEY_DEADZONE, is_thermostat=True
)
Appliance_Control_Thermostat_Frost = _ns_get_set(
    "Appliance.Control.Thermostat.Frost", mc.KEY_FROST, is_thermostat=True
)
Appliance_Control_Thermostat_HoldAction = _ns_get_push(
    "Appliance.Control.Thermostat.HoldAction", mc.KEY_HOLDACTION, is_thermostat=True
)
Appliance_Control_Thermostat_Mode = _ns_get_set_push(
    "Appliance.Control.Thermostat.Mode", mc.KEY_MODE, is_thermostat=True
)
Appliance_Control_Thermostat_ModeB = _ns_get_set_push(
    "Appliance.Control.Thermostat.ModeB", mc.KEY_MODEB, is_thermostat=True
)
Appliance_Control_Thermostat_ModeC = _ns_get_set_push(
    "Appliance.Control.Thermostat.ModeC", mc.KEY_CONTROL, PayloadType.LIST_C
) # pretty different namespace semantics for this device (mts300)
Appliance_Control_Thermostat_Overheat = _ns_get_set_push(
    "Appliance.Control.Thermostat.Overheat", mc.KEY_OVERHEAT, is_thermostat=True
)
Appliance_Control_Thermostat_Schedule = _ns_get_set_push(
    "Appliance.Control.Thermostat.Schedule", mc.KEY_SCHEDULE, is_thermostat=True
)
Appliance_Control_Thermostat_ScheduleB = _ns_get_set_push(
    "Appliance.Control.Thermostat.ScheduleB", mc.KEY_SCHEDULEB, is_thermostat=True
)
Appliance_Control_Thermostat_Sensor = _ns_get_push(
    "Appliance.Control.Thermostat.Sensor", mc.KEY_SENSOR, is_thermostat=True
)
Appliance_Control_Thermostat_SummerMode = _ns_get_set_push(
    "Appliance.Control.Thermostat.SummerMode", mc.KEY_SUMMERMODE, is_thermostat=True
)
Appliance_Control_Thermostat_System = _ns_get_push(
    "Appliance.Control.Thermostat.System", mc.KEY_CONTROL, is_thermostat=True
)
Appliance_Control_Thermostat_Timer = _ns_get_set_push(
    "Appliance.Control.Thermostat.Timer", mc.KEY_TIMER, is_thermostat=True
)
Appliance_Control_Thermostat_WindowOpened = _ns_get_push(
    "Appliance.Control.Thermostat.WindowOpened", mc.KEY_WINDOWOPENED, is_thermostat=True
)
Appliance_Control_TimerX = _ns_no_query("Appliance.Control.TimerX", mc.KEY_TIMERX)
Appliance_Control_Toggle = _ns_get_set_push("Appliance.Control.Toggle", mc.KEY_TOGGLE)
Appliance_Control_ToggleX = _ns_get_set_push(
    "Appliance.Control.ToggleX", mc.KEY_TOGGLEX
)
Appliance_Control_Trigger = _ns_get_set_push(
    "Appliance.Control.Trigger", mc.KEY_TRIGGER
)
Appliance_Control_TriggerX = _ns_get_set_push(
    "Appliance.Control.TriggerX", mc.KEY_TRIGGERX
)
Appliance_Control_Unbind = _ns_push_query("Appliance.Control.Unbind")
Appliance_Control_Upgrade = _ns_no_query("Appliance.Control.Upgrade")


Appliance_Digest_TimerX = _ns_no_query("Appliance.Digest.TimerX", mc.KEY_DIGEST)
Appliance_Digest_TriggerX = _ns_no_query("Appliance.Digest.TriggerX", mc.KEY_DIGEST)

Appliance_Encrypt_Suite = _ns_no_query("Appliance.Encrypt.Suite")
Appliance_Encrypt_ECDHE = _ns_no_query("Appliance.Encrypt.ECDHE")

Appliance_GarageDoor_Config = _ns_get_set(
    "Appliance.GarageDoor.Config", mc.KEY_CONFIG, PayloadType.DICT
)
Appliance_GarageDoor_MultipleConfig = _ns_get_set(
    "Appliance.GarageDoor.MultipleConfig",
    mc.KEY_CONFIG,
    PayloadType.LIST_C,
)
Appliance_GarageDoor_State = _ns_get_set_push(
    "Appliance.GarageDoor.State",
    mc.KEY_STATE,
    PayloadType.DICT,
    grammar=Grammar.EXPERIMENTAL,
)

Appliance_Digest_Hub = _ns_get(
    "Appliance.Digest.Hub", mc.KEY_HUB, PayloadType.LIST, map=HUB_NAMESPACES
)
Appliance_Hub_Battery = _ns_get_push(
    "Appliance.Hub.Battery", mc.KEY_BATTERY, is_hub_id=True
)
Appliance_Hub_Exception = _ns_get_push(
    "Appliance.Hub.Exception", mc.KEY_EXCEPTION, is_hub_id=True
)
Appliance_Hub_Online = _ns_get_push(
    "Appliance.Hub.Online", mc.KEY_ONLINE, is_hub_id=True
)
Appliance_Hub_PairSubDev = _ns_get_push("Appliance.Hub.PairSubDev", is_hub_id=True)
Appliance_Hub_Report = _ns_get_push("Appliance.Hub.Report", is_hub_id=True)
Appliance_Hub_Sensitivity = _ns_get_push("Appliance.Hub.Sensitivity", is_hub_id=True)
Appliance_Hub_SubdeviceList = _ns_get_push(
    "Appliance.Hub.SubdeviceList", is_hub_id=True
)
Appliance_Hub_ToggleX = _ns_get_set_push(
    "Appliance.Hub.ToggleX", mc.KEY_TOGGLEX, is_hub_id=True
)
Appliance_Hub_Mts100_Adjust = _ns_get_set(
    "Appliance.Hub.Mts100.Adjust", mc.KEY_ADJUST, is_hub_id=True
)
Appliance_Hub_Mts100_All = _ns_get(
    "Appliance.Hub.Mts100.All", mc.KEY_ALL, is_hub_id=True
)
Appliance_Hub_Mts100_Mode = _ns_get_set_push(
    "Appliance.Hub.Mts100.Mode", mc.KEY_MODE, is_hub_id=True
)
Appliance_Hub_Mts100_Schedule = _ns_get_set_push(
    "Appliance.Hub.Mts100.Schedule", mc.KEY_SCHEDULE, is_hub_id=True
)
Appliance_Hub_Mts100_ScheduleB = _ns_get_set_push(
    "Appliance.Hub.Mts100.ScheduleB", mc.KEY_SCHEDULE, is_hub_id=True
)
Appliance_Hub_Mts100_Temperature = _ns_get_set_push(
    "Appliance.Hub.Mts100.Temperature", mc.KEY_TEMPERATURE, is_hub_id=True
)
Appliance_Hub_Mts100_TimeSync = _ns_get_push(
    "Appliance.Hub.Mts100.TimeSync", is_hub_id=True
)
Appliance_Hub_Mts100_SuperCtl = _ns_get_push(
    "Appliance.Hub.Mts100.SuperCtl", is_hub_id=True
)
Appliance_Hub_Sensor_Adjust = _ns_get_set(
    "Appliance.Hub.Sensor.Adjust", mc.KEY_ADJUST, is_hub_id=True
)
Appliance_Hub_Sensor_Alert = _ns_get_push("Appliance.Hub.Sensor.Alert", is_hub_id=True)
Appliance_Hub_Sensor_All = _ns_get(
    "Appliance.Hub.Sensor.All", mc.KEY_ALL, is_hub_id=True
)
Appliance_Hub_Sensor_DoorWindow = _ns_get_push(
    "Appliance.Hub.Sensor.DoorWindow", mc.KEY_DOORWINDOW, is_hub_id=True
)
Appliance_Hub_Sensor_Latest = _ns_get_push(
    "Appliance.Hub.Sensor.Latest", mc.KEY_LATEST, is_hub_id=True
)
Appliance_Hub_Sensor_Motion = _ns_get_push(
    "Appliance.Hub.Sensor.Motion", is_hub_id=True
)
Appliance_Hub_Sensor_Smoke = _ns_get_push(
    "Appliance.Hub.Sensor.Smoke", mc.KEY_SMOKEALARM, is_hub_id=True
)
Appliance_Hub_Sensor_TempHum = _ns_get_push(
    "Appliance.Hub.Sensor.TempHum", mc.KEY_TEMPHUM, is_hub_id=True
)
Appliance_Hub_Sensor_WaterLeak = _ns_get_push(
    "Appliance.Hub.Sensor.WaterLeak", mc.KEY_WATERLEAK, is_hub_id=True
)
Appliance_Hub_SubDevice_Beep = _ns_get_push(
    "Appliance.Hub.SubDevice.Beep", is_hub_id=True
)
Appliance_Hub_SubDevice_MotorAdjust = _ns_get_push(
    "Appliance.Hub.SubDevice.MotorAdjust", mc.KEY_ADJUST, is_hub_id=True
)
Appliance_Hub_SubDevice_Version = _ns_get_push(
    "Appliance.Hub.SubDevice.Version", mc.KEY_VERSION, is_hub_id=True
)

Appliance_Mcu_Firmware = _ns_unknown("Appliance.Mcu.Firmware")
Appliance_Mcu_Upgrade = _ns_unknown("Appliance.Mcu.Upgrade")

# Smart cherub HP110A
Appliance_Mcu_Hp110_Firmware = _ns_unknown("Appliance.Mcu.Hp110.Firmware")
Appliance_Mcu_Hp110_Favorite = _ns_unknown("Appliance.Mcu.Hp110.Favorite")
Appliance_Mcu_Hp110_Preview = _ns_unknown("Appliance.Mcu.Hp110.Preview")
Appliance_Mcu_Hp110_Lock = _ns_unknown("Appliance.Mcu.Hp110.Lock")

Appliance_RollerShutter_Adjust = _ns_push_query(
    "Appliance.RollerShutter.Adjust", mc.KEY_ADJUST
)  # maybe SET supported too
Appliance_RollerShutter_Config = _ns_get_set(
    "Appliance.RollerShutter.Config", mc.KEY_CONFIG, PayloadType.LIST
)
Appliance_RollerShutter_Position = _ns_get_set_push(
    "Appliance.RollerShutter.Position", mc.KEY_POSITION, PayloadType.LIST
)
Appliance_RollerShutter_State = _ns_get_push(
    "Appliance.RollerShutter.State", mc.KEY_STATE, PayloadType.LIST
)
