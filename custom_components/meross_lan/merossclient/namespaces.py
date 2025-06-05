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


class RequestPayloadType(enum.Enum):
    """Depicts the payload structure in GET queries (defaults to DICT in case)."""

    DICT = {}
    """Command GET with an empty dict returns all the (channels) state."""
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
    """Used to mark namespaces for which our normalization is not 100% sure."""
    STABLE = enum.auto()
    """Used to mark namespaces for which our normalization is about 100% correct and complete."""


class Namespace:
    """
    Namespace descriptor helper class. This is used to build a definition
    of namespace behaviors and syntax.
    """

    if typing.TYPE_CHECKING:

        class Args(TypedDict):
            map: NotRequired[NamespacesMapType]
            key_channel: NotRequired[str | None]
            has_get: NotRequired[bool | None]
            has_push: NotRequired[bool | None]
            is_hub: NotRequired[bool]
            is_sensor: NotRequired[bool]
            is_thermostat: NotRequired[bool]

        DEFAULT_PUSH_PAYLOAD: Final
        name: Final[str]
        """The namespace name"""
        key: Final[str]
        """The root key of the payload"""
        key_channel: Final[str]
        """The key used to index items in list payloads"""
        has_get: Final[bool | None]
        """ns supports method GET - is None when we have no clue"""
        has_push: Final[bool | None]
        """ns supports method PUSH - is None when we have no clue"""
        request_payload_type: Final[RequestPayloadType]
        grammar: Final[Grammar]

    DEFAULT_PUSH_PAYLOAD = RequestPayloadType.DICT.value

    __slots__ = (
        "name",
        "key",
        "key_channel",
        "has_get",
        "has_push",
        "payload_get_type",
        "grammar",
        "__dict__",
    )

    def __init__(
        self,
        name: str,
        key: str | None = None,
        request_payload_type: RequestPayloadType | None = None,
        grammar: Grammar = Grammar.UNKNOWN,
        /,
        **kwargs: "Unpack[Args]",
    ) -> None:
        self.name = name
        self.grammar = grammar
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

        map = kwargs.pop("map", NAMESPACES)
        self.key_channel = kwargs.pop("key_channel", None)  # type: ignore
        self.has_get = kwargs.pop("has_get", None)
        self.has_push = kwargs.pop("has_push", None)
        # process eventual is_hub, is_thermostat or so
        for name, value in kwargs.items():
            setattr(self, name, value)

        if request_payload_type is None:
            match name.split("."):
                case (_, "Hub", *_):
                    self.is_hub = True
                    self.key_channel = self.key_channel or mc.KEY_ID
                    request_payload_type = RequestPayloadType.LIST
                case (_, "RollerShutter", *_):
                    request_payload_type = RequestPayloadType.LIST
                case (_, "Control", "Screen", *_):
                    request_payload_type = RequestPayloadType.LIST_C
                case (_, "Control", "Sensor", *_):
                    self.is_sensor = True
                    if map is HUB_NAMESPACES:
                        self.key_channel = self.key_channel or mc.KEY_SUBID
                        request_payload_type = RequestPayloadType.LIST_C
                    else:
                        self.key_channel = self.key_channel or mc.KEY_CHANNEL
                        request_payload_type = RequestPayloadType.DICT_C
                case (_, "Control", "Thermostat", *_):
                    self.is_thermostat = True
                    request_payload_type = RequestPayloadType.LIST_C
                case _:
                    request_payload_type = RequestPayloadType.DICT

        self.request_payload_type = request_payload_type
        # eventually fix the key_channel should we need some heuristics
        self.key_channel = self.key_channel or (
            mc.KEY_ID
            if self.is_hub
            else (
                mc.KEY_SUBID
                if (self.is_sensor and (map is HUB_NAMESPACES))
                else mc.KEY_CHANNEL
            )
        )
        map[name] = self  # type: ignore

    @cached_property
    def is_hub(self):
        """Namespace payload indexed on subdevice by key 'id'."""
        return bool(re.match(r"Appliance\.Hub\.(.*)", self.name))

    @cached_property
    def is_sensor(self):
        """Namespace payload indexed on hub/subdevice by key 'subId' or
        by 'channel' for regular devices."""
        return bool(re.match(r"Appliance\.Control\.Sensor\.(.*)", self.name))

    @cached_property
    def is_thermostat(self):
        return bool(re.match(r"Appliance\.Control\.Thermostat\.(.*)", self.name))

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
        if self.has_get is False:
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
    ns_key = None
    key_channel = None
    request_payload_type = None
    if payload:
        ns_payload = None
        # we hope the first key in the payload is the 'namespace key'
        for ns_key, ns_payload in payload.items():
            break

        if type(ns_payload) is list:
            request_payload_type = RequestPayloadType.LIST
            if ns_payload:
                ns_payload = ns_payload[0]
                for key_channel in (mc.KEY_SUBID, mc.KEY_ID, mc.KEY_CHANNEL):
                    if key_channel in ns_payload:
                        request_payload_type = RequestPayloadType.LIST_C
                        break
                else:
                    # let the Namespace ctor euristics
                    key_channel = None
        elif type(ns_payload) is dict:
            request_payload_type = RequestPayloadType.DICT

    return Namespace(
        namespace,
        ns_key,
        request_payload_type,
        Grammar.UNKNOWN,
        key_channel=key_channel,
        has_push=(method == mc.METHOD_PUSH) or None,
        map=map,
    )


