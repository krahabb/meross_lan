"""
Descriptors for namespaces management.
This file contains the knowledge about how namespaces work (their syntax and behaviors).
"""

import enum
from functools import cached_property
from typing import TYPE_CHECKING

from .. import const as mc

if TYPE_CHECKING:
    from typing import Final, Mapping, NotRequired, TypedDict, Unpack

    from ..types import MerossRequestType

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
            return ns(namespace)

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


class imdict(dict):
    def __hash__(self):
        return id(self)

    def _immutable(self, *args, **kws):
        raise TypeError(f"object of type <{type(self)}> is immutable")

    __setitem__ = _immutable
    __delitem__ = _immutable
    __ior__ = _immutable
    clear = _immutable
    update = _immutable
    setdefault = _immutable  # type: ignore
    pop = _immutable
    popitem = _immutable


class PayloadType(enum.Enum):
    """Depicts the payload structure in GET queries (defaults to DICT in case)."""

    NONE = imdict({})
    """No GET query supported."""
    DICT = imdict({})
    """Command GET with an empty dict (should) return all the (channels) state.
    This is used as default unless some heuristic (in __init__) states something different."""
    DICT_C = imdict({mc.KEY_CHANNEL: 0})
    """Command GET with channel index in dict returns the state requested."""
    LIST = []
    """Command GET with an empty list returns all the (channels) state."""
    LIST_C = [{mc.KEY_CHANNEL: 0}]
    """Command GET with channel index dicts in a list returns the states requested."""
    LIST_SX = [{mc.KEY_CHANNEL: 0, mc.KEY_DATA: []}]
    """Command GET for *.LatestX (and maybe *.HistoryX) ns."""


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

    if TYPE_CHECKING:

        class Args(TypedDict):
            """Args are often mututally exclusive and allow for a cascading of heuristics.
            For example, setting is_hub_id=True will automatically apply 'map' 'payload'
            'is_hub_namespace' 'key_channel'. The code in __init__ might seem a bit complex
            but tries to be as tidy (and efficient) as possible in order to be 'guided' by
            the parameters applied.
            We'll use a set of simple dicts working as small chunks and allowing to build
            concise yet complex definitions (See GET, NO_GET, etc definitions)."""

            map: NotRequired[NamespacesMapType]
            grammar: NotRequired[Grammar]
            payload: NotRequired[PayloadType | None]
            is_hub_id: NotRequired[bool]
            is_hub_subid: NotRequired[bool]
            is_sensor: NotRequired[bool]
            is_thermostat: NotRequired[bool]

            has_get: NotRequired[bool | None]
            has_set: NotRequired[bool | None]
            has_push: NotRequired[bool | None]
            has_push_query: NotRequired[bool | None]

        DEFAULT_PUSH_PAYLOAD: Final
        name: Final[str]
        """The namespace name"""
        slug: Final[str]
        """The namespace 'slug' i.e. the last split of name"""
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
        """This flag could also appear in Hub(s): for that case it is likely mapped to is_hub_subid."""
        is_thermostat: Final[bool]  # type: ignore

    DEFAULT_PUSH_PAYLOAD = PayloadType.DICT.value

    __slots__ = (
        "name",
        "slug",
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
        kwargs: "Args" = {},
    ) -> None:
        self.name = name
        name_split = name.split(".")
        slug = name_split[-1]
        # When namespace 'key' is not provided we'll infer it
        # by camelCasing the last split of the namespace
        # with special care for also the last char which looks
        # lowercase when it's a X (i.e. ToggleX -> togglex).
        # Recently (2025) it appears the namespace 'key' is likely to be the 2nd
        # split of the namespace though (i.e. Appliance.Config.DeviceCfg -> config)
        if slug[-1] == "X":
            self.slug = f"{slug[0].lower()}{slug[1:-1]}x"
        else:
            self.slug = f"{slug[0].lower()}{slug[1:]}"
        self.key = key or self.slug

        map = kwargs.get("map", NAMESPACES)
        payload = kwargs.get("payload")
        self.grammar = kwargs.get("grammar", Grammar.UNKNOWN)
        for _attr in ("has_get", "has_set", "has_push", "has_push_query"):
            setattr(self, _attr, kwargs.get(_attr, None))
        for _attr in ("is_hub_id", "is_hub_subid", "is_thermostat", "is_sensor"):
            setattr(self, _attr, kwargs.get(_attr, False))

        if self.is_hub_id:
            self.key_channel = mc.KEY_ID
            payload = payload or PayloadType.LIST
        elif self.is_hub_subid:
            self.key_channel = mc.KEY_SUBID
            payload = payload or PayloadType.LIST_C
        elif self.is_thermostat:
            self.key_channel = mc.KEY_CHANNEL
            payload = PayloadType.LIST_C
        elif self.is_sensor:
            # In a Hub this conditional should not be reached since
            # sensor namespaces in hubs are being mapped to 'is_hub_subid'
            assert not self.is_hub_subid
            self.key_channel = mc.KEY_CHANNEL
            payload = payload or PayloadType.LIST_C
        elif not payload:
            # Don't pass any payload arg if we want to automatically
            # apply heuristics to incoming new namespaces.
            # Forwarding a payload arg to the constructor is just an 'hint'
            # used by our factory functions ('_ns_xxx') to skip unneded name parsing
            match name_split:
                case (_, "Hub", *_):
                    # This is not always true: some 'hub' namespaces don't get indexed by 'id' (nor by 'subId')
                    # Examples are ExtraInfo or SubdeviceList. In our definitions we'll solve the problem
                    # by explicitly passing the map=HUB_NAMESPACES so that they're mapped into the right storage
                    # but the rules for parsing are very custom and likely need to be managed on a case by case
                    # at the HubMixin level.
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
                    else:
                        self.key_channel = mc.KEY_CHANNEL
                    payload = PayloadType.LIST_C
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

    r"""
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

    def __hash__(self):
        return hash(self.name)

    def __eq__(self, value):
        return self is value


def ns_build_from_message(
    namespace: str, method: str, payload: "Mapping", map: "NamespacesMapType", /
):
    # we hope the first key in the payload is the 'namespace key'
    for ns_key in payload.keys():
        break
    else:
        ns_key = None

    return Namespace(
        namespace,
        ns_key,
        {
            "has_get": (method == mc.METHOD_GETACK) or None,
            "has_set": (method == mc.METHOD_SETACK) or None,
            "has_push": (method == mc.METHOD_PUSH) or None,
            "map": map,
        },
    )


ns = Namespace  # shortcut for declarations


GET: "Namespace.Args" = {"has_get": True}
"""Device supports method GET."""
NO_GET: "Namespace.Args" = {"has_get": False}
SET: "Namespace.Args" = {"has_set": True}
"""Device supports method SET."""
NO_SET: "Namespace.Args" = {"has_set": False}
PUSH: "Namespace.Args" = {"has_push": True}
"""Device sends async PUSH."""
NO_PUSH: "Namespace.Args" = {"has_push": False}
PUSHQ: "Namespace.Args" = {"has_push_query": True}
"""Supports querying by PUSH client -> device"""
NO_PUSHQ: "Namespace.Args" = {"has_push_query": False}
IS_SENSOR: "Namespace.Args" = {"is_sensor": True}
P_NONE: "Namespace.Args" = {"payload": PayloadType.NONE}
P_DICT: "Namespace.Args" = {"payload": PayloadType.DICT}
P_DICT_C: "Namespace.Args" = {"payload": PayloadType.DICT_C}
P_LIST: "Namespace.Args" = {"payload": PayloadType.LIST}
P_LIST_C: "Namespace.Args" = {"payload": PayloadType.LIST_C}
P_LIST_SX: "Namespace.Args" = {"payload": PayloadType.LIST_SX}
G_UNKNOWN: "Namespace.Args" = {"grammar": Grammar.UNKNOWN}
G_STABLE: "Namespace.Args" = {"grammar": Grammar.STABLE}
G_EXPERIMENTAL: "Namespace.Args" = {"grammar": Grammar.EXPERIMENTAL}


ARGS_NO_Q = NO_GET | NO_SET | NO_PUSH | NO_PUSHQ | P_NONE | G_STABLE
"""No querying allowed. This is the 'root' of all other set definitions
which will then just override these defaults."""
ARGS_GET = ARGS_NO_Q | GET | P_DICT
"""Only method GET supported with default 'dict' payload."""
ARGS_GETSET = ARGS_GET | SET
"""Supports: GET-SET - payload 'dict' by default."""
ARGS_GETSETPUSH = ARGS_GETSET | PUSH
"""Supports: GET-SET-PUSH - payload 'dict' by default."""
ARGS_GETSETPUSHQ = ARGS_GETSETPUSH | PUSHQ
"""Supports: GET-SET-PUSH query - payload 'dict' by default."""
ARGS_GETPUSH = ARGS_GET | PUSH
ARGS_SET = ARGS_NO_Q | SET
ARGS_SETPUSH = ARGS_SET | PUSH
ARGS_SETPUSHQ = ARGS_SETPUSH | PUSHQ
ARGS_PUSH = ARGS_NO_Q | PUSH
ARGS_PUSHQ = ARGS_PUSH | PUSHQ


# We predefine grammar for some widely used and well known namespaces either to skip 'euristics'
# and time consuming evaluation.
# Moreover, for some namespaces, the euristics about 'namespace key' and payload structure are not
# good so we must fix those beforehand.
Appliance_Config_DeviceCfg = ns(
    "Appliance.Config.DeviceCfg", mc.KEY_CONFIG, ARGS_GETSETPUSH | P_LIST_C
)  # mts300
Appliance_Config_Info = ns("Appliance.Config.Info", mc.KEY_INFO, ARGS_GET | ARGS_PUSHQ)
Appliance_Config_Key = ns("Appliance.Config.Key", mc.KEY_KEY, ARGS_SET)
Appliance_Config_Matter = ns("Appliance.Config.Matter", mc.KEY_CONFIG, ARGS_PUSHQ)
Appliance_Config_NtpSite = ns("Appliance.Config.NtpSite", None, ARGS_NO_Q)
Appliance_Config_OverTemp = ns("Appliance.Config.OverTemp", mc.KEY_OVERTEMP, ARGS_GET)
Appliance_Config_Trace = ns("Appliance.Config.Trace", None, ARGS_NO_Q)
Appliance_Config_Wifi = ns("Appliance.Config.Wifi", None, ARGS_NO_Q)
Appliance_Config_WifiList = ns("Appliance.Config.WifiList", None, ARGS_NO_Q)
Appliance_Config_WifiX = ns("Appliance.Config.WifiX", None, ARGS_NO_Q)

Appliance_Config_Sensor_Association = ns(
    "Appliance.Config.Sensor.Association",
    mc.KEY_CONFIG,
    ARGS_GETSETPUSHQ | P_LIST_C | IS_SENSOR,
)
Appliance_Control_AlertConfig = ns(
    "Appliance.Control.AlertConfig", mc.KEY_CONFIG, ARGS_GETSETPUSHQ | P_LIST_C
)  # mts300 support the full set of verbs - em06 also exposes it but that's likely different
Appliance_Control_AlertReport = ns(
    "Appliance.Control.AlertReport", mc.KEY_REPORT, ARGS_GETSET | P_LIST_C
)
Appliance_Control_Bind = ns("Appliance.Control.Bind", mc.KEY_BIND, ARGS_NO_Q)
Appliance_Control_ChangeWifi = ns("Appliance.Control.ChangeWiFi", None, ARGS_NO_Q)
Appliance_Control_ConsumptionConfig = ns(
    "Appliance.Control.ConsumptionConfig", mc.KEY_CONFIG, ARGS_GET
)
Appliance_Control_ConsumptionH = ns(
    "Appliance.Control.ConsumptionH", mc.KEY_CONSUMPTIONH, ARGS_GET | P_LIST_C
)
Appliance_Control_ConsumptionX = ns(
    "Appliance.Control.ConsumptionX", mc.KEY_CONSUMPTIONX, ARGS_GETPUSH | P_LIST
)
Appliance_Control_Diffuser_Light = ns(
    "Appliance.Control.Diffuser.Light", mc.KEY_LIGHT, ARGS_GETSETPUSH
)
Appliance_Control_Diffuser_Sensor = ns(
    "Appliance.Control.Diffuser.Sensor", mc.KEY_SENSOR, ARGS_GETPUSH
)  # this ns has no ns_key in payload response
Appliance_Control_Diffuser_Spray = ns(
    "Appliance.Control.Diffuser.Spray", mc.KEY_SPRAY, ARGS_GETSETPUSH
)
Appliance_Control_Electricity = ns(
    "Appliance.Control.Electricity", mc.KEY_ELECTRICITY, ARGS_GETPUSH
)
Appliance_Control_ElectricityX = ns(
    "Appliance.Control.ElectricityX",
    mc.KEY_ELECTRICITY,
    ARGS_GETPUSH | P_LIST_C | G_EXPERIMENTAL,
)
Appliance_Control_Fan = ns("Appliance.Control.Fan", mc.KEY_FAN, ARGS_GETSET | P_LIST_C)
Appliance_Control_Fan_BtnConfig = ns(
    "Appliance.Control.Fan.BtnConfig", mc.KEY_FAN, ARGS_GETSETPUSHQ | P_LIST_C
)
Appliance_Control_Fan_Config = ns(
    "Appliance.Control.Fan.Config", mc.KEY_FAN, ARGS_GETSET | P_LIST_C
)
Appliance_Control_FilterMaintenance = ns(
    "Appliance.Control.FilterMaintenance", mc.KEY_FILTER, ARGS_PUSHQ | P_LIST
)
Appliance_Control_Light = ns("Appliance.Control.Light", mc.KEY_LIGHT, ARGS_GETSETPUSH)
Appliance_Control_Light_Effect = ns(
    "Appliance.Control.Light.Effect", mc.KEY_EFFECT, ARGS_GETSET | P_LIST
)
Appliance_Control_Mp3 = ns("Appliance.Control.Mp3", mc.KEY_MP3, ARGS_GETSETPUSH)
Appliance_Control_McuUpgrade = ns("Appliance.Control.McuUpgrade", None, ARGS_NO_Q)
Appliance_Control_Multiple = ns("Appliance.Control.Multiple", mc.KEY_MULTIPLE, ARGS_SET)
Appliance_Control_OverTemp = ns(
    "Appliance.Control.OverTemp", mc.KEY_OVERTEMP, ARGS_GET | P_LIST
)
Appliance_Control_PhysicalLock = ns(
    "Appliance.Control.PhysicalLock", mc.KEY_LOCK, ARGS_SETPUSHQ | P_LIST
)
Appliance_Control_Presence_Config = ns(
    "Appliance.Control.Presence.Config", mc.KEY_CONFIG, ARGS_GET | P_LIST_C
)
Appliance_Control_Presence_Study = ns(
    "Appliance.Control.Presence.Study", mc.KEY_CONFIG, ARGS_PUSHQ | P_LIST
)
Appliance_Control_Screen_Brightness = ns(
    "Appliance.Control.Screen.Brightness", mc.KEY_BRIGHTNESS, ARGS_GETSETPUSH | P_LIST_C
)
# Appliance.Control.Sensor.* appear on both regular devices (ms600) and hub/subdevices (ms130)
# To distinguish the grammar between regular devices and hubs we save different definitions
# in NAMESPACES (for regular devices) and in HUB_NAMESPACES (for hubs).
# For regular devices, even if traces show presence of values at channel 0,
# the 'LIST_C' query format doesn't always work
# We so try introduce a new payload type 'DICT_C'. PUSH query too seems to not work.
Appliance_Control_Sensor_Association = ns(
    "Appliance.Control.Sensor.Association",
    mc.KEY_CONTROL,
    ARGS_GET | P_LIST | IS_SENSOR,
)  # mts300 works
Appliance_Control_Sensor_History = ns(
    "Appliance.Control.Sensor.History", mc.KEY_HISTORY, ARGS_GET | P_LIST_C | IS_SENSOR
)  # history of sensor values
Appliance_Control_Sensor_Latest = ns(
    "Appliance.Control.Sensor.Latest",
    mc.KEY_LATEST,
    ARGS_GETPUSH | P_LIST_C | IS_SENSOR,
)  # carrying miscellaneous sensor values (temp/humi)
Appliance_Control_Sensor_HistoryX = ns(
    "Appliance.Control.Sensor.HistoryX",
    mc.KEY_HISTORY,
    ARGS_GET | P_LIST_SX | IS_SENSOR,
)  # cannot get query to work...it might look like LatestX
Appliance_Control_Sensor_LatestX = ns(
    "Appliance.Control.Sensor.LatestX",
    mc.KEY_LATEST,
    ARGS_GETPUSH | P_LIST_SX | IS_SENSOR,
)
Appliance_Control_Spray = ns("Appliance.Control.Spray", mc.KEY_SPRAY, ARGS_GETSETPUSH)
Appliance_Control_TempUnit = ns(
    "Appliance.Control.TempUnit", mc.KEY_TEMPUNIT, ARGS_GETSET | P_LIST_C
)
Appliance_Control_Timer = ns("Appliance.Control.Timer", mc.KEY_TIMER, ARGS_GET | P_LIST)
Appliance_Control_TimerX = ns("Appliance.Control.TimerX", mc.KEY_TIMERX, ARGS_NO_Q)
Appliance_Control_Toggle = ns(
    "Appliance.Control.Toggle", mc.KEY_TOGGLE, ARGS_GETSETPUSH
)
Appliance_Control_ToggleX = ns(
    "Appliance.Control.ToggleX", mc.KEY_TOGGLEX, ARGS_GETSETPUSH
)
Appliance_Control_Trigger = ns(
    "Appliance.Control.Trigger", mc.KEY_TRIGGER, ARGS_GETSETPUSH
)
Appliance_Control_TriggerX = ns(
    "Appliance.Control.TriggerX", mc.KEY_TRIGGERX, ARGS_GETSETPUSH
)
Appliance_Control_Unbind = ns("Appliance.Control.Unbind", None, ARGS_PUSHQ)
Appliance_Control_Upgrade = ns("Appliance.Control.Upgrade", None, ARGS_NO_Q)
Appliance_Control_Weather = ns("Appliance.Control.Weather", None, ARGS_NO_Q)

Appliance_Digest_TimerX = ns("Appliance.Digest.TimerX", mc.KEY_DIGEST, ARGS_NO_Q)
Appliance_Digest_TriggerX = ns("Appliance.Digest.TriggerX", mc.KEY_DIGEST, ARGS_NO_Q)

Appliance_Encrypt_Suite = ns("Appliance.Encrypt.Suite", None, ARGS_NO_Q)
Appliance_Encrypt_ECDHE = ns("Appliance.Encrypt.ECDHE", None, ARGS_NO_Q)

Appliance_GarageDoor_Config = ns(
    "Appliance.GarageDoor.Config", mc.KEY_CONFIG, ARGS_GETSET
)
Appliance_GarageDoor_MultipleConfig = ns(
    "Appliance.GarageDoor.MultipleConfig", mc.KEY_CONFIG, ARGS_GETSET | P_LIST_C
)
Appliance_GarageDoor_State = ns(
    "Appliance.GarageDoor.State", mc.KEY_STATE, ARGS_GETSETPUSH | G_EXPERIMENTAL
)


Appliance_Mcu_Firmware = ns("Appliance.Mcu.Firmware")
Appliance_Mcu_Upgrade = ns("Appliance.Mcu.Upgrade")

# Smart cherub HP110A
Appliance_Mcu_Hp110_Firmware = ns("Appliance.Mcu.Hp110.Firmware")
Appliance_Mcu_Hp110_Favorite = ns("Appliance.Mcu.Hp110.Favorite")
Appliance_Mcu_Hp110_Preview = ns("Appliance.Mcu.Hp110.Preview")
Appliance_Mcu_Hp110_Lock = ns("Appliance.Mcu.Hp110.Lock")

Appliance_RollerShutter_Adjust = ns(
    "Appliance.RollerShutter.Adjust", mc.KEY_ADJUST, ARGS_PUSHQ | P_LIST
)  # maybe SET supported too
Appliance_RollerShutter_Config = ns(
    "Appliance.RollerShutter.Config", mc.KEY_CONFIG, ARGS_GETSET | P_LIST
)
Appliance_RollerShutter_Position = ns(
    "Appliance.RollerShutter.Position", mc.KEY_POSITION, ARGS_GETSETPUSH | P_LIST
)
Appliance_RollerShutter_State = ns(
    "Appliance.RollerShutter.State", mc.KEY_STATE, ARGS_GETPUSH | P_LIST
)

Appliance_System_Ability = ns("Appliance.System.Ability", mc.KEY_ABILITY, ARGS_GET)
Appliance_System_All = ns("Appliance.System.All", mc.KEY_ALL, ARGS_GET)
Appliance_System_Clock = ns("Appliance.System.Clock", mc.KEY_CLOCK, ARGS_PUSHQ)
Appliance_System_Debug = ns("Appliance.System.Debug", mc.KEY_DEBUG, ARGS_GET)
Appliance_System_DNDMode = ns("Appliance.System.DNDMode", mc.KEY_DNDMODE, ARGS_GET)
Appliance_System_Factory = ns("Appliance.System.Factory", "factory", ARGS_GET)
Appliance_System_Firmware = ns("Appliance.System.Firmware", mc.KEY_FIRMWARE, ARGS_GET)
Appliance_System_Hardware = ns("Appliance.System.Hardware", mc.KEY_HARDWARE, ARGS_GET)
Appliance_System_Online = ns("Appliance.System.Online", mc.KEY_ONLINE, ARGS_GETPUSH)
Appliance_System_Report = ns("Appliance.System.Report", mc.KEY_REPORT, ARGS_PUSH)
Appliance_System_Runtime = ns("Appliance.System.Runtime", mc.KEY_RUNTIME, ARGS_GET)
Appliance_System_Time = ns("Appliance.System.Time", mc.KEY_TIME, ARGS_GETPUSH)
Appliance_System_Position = ns("Appliance.System.Position", mc.KEY_POSITION, ARGS_GET)

"""
Experiment to try declare namespaces as Enum

class NamespaceEnum(Namespace, enum.Enum):
    def __new__(cls, key=None, kwargs={}):
        return Namespace.__new__(cls)

    def __init__(self, key=None, kwargs={}):
        fq_name = self.__class__.__name__.split("_")
        fq_name.append(self._name_)
        fq_name = ".".join(fq_name)
        super().__init__(fq_name, key, kwargs)
        self._value_ = self


class Appliance_System(NamespaceEnum):

    Position = (
        mc.KEY_POSITION,
        _ns_get_args,
    )
"""