def _ns_unknown(name: str, key: str | None = None, /):
    """Builds a definition for a namespace without specific knowledge of supported methods"""
    return Namespace(name, key, None, Grammar.UNKNOWN)


def _ns_push(name: str, key: str | None = None, /):
    """Builds a definition for a namespace supporting only PUSH queries (no GET)"""
    return Namespace(name, key, None, Grammar.STABLE, has_get=False, has_push=True)


def _ns_get(
    name: str,
    key: str | None = None,
    request_payload_type: RequestPayloadType | None = None,
    grammar: Grammar = Grammar.STABLE,
    /,
    **kwargs: "Unpack[Namespace.Args]",
):
    """Builds a definition for a namespace supporting only GET queries (no PUSH)"""
    kwargs["has_get"] = True
    kwargs["has_push"] = False
    return Namespace(name, key, request_payload_type, grammar, **kwargs)


def _ns_get_hub(
    name: str,
    key: str | None = None,
    request_payload_type: RequestPayloadType | None = None,
    grammar: Grammar = Grammar.STABLE,
    /,
):
    """Builds a definition for a namespace supporting only GET queries (no PUSH)"""
    return Namespace(
        name,
        key,
        request_payload_type,
        grammar,
        **{
            "map": HUB_NAMESPACES,
            "key_channel": mc.KEY_ID,
            "has_get": True,
            "has_push": False,
            "is_hub": True,
            "is_thermostat": False,
            "is_sensor": False,
        },
    )


def _ns_get_sensor(
    name: str,
    key: str | None = None,
    map: "NamespacesMapType" = NAMESPACES,
    /,
):
    """Builds a definition for a namespace supporting only GET queries (no PUSH).
    By specifying the 'map' parameter we can define both 'regular' devices grammar
    and a different grammar for Hub(s)."""
    return Namespace(
        name,
        key,
        (
            RequestPayloadType.LIST_C
            if map is HUB_NAMESPACES
            else RequestPayloadType.DICT_C
        ),
        Grammar.EXPERIMENTAL,
        **{
            "map": map,
            "key_channel": mc.KEY_SUBID if map is HUB_NAMESPACES else mc.KEY_CHANNEL,
            "has_get": True,
            "has_push": False,
            "is_hub": False,
            "is_thermostat": False,
            "is_sensor": True,
        },
    )


def _ns_get_thermostat(
    name: str,
    key: str | None = None,
    request_payload_type: RequestPayloadType | None = None,
    grammar: Grammar = Grammar.STABLE,
    /,
):
    """Builds a definition for a namespace supporting only GET queries (no PUSH)"""
    return Namespace(
        name,
        key,
        request_payload_type,
        grammar,
        **{
            "key_channel": mc.KEY_CHANNEL,
            "has_get": True,
            "has_push": False,
            "is_hub": False,
            "is_thermostat": True,
            "is_sensor": False,
        },
    )


def _ns_get_push(
    name: str,
    key: str | None = None,
    request_payload_type: RequestPayloadType | None = None,
    grammar: Grammar = Grammar.STABLE,
    /,
    **kwargs: "Unpack[Namespace.Args]",
):
    """Builds a definition for a namespace supporting GET queries (which also PUSHes updates)"""
    kwargs["has_get"] = True
    kwargs["has_push"] = True
    return Namespace(name, key, request_payload_type, grammar, **kwargs)


def _ns_get_push_hub(
    name: str,
    key: str | None = None,
    request_payload_type: RequestPayloadType | None = None,
    grammar: Grammar = Grammar.STABLE,
    /,
):
    """Builds a definition for a namespace supporting GET queries (which also PUSHes updates)"""
    return Namespace(
        name,
        key,
        request_payload_type,
        grammar,
        **{
            "map": HUB_NAMESPACES,
            "key_channel": mc.KEY_ID,
            "has_get": True,
            "has_push": True,
            "is_hub": True,
            "is_thermostat": False,
            "is_sensor": False,
        },
    )


def _ns_get_push_thermostat(
    name: str,
    key: str | None = None,
    request_payload_type: RequestPayloadType | None = None,
    grammar: Grammar = Grammar.STABLE,
    /,
):
    """Builds a definition for a namespace supporting only GET queries (no PUSH)"""
    return Namespace(
        name,
        key,
        request_payload_type,
        grammar,
        **{
            "key_channel": mc.KEY_CHANNEL,
            "has_get": True,
            "has_push": True,
            "is_hub": False,
            "is_thermostat": True,
            "is_sensor": False,
        },
    )


def _ns_set(
    name: str,
    key: str | None = None,
    request_payload_type: RequestPayloadType | None = None,
    grammar: Grammar = Grammar.STABLE,
    /,
    **kwargs: "Unpack[Namespace.Args]",
):
    """Builds a definition for a namespace supporting only SET.
    Actually indistinguishable from 'no_query'."""
    kwargs["has_get"] = False
    kwargs["has_push"] = False
    return Namespace(name, key, request_payload_type, grammar, **kwargs)


def _ns_no_query(
    name: str,
    key: str | None = None,
    grammar: Grammar = Grammar.STABLE,
    /,
    **kwargs: "Unpack[Namespace.Args]",
):
    """Builds a definition for a namespace not supporting GET,PUSH"""
    kwargs["has_get"] = False
    kwargs["has_push"] = False
    return Namespace(name, key, None, grammar, **kwargs)


# We predefine grammar for some widely used and well known namespaces either to skip 'euristics'
# and time consuming evaluation.
# Moreover, for some namespaces, the euristics about 'namespace key' and payload structure are not
# good so we must fix those beforehand.
Appliance_System_Ability = _ns_get(
    "Appliance.System.Ability", mc.KEY_ABILITY, RequestPayloadType.DICT
)
Appliance_System_All = _ns_get(
    "Appliance.System.All", mc.KEY_ALL, RequestPayloadType.DICT
)
Appliance_System_Clock = _ns_push("Appliance.System.Clock", mc.KEY_CLOCK)
Appliance_System_Debug = _ns_get(
    "Appliance.System.Debug", mc.KEY_DEBUG, RequestPayloadType.DICT
)
Appliance_System_DNDMode = _ns_get(
    "Appliance.System.DNDMode", mc.KEY_DNDMODE, RequestPayloadType.DICT
)
Appliance_System_Firmware = _ns_get(
    "Appliance.System.Firmware", mc.KEY_FIRMWARE, RequestPayloadType.DICT
)
Appliance_System_Hardware = _ns_get(
    "Appliance.System.Hardware", mc.KEY_HARDWARE, RequestPayloadType.DICT
)
Appliance_System_Online = _ns_get_push(
    "Appliance.System.Online", mc.KEY_ONLINE, RequestPayloadType.DICT
)
Appliance_System_Report = _ns_push("Appliance.System.Report", mc.KEY_REPORT)
Appliance_System_Runtime = _ns_get(
    "Appliance.System.Runtime", mc.KEY_RUNTIME, RequestPayloadType.DICT
)
Appliance_System_Time = _ns_get_push(
    "Appliance.System.Time", mc.KEY_TIME, RequestPayloadType.DICT
)
Appliance_System_Position = _ns_get(
    "Appliance.System.Position", mc.KEY_POSITION, RequestPayloadType.DICT
)

Appliance_Config_Key = _ns_set(
    "Appliance.Config.Key", mc.KEY_KEY, RequestPayloadType.DICT
)
Appliance_Config_OverTemp = _ns_get(
    "Appliance.Config.OverTemp", mc.KEY_OVERTEMP, RequestPayloadType.DICT
)
Appliance_Config_Trace = _ns_get("Appliance.Config.Trace")
Appliance_Config_Wifi = _ns_get("Appliance.Config.Wifi")
Appliance_Config_WifiList = _ns_get("Appliance.Config.WifiList")
Appliance_Config_WifiX = _ns_get("Appliance.Config.WifiX")


Appliance_Control_Bind = _ns_get(
    "Appliance.Control.Bind", mc.KEY_BIND, RequestPayloadType.DICT
)
Appliance_Control_ConsumptionConfig = _ns_get(
    "Appliance.Control.ConsumptionConfig", mc.KEY_CONFIG, RequestPayloadType.DICT
)
Appliance_Control_ConsumptionH = _ns_get(
    "Appliance.Control.ConsumptionH", mc.KEY_CONSUMPTIONH, RequestPayloadType.LIST_C
)
Appliance_Control_ConsumptionX = _ns_get_push(
    "Appliance.Control.ConsumptionX", mc.KEY_CONSUMPTIONX, RequestPayloadType.LIST
)
Appliance_Control_Diffuser_Light = _ns_get_push(
    "Appliance.Control.Diffuser.Light", mc.KEY_LIGHT, RequestPayloadType.DICT
)
Appliance_Control_Diffuser_Sensor = _ns_get_push(
    "Appliance.Control.Diffuser.Sensor", mc.KEY_SENSOR, RequestPayloadType.DICT
)
Appliance_Control_Diffuser_Spray = _ns_get_push(
    "Appliance.Control.Diffuser.Spray", mc.KEY_SPRAY, RequestPayloadType.DICT
)
Appliance_Control_Electricity = _ns_get_push(
    "Appliance.Control.Electricity", mc.KEY_ELECTRICITY, RequestPayloadType.DICT
)
Appliance_Control_ElectricityX = _ns_get_push(
    "Appliance.Control.ElectricityX",
    mc.KEY_ELECTRICITY,
    RequestPayloadType.LIST_C,
    Grammar.EXPERIMENTAL,
)
Appliance_Control_Fan = _ns_get("Appliance.Control.Fan", mc.KEY_FAN)
Appliance_Control_FilterMaintenance = _ns_push(
    "Appliance.Control.FilterMaintenance", mc.KEY_FILTER
)
Appliance_Control_Light = _ns_get_push("Appliance.Control.Light")
Appliance_Control_Light_Effect = _ns_get(
    "Appliance.Control.Light.Effect", mc.KEY_EFFECT, RequestPayloadType.LIST
)
Appliance_Control_Mp3 = _ns_get_push(
    "Appliance.Control.Mp3", mc.KEY_MP3, RequestPayloadType.DICT
)
Appliance_Control_Multiple = _ns_get(
    "Appliance.Control.Multiple", mc.KEY_MULTIPLE, RequestPayloadType.LIST
)
Appliance_Control_OverTemp = _ns_get(
    "Appliance.Control.OverTemp", mc.KEY_OVERTEMP, RequestPayloadType.LIST
)
Appliance_Control_PhysicalLock = _ns_push("Appliance.Control.PhysicalLock", mc.KEY_LOCK)
Appliance_Control_Presence_Config = _ns_get(
    "Appliance.Control.Presence.Config", mc.KEY_CONFIG, RequestPayloadType.LIST_C
)
Appliance_Control_Presence_Study = _ns_push(
    "Appliance.Control.Presence.Study", mc.KEY_CONFIG
)  # TODO: parse this namespace (ms600)?
Appliance_Control_Spray = _ns_get_push(
    "Appliance.Control.Spray", mc.KEY_SPRAY, RequestPayloadType.DICT
)
Appliance_Control_TempUnit = _ns_get_push(
    "Appliance.Control.TempUnit", mc.KEY_TEMPUNIT, RequestPayloadType.LIST_C
)
Appliance_Control_TimerX = _ns_get(
    "Appliance.Control.TimerX", mc.KEY_TIMERX, RequestPayloadType.DICT
)
Appliance_Control_Toggle = _ns_get_push(
    "Appliance.Control.Toggle", mc.KEY_TOGGLE, RequestPayloadType.DICT
)
Appliance_Control_ToggleX = _ns_get_push(
    "Appliance.Control.ToggleX", mc.KEY_TOGGLEX, RequestPayloadType.LIST
)
Appliance_Control_Trigger = _ns_get(
    "Appliance.Control.Trigger", mc.KEY_TRIGGER, RequestPayloadType.DICT
)
Appliance_Control_TriggerX = _ns_get(
    "Appliance.Control.TriggerX", mc.KEY_TRIGGERX, RequestPayloadType.DICT
)
Appliance_Control_Unbind = _ns_push("Appliance.Control.Unbind")
Appliance_Control_Upgrade = _ns_get("Appliance.Control.Upgrade")

Appliance_Control_Sensor_Latest = _ns_get_push(
    "Appliance.Control.Sensor.Latest", mc.KEY_LATEST, RequestPayloadType.LIST_C
)  # carrying miscellaneous sensor values (temp/humi)
Appliance_Control_Sensor_History = _ns_get_push(
    "Appliance.Control.Sensor.History", mc.KEY_HISTORY, RequestPayloadType.LIST_C
)  # history of sensor values
# Appliance.Control.Sensor.* appear on both regular devices (ms600) and hub/subdevices (ms130)
# To distinguish the grammar between regular devices and hubs we save different definitions
# in NAMESPACES (for regular devices) and in HUB_NAMESPACES (for hubs).
# For regular devices, even if traces show presence of values at channel 0,
# the 'LIST_C' query format doesn't work
# We so try introduce a new payload type 'DICT_C'. PUSH query too seems to not work.
# See _ns_get_sensor to get some clues.
Appliance_Control_Sensor_HistoryX = _ns_get_sensor(
    "Appliance.Control.Sensor.HistoryX",
    mc.KEY_HISTORY,
)
Hub_Control_Sensor_HistoryX = _ns_get_sensor(
    "Appliance.Control.Sensor.HistoryX",
    mc.KEY_HISTORY,
    HUB_NAMESPACES,
)
Appliance_Control_Sensor_LatestX = _ns_get_sensor(
    "Appliance.Control.Sensor.LatestX",
    mc.KEY_LATEST,
)
Hub_Control_Sensor_LatestX = _ns_get_sensor(
    "Appliance.Control.Sensor.LatestX",
    mc.KEY_LATEST,
    HUB_NAMESPACES,
)

# MTS200-960 smart thermostat
Appliance_Control_Screen_Brightness = _ns_get_push(
    "Appliance.Control.Screen.Brightness"
)
Appliance_Control_Thermostat_Alarm = _ns_get_push_thermostat(
    "Appliance.Control.Thermostat.Alarm"
)
Appliance_Control_Thermostat_AlarmConfig = _ns_get_thermostat(
    "Appliance.Control.Thermostat.AlarmConfig"
)
Appliance_Control_Thermostat_Calibration = _ns_get_thermostat(
    "Appliance.Control.Thermostat.Calibration"
)
Appliance_Control_Thermostat_CompressorDelay = _ns_get_thermostat(
    "Appliance.Control.Thermostat.CompressorDelay",
    mc.KEY_DELAY,
    RequestPayloadType.LIST_C,
)
Appliance_Control_Thermostat_CtlRange = _ns_get_thermostat(
    "Appliance.Control.Thermostat.CtlRange"
)
Appliance_Control_Thermostat_DeadZone = _ns_get_thermostat(
    "Appliance.Control.Thermostat.DeadZone"
)
Appliance_Control_Thermostat_Frost = _ns_get_thermostat(
    "Appliance.Control.Thermostat.Frost"
)
Appliance_Control_Thermostat_HoldAction = _ns_get_push_thermostat(
    "Appliance.Control.Thermostat.HoldAction"
)
Appliance_Control_Thermostat_Mode = _ns_get_push_thermostat(
    "Appliance.Control.Thermostat.Mode"
)
Appliance_Control_Thermostat_ModeB = _ns_get_push_thermostat(
    "Appliance.Control.Thermostat.ModeB"
)
Appliance_Control_Thermostat_Overheat = _ns_get_push_thermostat(
    "Appliance.Control.Thermostat.Overheat"
)
Appliance_Control_Thermostat_Schedule = _ns_get_push_thermostat(
    "Appliance.Control.Thermostat.Schedule"
)
Appliance_Control_Thermostat_ScheduleB = _ns_get_push_thermostat(
    "Appliance.Control.Thermostat.ScheduleB"
)
Appliance_Control_Thermostat_Sensor = _ns_get_push_thermostat(
    "Appliance.Control.Thermostat.Sensor"
)
Appliance_Control_Thermostat_SummerMode = _ns_get_push_thermostat(
    "Appliance.Control.Thermostat.SummerMode"
)
Appliance_Control_Thermostat_Timer = _ns_get_push_thermostat(
    "Appliance.Control.Thermostat.Timer"
)
Appliance_Control_Thermostat_WindowOpened = _ns_get_push_thermostat(
    "Appliance.Control.Thermostat.WindowOpened"
)

Appliance_Digest_TimerX = _ns_get(
    "Appliance.Digest.TimerX", mc.KEY_DIGEST, RequestPayloadType.LIST
)
Appliance_Digest_TriggerX = _ns_get(
    "Appliance.Digest.TriggerX", mc.KEY_DIGEST, RequestPayloadType.LIST
)

Appliance_Encrypt_Suite = _ns_get("Appliance.Encrypt.Suite")
Appliance_Encrypt_ECDHE = _ns_no_query("Appliance.Encrypt.ECDHE")

Appliance_GarageDoor_Config = _ns_get(
    "Appliance.GarageDoor.Config", mc.KEY_CONFIG, RequestPayloadType.DICT
)
Appliance_GarageDoor_MultipleConfig = _ns_get(
    "Appliance.GarageDoor.MultipleConfig",
    mc.KEY_CONFIG,
    RequestPayloadType.LIST_C,
)
Appliance_GarageDoor_State = _ns_get_push(
    "Appliance.GarageDoor.State",
    mc.KEY_STATE,
    RequestPayloadType.DICT,
    Grammar.EXPERIMENTAL,
)

Appliance_Digest_Hub = _ns_get(
    "Appliance.Digest.Hub", mc.KEY_HUB, RequestPayloadType.LIST, map=HUB_NAMESPACES
)

Appliance_Hub_Battery = _ns_get_push_hub(
    "Appliance.Hub.Battery", mc.KEY_BATTERY, RequestPayloadType.LIST
)
Appliance_Hub_Exception = _ns_get_push_hub(
    "Appliance.Hub.Exception", mc.KEY_EXCEPTION, RequestPayloadType.LIST
)
Appliance_Hub_Online = _ns_get_push_hub(
    "Appliance.Hub.Online", mc.KEY_ONLINE, RequestPayloadType.LIST
)
Appliance_Hub_PairSubDev = _ns_get_push_hub("Appliance.Hub.PairSubDev")
Appliance_Hub_Report = _ns_get_push_hub("Appliance.Hub.Report")
Appliance_Hub_Sensitivity = _ns_get_push_hub("Appliance.Hub.Sensitivity")
Appliance_Hub_SubdeviceList = _ns_get_push_hub("Appliance.Hub.SubdeviceList")
Appliance_Hub_ToggleX = _ns_get_push_hub(
    "Appliance.Hub.ToggleX", mc.KEY_TOGGLEX, RequestPayloadType.LIST
)

Appliance_Hub_Mts100_Adjust = _ns_get_hub(
    "Appliance.Hub.Mts100.Adjust", mc.KEY_ADJUST, RequestPayloadType.LIST
)
Appliance_Hub_Mts100_All = _ns_get_hub(
    "Appliance.Hub.Mts100.All", mc.KEY_ALL, RequestPayloadType.LIST
)
Appliance_Hub_Mts100_Mode = _ns_get_push_hub(
    "Appliance.Hub.Mts100.Mode", mc.KEY_MODE, RequestPayloadType.LIST
)
Appliance_Hub_Mts100_Schedule = _ns_get_push_hub(
    "Appliance.Hub.Mts100.Schedule", mc.KEY_SCHEDULE, RequestPayloadType.LIST
)
Appliance_Hub_Mts100_ScheduleB = _ns_get_push_hub(
    "Appliance.Hub.Mts100.ScheduleB", mc.KEY_SCHEDULE, RequestPayloadType.LIST
)
Appliance_Hub_Mts100_Temperature = _ns_get_push_hub(
    "Appliance.Hub.Mts100.Temperature",
    mc.KEY_TEMPERATURE,
    RequestPayloadType.LIST,
)
Appliance_Hub_Mts100_TimeSync = _ns_get_push_hub("Appliance.Hub.Mts100.TimeSync")
Appliance_Hub_Mts100_SuperCtl = _ns_get_push_hub("Appliance.Hub.Mts100.SuperCtl")

Appliance_Hub_Sensor_Adjust = _ns_get_hub(
    "Appliance.Hub.Sensor.Adjust", mc.KEY_ADJUST, RequestPayloadType.LIST
)
Appliance_Hub_Sensor_Alert = _ns_get_push_hub("Appliance.Hub.Sensor.Alert")
Appliance_Hub_Sensor_All = _ns_get_hub(
    "Appliance.Hub.Sensor.All", mc.KEY_ALL, RequestPayloadType.LIST
)
Appliance_Hub_Sensor_DoorWindow = _ns_get_push_hub(
    "Appliance.Hub.Sensor.DoorWindow", mc.KEY_DOORWINDOW, RequestPayloadType.LIST
)
Appliance_Hub_Sensor_Latest = _ns_get_push_hub(
    "Appliance.Hub.Sensor.Latest", mc.KEY_LATEST, RequestPayloadType.LIST
)
Appliance_Hub_Sensor_Motion = _ns_get_push_hub("Appliance.Hub.Sensor.Motion")
Appliance_Hub_Sensor_Smoke = _ns_get_push_hub(
    "Appliance.Hub.Sensor.Smoke", mc.KEY_SMOKEALARM, RequestPayloadType.LIST
)
Appliance_Hub_Sensor_TempHum = _ns_get_push_hub("Appliance.Hub.Sensor.TempHum")
Appliance_Hub_Sensor_WaterLeak = _ns_get_push_hub("Appliance.Hub.Sensor.WaterLeak")

Appliance_Hub_SubDevice_Beep = _ns_get_push_hub("Appliance.Hub.SubDevice.Beep")
Appliance_Hub_SubDevice_MotorAdjust = _ns_get_push_hub(
    "Appliance.Hub.SubDevice.MotorAdjust", mc.KEY_ADJUST, RequestPayloadType.LIST
)
Appliance_Hub_SubDevice_Version = _ns_get_push_hub(
    "Appliance.Hub.SubDevice.Version", mc.KEY_VERSION, RequestPayloadType.LIST
)

Appliance_Mcu_Firmware = _ns_unknown("Appliance.Mcu.Firmware")
Appliance_Mcu_Upgrade = _ns_unknown("Appliance.Mcu.Upgrade")

# Smart cherub HP110A
Appliance_Mcu_Hp110_Firmware = _ns_unknown("Appliance.Mcu.Hp110.Firmware")
Appliance_Mcu_Hp110_Favorite = _ns_unknown("Appliance.Mcu.Hp110.Favorite")
Appliance_Mcu_Hp110_Preview = _ns_unknown("Appliance.Mcu.Hp110.Preview")
Appliance_Mcu_Hp110_Lock = _ns_unknown("Appliance.Mcu.Hp110.Lock")

Appliance_RollerShutter_Adjust = _ns_push("Appliance.RollerShutter.Adjust")
Appliance_RollerShutter_Config = _ns_get("Appliance.RollerShutter.Config")
Appliance_RollerShutter_Position = _ns_get_push("Appliance.RollerShutter.Position")
Appliance_RollerShutter_State = _ns_get_push("Appliance.RollerShutter.State")
