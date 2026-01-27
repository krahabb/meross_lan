"""
Descriptors for namespaces management.
This file contains the knowledge about how namespaces work (their syntax and behaviors).
"""

from copy import deepcopy
import enum
from functools import cached_property
from typing import TYPE_CHECKING

from .. import const as mc

if TYPE_CHECKING:
    from typing import (
        Any,
        Callable,
        Final,
        Mapping,
        NotRequired,
        Protocol,
        TypedDict,
        Unpack,
    )

    from ..types import MerossPayloadType, MerossRequestType

    type NamespacesMapType = Mapping[str, "Namespace"]
    NAMESPACES: Final[NamespacesMapType]
    HUB_NAMESPACES: Final[NamespacesMapType]


def _slug_split(split: str) -> str:
    """Helper to camelCase a namespace split. This is mostly used to infer
    the 'key' of a namespace starting from its (last) split."""
    if split[-1] == "X":
        return f"{split[0].lower()}{split[1:-1]}x"
    else:
        return f"{split[0].lower()}{split[1:]}"


def _heuristic_args(name: str, kwargs: "Namespace.Args") -> "Namespace.Args":
    """Apply some euristics based on the namespace name to deduce
    missing arguments in the namespace definition.
    Beware kwargs is modified in place and returned."""

    kwargs["grammar"] = Grammar.UNKNOWN
    kwargs["payload_set"] = PayloadType.UNKNOWN
    kwargs["payload_del"] = PayloadType.UNKNOWN
    kwargs["payload_psh"] = PayloadType.UNKNOWN

    match name.split("."):
        case (_, "Hub", *_):
            # This is not always true: some 'hub' namespaces don't get indexed by 'id' (nor by 'subId')
            # Examples are ExtraInfo or SubdeviceList. In our definitions we'll solve the problem
            # by explicitly passing the map=HUB_NAMESPACES so that they're mapped into the right storage
            # but the rules for parsing are very custom and likely need to be managed on a case by case
            # at the HubMixin level.
            kwargs["key_channel"] = mc.KEY_ID
        case (_, "RollerShutter", *_):
            kwargs["key_channel"] = mc.KEY_CHANNEL
            kwargs["payload_get"] = PayloadType.LIST_C
        case (_, "GarageDoor", *_):
            kwargs["key_channel"] = mc.KEY_CHANNEL
        case (_, "Control", "Screen", *_):
            kwargs["key_channel"] = mc.KEY_CHANNEL
            kwargs["payload_get"] = PayloadType.LIST_C_STRICT
        case (_, "Control", "Sensor", *_):
            if kwargs.get("map") is HUB_NAMESPACES:
                kwargs["key_channel"] = mc.KEY_SUBID
            else:
                kwargs["key_channel"] = mc.KEY_CHANNEL
            kwargs["payload_get"] = PayloadType.LIST_C_STRICT
        case (_, "Control", "Thermostat", *_):
            kwargs["is_thermostat"] = True
            kwargs["key_channel"] = mc.KEY_CHANNEL
            kwargs["payload_get"] = PayloadType.LIST_C_STRICT
        case _:
            kwargs["payload_get"] = PayloadType.UNKNOWN
    return kwargs


class _NamespacesMap(dict):
    """
    Default general map of Namespace(s).
    This map is populated with a set of static (known) definitions but could also be
    updated at runtime when a new undefined namespace enter the device message pipe.
    """

    def __getitem__(self, name: str) -> "Namespace":
        try:
            return dict.__getitem__(self, name)
        except KeyError:
            return Namespace(
                name,
                _slug_split(name.split(".")[-1]),
                _heuristic_args(name, {"map": self}),
            )

    def get(self, name: str) -> "Namespace | None":
        try:
            return dict.__getitem__(self, name)
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

    def __getitem__(self, name: str) -> "Namespace":
        try:
            return dict.__getitem__(self, name)
        except KeyError:
            if ns := NAMESPACES.get(name):
                return ns
            return Namespace(
                name,
                _slug_split(name.split(".")[-1]),
                _heuristic_args(name, {"map": self}),
            )

    def get(self, name: str) -> "Namespace | None":
        try:
            return dict.__getitem__(self, name)
        except KeyError:
            return NAMESPACES.get(name)


HUB_NAMESPACES = _HubNamespacesMap()


class _immutable:
    def __hash__(self):
        return id(self)

    def __eq__(self, other):
        return self is other

    def _raise(self, *args, **kws):
        raise TypeError(f"object of type <{type(self)}> is immutable")

    clear = _raise
    pop = _raise
    __setitem__ = _raise
    __delitem__ = _raise


class _immutabledict(_immutable, dict[str, "Any"]):
    __ior__ = _immutable._raise  # type: ignore
    update = _immutable._raise  # type: ignore
    setdefault = _immutable._raise  # type: ignore
    pop = _immutable._raise  # type: ignore
    popitem = _immutable._raise  # type: ignore

    def clone(self):
        return {key: deepcopy(value) for key, value in self.items()}


class _immutablelist(_immutable, list[_immutabledict]):
    append = _immutable._raise  # type: ignore
    extend = _immutable._raise  # type: ignore
    insert = _immutable._raise  # type: ignore
    remove = _immutable._raise  # type: ignore
    sort = _immutable._raise  # type: ignore
    __iadd__ = _immutable._raise  # type: ignore
    __imul__ = _immutable._raise  # type: ignore

    def clone(self):
        return [value.clone() for value in self]


EMPTY_DICT: "Final" = _immutabledict()
EMPTY_LIST: "Final" = _immutablelist()


class _PayloadType:

    if TYPE_CHECKING:

        class BuildType(Protocol):
            def __call__(
                self, ns: "Namespace", /, *channels
            ) -> "MerossPayloadType": ...

        build: Final[BuildType]
        prototype: Final[_immutabledict | _immutablelist]  # REMOVE: useless
        indexed: Final[bool]

    __slots__ = (
        "prototype",
        "build",
        "indexed",
        "__dict__",
    )

    def __init__(
        self,
        prototype: dict | list | _immutabledict | _immutablelist,
        build: "BuildType",
        indexed: bool,
        /,
    ):
        if type(prototype) is dict:
            prototype = _immutabledict(prototype)
            self.prototype = prototype
            self.prototype.__class__ = _immutabledict
        elif type(prototype) is list:
            prototype = _immutablelist(prototype)
            self.prototype = prototype
            self.prototype.__class__ = _immutablelist
        else:
            self.prototype = prototype  # type: ignore
        self.build = build
        self.indexed = indexed

    @property
    def value(self):
        return self.prototype

    def build_get(self, ns: "Namespace", /, *channels) -> "MerossRequestType":
        return ns, mc.METHOD_GET, self.build(ns, *channels)


class PayloadType(_PayloadType, enum.Enum):
    """Depicts the payload structure in method queries."""

    @staticmethod
    def _build_unsupported(ns: "Namespace", /, *channels) -> "MerossPayloadType":
        raise NotImplementedError("Unsupported payload type")

    UNSUPPORTED = (
        EMPTY_DICT,
        _build_unsupported,
        False,
    )
    """Method is not supported."""
    UNKNOWN = EMPTY_DICT, lambda *args: EMPTY_DICT, False
    """Method is supported but payload type is unknown."""
    EMPTY = EMPTY_DICT, lambda *args: EMPTY_DICT, False
    """Payload for assigned method is an empty dict."""
    DICT = EMPTY_DICT, lambda ns, *channels: {ns.key: EMPTY_DICT}, False
    """Command GET with {ns_key: {}} returns the state requested."""
    # Payload types for channel based namespaces
    # TODO: manage the 'key_channel' concept better in Namespace class
    DICT_C = (
        EMPTY_DICT,
        lambda ns, *channels: {
            ns.key: (
                {ns.key_channel: channel for channel in channels}
                if channels
                else EMPTY_DICT
            )
        },
        True,
    )
    """Command GET with {ns_key: {}} returns all the (channels) state (key_channel must be defined)."""
    DICT_C_65535 = (
        {mc.KEY_CHANNEL: 65535},
        lambda ns, *channels: {
            ns.key: (
                {ns.key_channel: channel for channel in channels}
                if channels
                else {ns.key_channel: 65535}
            )
        },
        True,
    )
    """Command GET with channel 65535 in dict returns all the channels (only refoss devices ?). Else DICT_C_STRICT."""
    DICT_C_STRICT = (
        {mc.KEY_CHANNEL: 0},
        lambda ns, *channels: {
            ns.key: (
                {ns.key_channel: channel for channel in channels}
                if channels
                else {ns.key_channel: 0}
            )
        },
        True,
    )
    """Command GET with channel index in dict returns the channel state requested."""
    LIST_C = (
        EMPTY_LIST,
        lambda ns, *channels: {
            ns.key: (
                [{ns.key_channel: channel} for channel in channels]
                if channels
                else EMPTY_LIST
            )
        },
        True,
    )
    """Command GET with an empty list returns all the (channels) state (key_channel must be defined)."""
    LIST_C_STRICT = (
        [{mc.KEY_CHANNEL: 0}],
        lambda ns, *channels: {
            ns.key: (
                [{ns.key_channel: channel} for channel in channels]
                if channels
                else [{mc.KEY_CHANNEL: 0}]
            )
        },
        True,
    )
    """Command GET with channel index dicts in a list returns the states requested.
    TODO: it might be feasible that also (empty i.e. no ns_key) EMPTY payload type would work to retrieve the full set
      especially if PUSH_QUERY appeared to work in our traces."""
    LIST_C_DATA_STRICT = (
        [_immutabledict({mc.KEY_CHANNEL: 0, mc.KEY_DATA: []})],
        lambda ns, *channels: {
            ns.key: (
                [{ns.key_channel: channel, mc.KEY_DATA: []} for channel in channels]
                if channels
                else [_immutabledict({mc.KEY_CHANNEL: 0, mc.KEY_DATA: []})]
            )
        },
        True,
    )

    """Command GET for *.LatestX (and maybe *.HistoryX) ns."""

    # Payload types for PUSH verb
    PUSH = EMPTY_DICT, lambda *args: EMPTY_DICT, False
    """Namespace supports async PUSH of the state."""
    PUSH_QUERY = EMPTY_DICT, lambda *args: EMPTY_DICT, False
    """Namespace supports PUSH by client triggering (i.e. client query with method PUSH and EMPTY payload).
    TODO: This might not be a real query feature, instead it might be that ns looking like supporting this
    in our traces are queriable by using (GET, {}) i.e. no ns_key in request payload (
    Actually corresponding to PayloadType.EMPTY)."""


# Semantics helpers symbols:
PUSH_PAYLOADS = (
    PayloadType.UNSUPPORTED,
    PayloadType.UNKNOWN,
    PayloadType.PUSH,
    PayloadType.PUSH_QUERY,
)


class Grammar(enum.StrEnum):
    UNKNOWN = "UNKNOWN"
    """Used to mark namespaces which still don't have a proper normalization (new/unknown ones)."""
    EXPERIMENTAL = "EXPERIMENTAL"
    """Used to mark namespaces for which our normalization is not 100% sure.
    During tracing we'll check every possible verb/payload format despite our knowledge."""
    STABLE = "STABLE"
    """Used to mark namespaces for which our normalization is about 100% correct and complete."""


class Namespace(str):
    """
    Namespace descriptor helper class. This is used to build a definition
    of namespace behaviors and syntax.
    """

    if TYPE_CHECKING:

        class Args(TypedDict):
            """Args are often mututally exclusive and allow for a cascading of heuristics.
            We'll use a set of simple dicts working as small chunks and allowing to build
            concise yet complex definitions (See GET, NO_GET, etc definitions)."""

            map: NotRequired[NamespacesMapType]
            grammar: NotRequired[Grammar]
            key_channel: NotRequired[str]
            payload_get: NotRequired[PayloadType | None]
            payload_set: NotRequired[PayloadType | None]
            payload_del: NotRequired[PayloadType | None]
            payload_psh: NotRequired[PayloadType | None]
            is_thermostat: NotRequired[bool]

        key: Final[str]  # type: ignore
        """The root key of the payload"""
        key_channel: Final[str]  # type: ignore
        """The key used to index items in list payloads. If None/empty no indexing is used."""
        # These indicate support and format for the corresponding verb. None means no support.
        payload_get: Final[PayloadType]  # type: ignore
        """If not None Namespace supports GET verb with this payload type."""
        payload_set: Final[PayloadType]  # type: ignore
        """If not None Namespace supports SET verb with this payload type."""
        payload_del: Final[PayloadType]  # type: ignore
        """If not None Namespace supports DELETE verb with this payload type."""
        payload_psh: Final[PayloadType]  # type: ignore
        """If not None Namespace supports PUSH verb with this payload type."""
        is_thermostat: Final[bool]  # type: ignore
        grammar: Final[Grammar]  # type: ignore
        """The grammar stability level for this namespace."""

    __slots__ = (
        "key",
        "key_channel",
        "payload_get",
        "payload_set",
        "payload_del",
        "payload_psh",
        "is_thermostat",
        "grammar",
        "__dict__",
    )

    @staticmethod
    def infer_key(name: str, payload: "MerossPayloadType") -> str:
        """Infer the namespace key from the namespace name and payload."""
        if len(payload) > 1:
            # Infer the namespace 'key' by looking up some euristic in the payload
            # We typically have the key deducted from the last split of the namespace
            # but it could also come from an intermediate split
            # (see Appliance.Config.DeviceCfg).
            for split in reversed(name.split(".")):
                ns_key = _slug_split(split)
                if ns_key in payload:
                    return ns_key

            # Some namespaces use a totally 'random' concept (see Appliance.Control.Beep -> alarm)
            # so we try to cover some 'likely' candidates here.
            for ns_key in (mc.KEY_ALARM, mc.KEY_CONFIG, mc.KEY_CONTROL):
                if ns_key in payload:
                    return ns_key

        # no better way than hoping the first key is the right one
        for ns_key in payload:
            return ns_key
        else:
            return mc.KEY_

    @staticmethod
    def from_message(
        name: str,
        method: str,
        payload: "MerossPayloadType",
        map: "NamespacesMapType",
        /,
    ):
        if method == mc.METHOD_ERROR:
            return Namespace(
                name,
                _slug_split(name.split(".")[-1]),
                _heuristic_args(name, {"map": map}),
            )
        return Namespace(
            name,
            Namespace.infer_key(name, payload),
            _heuristic_args(name, {"map": map}),
        )

    def __new__(
        cls,
        name: str,
        key: str,
        *args: "Args",
    ):
        return str.__new__(cls, name)

    def __init__(self, name: str, key: str, *args: "Namespace.Args"):
        # We accept multiple args dicts so that we can build complex definitions
        # by composing small 'chunks' like ARGS_GET, ARGS_NO_GET, etc.
        # This also allows us to centralize here the defaults for parameters
        kwargs: "Namespace.Args" = {
            "map": NAMESPACES,
            "grammar": Grammar.STABLE,
            "key_channel": mc.KEY_,
            "is_thermostat": False,
        }
        for _extra in args:
            kwargs.update(_extra)

        self.key = key  # type: ignore
        self.grammar = kwargs["grammar"]  # type: ignore

        # TODO: remove (maybe) some of these flags in favor of just 'key_channel' presence
        for _attr in ("is_thermostat",):
            setattr(self, _attr, kwargs[_attr])

        self.key_channel = kwargs["key_channel"]
        self.payload_get = kwargs.get("payload_get") or PayloadType.UNSUPPORTED
        self.payload_set = kwargs.get("payload_set") or PayloadType.UNSUPPORTED
        self.payload_del = kwargs.get("payload_del") or PayloadType.UNSUPPORTED
        self.payload_psh = kwargs.get("payload_psh") or PayloadType.UNSUPPORTED

        # TODO: check consistencies:
        # for example key_channel must be set if payload_get is any of DICT_C DICT_C_STRICT LIST_C or LIST_C_STRICT
        if self.indexed:
            if not self.key_channel:
                raise ValueError(
                    f"Namespace {self} uses indexed payloads but has no key_channel defined."
                )

        assert (
            self.payload_psh in PUSH_PAYLOADS
        ), f"Namespace {self} has invalid payload_psh {self.payload_psh}"

        kwargs["map"][name] = self  # type: ignore

    @property
    def slug(self) -> str:
        return self.lower().replace(".", "_")

    @cached_property
    def slug_end(self) -> str:
        return _slug_split(self.split(".")[-1])

    @cached_property
    def has_get(self) -> bool:
        return self.payload_get is not PayloadType.UNSUPPORTED

    @cached_property
    def has_set(self) -> bool:
        return self.payload_set is not PayloadType.UNSUPPORTED

    @cached_property
    def has_psh(self) -> bool:
        return self.payload_psh is not PayloadType.UNSUPPORTED

    @cached_property
    def has_psq(self) -> bool:
        return self.payload_psh is PayloadType.PUSH_QUERY

    @cached_property
    def has_del(self) -> bool:
        return self.payload_del is not PayloadType.UNSUPPORTED

    @cached_property
    def indexed(self) -> bool:
        """Indicates if the namespace uses indexed payloads for any verb.
        This is typically true for 'channel based' namespaces."""
        return self.payload_get.indexed or self.payload_set.indexed

    @cached_property
    def request_default(self) -> "MerossRequestType":
        if self.has_get:
            return self.payload_get.build_get(self)
        elif self.has_psq:
            return self, mc.METHOD_PUSH, EMPTY_DICT
        else:
            raise ValueError(
                f"Namespace {self} has no default request (no GET nor PUSH supported)."
            )

    @property
    def can_query(self) -> bool:
        """Indicates if the namespace supports querying (GET or PUSH_QUERY)."""
        return self.has_get or self.has_psq

    @cached_property
    def request_set(self) -> "Callable[..., MerossRequestType]":
        # TODO: articulate request building to cover defaults and unsupported types
        """
        Returns a callable generating a proper SET request for this namespace.
        The callable accepts the payload dict as argument.
        """
        match self.payload_set:
            case PayloadType.LIST_C:
                return self.request_set_list_c
            case PayloadType.DICT_C:
                return self.request_set_dict_c
            case PayloadType.DICT:
                return self.request_set_dict
            case PayloadType.EMPTY:
                return self.request_set_empty
            case _:
                raise Exception(f"{self} namespace does not support SET method")

    def request_set_empty(self, *args) -> "MerossRequestType":
        return self, mc.METHOD_SET, EMPTY_DICT

    def request_set_dict(self, payload, *args) -> "MerossRequestType":
        return self, mc.METHOD_SET, {self.key: payload}

    def request_set_dict_c(self, payload, channel, /) -> "MerossRequestType":
        payload[self.key_channel] = channel
        return (
            self,
            mc.METHOD_SET,
            {self.key: payload},
        )

    def request_set_list_c(self, payload, channel, /) -> "MerossRequestType":
        payload[self.key_channel] = channel
        return (
            self,
            mc.METHOD_SET,
            {self.key: [payload]},
        )


ns = Namespace  # shortcut for declarations

# When using ARGS_ symbols we're explicitly stating that the namespace
# doesn't support the missing verbs.

EXP: "ns.Args" = {"grammar": Grammar.EXPERIMENTAL}

IDX_C: "ns.Args" = {"key_channel": mc.KEY_CHANNEL}
IDX_ID: "ns.Args" = {"key_channel": mc.KEY_ID}
IDX_SUB: "ns.Args" = {"key_channel": mc.KEY_SUBID}

G_E: "ns.Args" = {"payload_get": PayloadType.EMPTY}
G_D: "ns.Args" = {"payload_get": PayloadType.DICT}
G_DC: "ns.Args" = {"payload_get": PayloadType.DICT_C}
G_DCS: "ns.Args" = {"payload_get": PayloadType.DICT_C_STRICT}
G_DC65535: "ns.Args" = {"payload_get": PayloadType.DICT_C_65535}
G_LC: "ns.Args" = {"payload_get": PayloadType.LIST_C}
G_LCS: "ns.Args" = {"payload_get": PayloadType.LIST_C_STRICT}
G_LCDS: "ns.Args" = {"payload_get": PayloadType.LIST_C_DATA_STRICT}

S_E: "ns.Args" = {"payload_set": PayloadType.EMPTY}
S_D: "ns.Args" = {"payload_set": PayloadType.DICT}
S_DC: "ns.Args" = {"payload_set": PayloadType.DICT_C}
S_LC: "ns.Args" = {"payload_set": PayloadType.LIST_C}

D_DC: "ns.Args" = {"payload_del": PayloadType.DICT_C}
D_LC: "ns.Args" = {"payload_del": PayloadType.LIST_C}

PSH: "ns.Args" = {"payload_psh": PayloadType.PUSH}
PSQ: "ns.Args" = {"payload_psh": PayloadType.PUSH_QUERY}


# We predefine grammar for some widely used and well known namespaces either to skip 'euristics'
# and time consuming evaluation.
# Moreover, for some namespaces, the euristics about 'namespace key' and payload structure are not
# good so we must fix those beforehand.
Appliance_Config_Alarm = ns(
    "Appliance.Config.Alarm", mc.KEY_CONFIG, G_LC, S_LC, PSQ, IDX_C, EXP
)
Appliance_Config_DeviceCfg = ns(
    "Appliance.Config.DeviceCfg", mc.KEY_CONFIG, G_LCS, S_LC, IDX_C, PSH
)
Appliance_Config_Info = ns("Appliance.Config.Info", mc.KEY_INFO, G_E, S_D, PSQ)
Appliance_Config_Key = ns("Appliance.Config.Key", mc.KEY_KEY, S_D)
Appliance_Config_Matter = ns("Appliance.Config.Matter", mc.KEY_CONFIG, PSQ)
Appliance_Config_NtpSite = ns("Appliance.Config.NtpSite", mc.KEY_CONFIG)
Appliance_Config_OverTemp = ns("Appliance.Config.OverTemp", mc.KEY_OVERTEMP, G_E, S_D)
Appliance_Config_StandbyKiller = ns(
    "Appliance.Config.StandbyKiller", mc.KEY_CONFIG, G_LCS, S_LC, PSQ, IDX_C
)  # according to Meross app could also support subId indexing
Appliance_Config_Trace = ns("Appliance.Config.Trace", "trace", G_D)
Appliance_Config_Wifi = ns("Appliance.Config.Wifi", mc.KEY_WIFI, S_D)
Appliance_Config_WifiList = ns("Appliance.Config.WifiList", "wifiList", G_E)
Appliance_Config_WifiX = ns("Appliance.Config.WifiX", mc.KEY_WIFI, S_D)

Appliance_Config_Sensor_Association = ns(
    "Appliance.Config.Sensor.Association",
    mc.KEY_CONFIG,
    G_LCS,
    S_LC,
    PSQ,
    IDX_C,
)

Appliance_Control_Alarm = ns(
    "Appliance.Control.Alarm", mc.KEY_ALARM, G_LC, S_LC, IDX_C
)  # mst100/ms130 actually only seen in hub
Appliance_Control_AlertConfig = ns(
    "Appliance.Control.AlertConfig", mc.KEY_CONFIG, G_LCS, S_LC, PSQ, IDX_C
)  # mts300 support the full set of verbs - em06 also exposes it but that's likely different
Appliance_Control_AlertReport = ns(
    "Appliance.Control.AlertReport", mc.KEY_ALERT, G_LCS, S_LC, IDX_C, EXP
)
Appliance_Control_Beep = ns("Appliance.Control.Beep", mc.KEY_ALARM, G_LCS, S_LC, IDX_C)
Appliance_Control_Bind = ns("Appliance.Control.Bind", mc.KEY_BIND)
Appliance_Control_ChangeWifi = ns(
    "Appliance.Control.ChangeWiFi", mc.KEY_
)  # unknown payload
Appliance_Control_CloudEvent = ns(
    "Appliance.Control.CloudEvent", mc.KEY_
)  # unknown payload
Appliance_Control_ConsumptionConfig = ns(
    "Appliance.Control.ConsumptionConfig", mc.KEY_CONFIG, G_E, PSH
)
Appliance_Control_ConsumptionH = ns(
    "Appliance.Control.ConsumptionH", mc.KEY_CONSUMPTIONH, G_LCS, D_LC, IDX_C
)
Appliance_Control_ConsumptionX = ns(
    "Appliance.Control.ConsumptionX", mc.KEY_CONSUMPTIONX, G_E, PSH
)
Appliance_Control_Diffuser_Light = ns(
    "Appliance.Control.Diffuser.Light", mc.KEY_LIGHT, G_E, S_LC, PSQ, IDX_C
)
Appliance_Control_Diffuser_Sensor = ns(
    "Appliance.Control.Diffuser.Sensor", mc.KEY_, G_E, PSH
)  # this ns has no ns_key in payload response
Appliance_Control_Diffuser_Spray = ns(
    "Appliance.Control.Diffuser.Spray", mc.KEY_SPRAY, G_E, S_LC, PSH, IDX_C
)
Appliance_Control_Electricity = ns(
    "Appliance.Control.Electricity", mc.KEY_ELECTRICITY, G_E, PSH
)
Appliance_Control_ElectricityX = ns(
    "Appliance.Control.ElectricityX",
    mc.KEY_ELECTRICITY,
    G_DC65535,
    PSH,
    IDX_C | EXP,
)
Appliance_Control_Fan = ns("Appliance.Control.Fan", mc.KEY_FAN, G_LCS, S_LC, IDX_C)
Appliance_Control_Fan_BtnConfig = ns(
    "Appliance.Control.Fan.BtnConfig", mc.KEY_CONFIG, G_LCS, S_LC, PSQ, IDX_C
)
Appliance_Control_Fan_Config = ns(
    "Appliance.Control.Fan.Config", mc.KEY_CONFIG, G_LCS, S_LC, PSQ, IDX_C
)
Appliance_Control_FilterMaintenance = ns(
    "Appliance.Control.FilterMaintenance", mc.KEY_FILTER, G_LCS, S_LC, PSQ, IDX_C
)
Appliance_Control_Light = ns("Appliance.Control.Light", mc.KEY_LIGHT, G_E, S_DC, IDX_C)
Appliance_Control_Light_Effect = ns(
    "Appliance.Control.Light.Effect", mc.KEY_EFFECT, G_E, S_LC, D_LC, IDX_ID
)
Appliance_Control_Mp3 = ns("Appliance.Control.Mp3", mc.KEY_MP3, G_DC, S_DC, IDX_C)
Appliance_Control_McuUpgrade = ns("Appliance.Control.McuUpgrade", mc.KEY_)
Appliance_Control_Multiple = ns("Appliance.Control.Multiple", mc.KEY_MULTIPLE, S_D)
Appliance_Control_OverTemp = ns("Appliance.Control.OverTemp", mc.KEY_OVERTEMP, PSH)
Appliance_Control_PhysicalLock = ns(
    "Appliance.Control.PhysicalLock", mc.KEY_LOCK, G_LCS, S_LC, PSQ, IDX_C
)
Appliance_Control_Presence_Config = ns(
    "Appliance.Control.Presence.Config", mc.KEY_CONFIG, G_LCS, S_LC, IDX_C
)
Appliance_Control_Presence_Study = ns(
    "Appliance.Control.Presence.Study", mc.KEY_CONFIG, G_LCS, S_LC, PSQ, IDX_C
)
Appliance_Control_Screen_Brightness = ns(
    "Appliance.Control.Screen.Brightness", mc.KEY_BRIGHTNESS, G_LCS, S_LC, PSH, IDX_C
)
# Appliance.Control.Sensor.* appear on both regular devices (ms600) and hub/subdevices (ms130)
# To distinguish the grammar between regular devices and hubs we save different definitions
# in NAMESPACES (for regular devices) and in HUB_NAMESPACES (for hubs).
Appliance_Control_Sensor_Association = ns(
    "Appliance.Control.Sensor.Association", mc.KEY_CONTROL, G_LC, IDX_C
)  # mts300 works: though it seems this ns just returns (in a GET) the list of keys it supports (a kind of grammar).
# We could setup an heuristic handler alone which queries this ns once and then setups some 'config entities'
# working on Appliance.Config.Sensor.Association (which looks like the effective configuration).
Appliance_Control_Sensor_History = ns(
    "Appliance.Control.Sensor.History", mc.KEY_HISTORY, G_LCS, D_LC, IDX_C
)  # history of sensor values
Appliance_Control_Sensor_Latest = ns(
    "Appliance.Control.Sensor.Latest", mc.KEY_LATEST, G_LCS, PSH, IDX_C
)  # carrying miscellaneous sensor values (temp/humi)
Appliance_Control_Sensor_HistoryX = ns(
    "Appliance.Control.Sensor.HistoryX", mc.KEY_HISTORY, G_LCDS, D_LC, IDX_C
)  # cannot get query to work...it might look like LatestX
Appliance_Control_Sensor_LatestX = ns(
    "Appliance.Control.Sensor.LatestX", mc.KEY_LATEST, G_LCDS, PSH, IDX_C
)
Appliance_Control_Spray = ns(
    "Appliance.Control.Spray", mc.KEY_SPRAY, G_D, S_DC, PSH, IDX_C
)
Appliance_Control_TempUnit = ns(
    "Appliance.Control.TempUnit", mc.KEY_TEMPUNIT, G_LCS, S_LC, IDX_C
)
Appliance_Control_Timer = ns(
    "Appliance.Control.Timer", mc.KEY_TIMER, G_E, S_DC, D_DC, IDX_ID
)
Appliance_Control_TimerX = ns(
    "Appliance.Control.TimerX", mc.KEY_TIMERX, G_DC, S_DC, D_DC, IDX_ID
)
Appliance_Control_Toggle = ns("Appliance.Control.Toggle", mc.KEY_TOGGLE, G_D, S_D, PSH)
Appliance_Control_ToggleX = ns(
    "Appliance.Control.ToggleX", mc.KEY_TOGGLEX, G_DC, S_DC, PSH, IDX_C
)
Appliance_Control_Trigger = ns(
    "Appliance.Control.Trigger", mc.KEY_TRIGGER, G_E, S_DC, D_DC, PSH, IDX_ID
)
Appliance_Control_TriggerX = ns(
    "Appliance.Control.TriggerX", mc.KEY_TRIGGERX, G_DC, S_DC, D_DC, PSH, IDX_ID
)
Appliance_Control_Unbind = ns("Appliance.Control.Unbind", mc.KEY_, PSQ)
Appliance_Control_Upgrade = ns(
    "Appliance.Control.Upgrade", "upgrade", S_D
)  # TODO? (check app)
Appliance_Control_Weather = ns("Appliance.Control.Weather", mc.KEY_)

Appliance_Digest_TimerX = ns("Appliance.Digest.TimerX", mc.KEY_DIGEST, G_E)
Appliance_Digest_TriggerX = ns("Appliance.Digest.TriggerX", mc.KEY_DIGEST, G_E)

Appliance_Encrypt_Suite = ns("Appliance.Encrypt.Suite", mc.KEY_, G_E)
Appliance_Encrypt_ECDHE = ns("Appliance.Encrypt.ECDHE", "ecdhe", S_D)

Appliance_GarageDoor_Config = ns("Appliance.GarageDoor.Config", mc.KEY_CONFIG, G_E, S_D)
Appliance_GarageDoor_MultipleConfig = ns(
    "Appliance.GarageDoor.MultipleConfig", mc.KEY_CONFIG, G_LCS, S_LC, IDX_C
)
Appliance_GarageDoor_State = ns(
    "Appliance.GarageDoor.State", mc.KEY_STATE, G_DCS, S_DC, IDX_C, EXP
)


Appliance_Mcu_Firmware = ns("Appliance.Mcu.Firmware", mc.KEY_FIRMWARE, G_E)
Appliance_Mcu_Upgrade = ns("Appliance.Mcu.Upgrade", mc.KEY_UPGRADE, S_D)

# Smart cherub HP110A TODO: try implement features for these namespaces
Appliance_Mcu_Hp110_Favorite = ns(
    "Appliance.Mcu.Hp110.Favorite", "favorite", G_DCS, S_DC, IDX_ID
)
Appliance_Mcu_Hp110_Firmware = ns("Appliance.Mcu.Hp110.Firmware", mc.KEY_FIRMWARE, G_E)
Appliance_Mcu_Hp110_Lock = ns(
    "Appliance.Mcu.Hp110.Lock", mc.KEY_LOCK, G_E, S_D  # TODO: easy implement
)
Appliance_Mcu_Hp110_Preview = ns("Appliance.Mcu.Hp110.Preview", "preview", S_D)


Appliance_RollerShutter_Adjust = ns(
    "Appliance.RollerShutter.Adjust", mc.KEY_ADJUST, S_DC, PSQ, IDX_C
)  # maybe SET supported too and/or GET with EMPTY
Appliance_RollerShutter_Config = ns(
    "Appliance.RollerShutter.Config", mc.KEY_CONFIG, G_LC, S_DC, IDX_C
)
Appliance_RollerShutter_Position = ns(
    "Appliance.RollerShutter.Position", mc.KEY_POSITION, G_LC, S_DC, PSH, IDX_C
)
Appliance_RollerShutter_State = ns(
    "Appliance.RollerShutter.State", mc.KEY_STATE, G_LC, PSH, IDX_C
)

Appliance_System_Ability = ns("Appliance.System.Ability", mc.KEY_ABILITY, G_E)
Appliance_System_All = ns("Appliance.System.All", mc.KEY_ALL, G_E)
Appliance_System_Clock = ns("Appliance.System.Clock", mc.KEY_CLOCK, PSQ)
Appliance_System_Debug = ns("Appliance.System.Debug", mc.KEY_DEBUG, G_E)
Appliance_System_DNDMode = ns("Appliance.System.DNDMode", mc.KEY_DNDMODE, G_E, S_D)
Appliance_System_Factory = ns("Appliance.System.Factory", "factory", G_D, S_D)
Appliance_System_Firmware = ns("Appliance.System.Firmware", mc.KEY_FIRMWARE, G_E)
Appliance_System_Hardware = ns("Appliance.System.Hardware", mc.KEY_HARDWARE, G_E)
Appliance_System_Log = ns("Appliance.System.Log", mc.KEY_)  # unknown payload
Appliance_System_Online = ns("Appliance.System.Online", mc.KEY_ONLINE, G_E, PSH)
Appliance_System_Report = ns("Appliance.System.Report", mc.KEY_REPORT, PSH)
Appliance_System_Runtime = ns("Appliance.System.Runtime", mc.KEY_RUNTIME, G_E)
Appliance_System_Time = ns("Appliance.System.Time", mc.KEY_TIME, G_E, S_D, PSH)
Appliance_System_Position = ns("Appliance.System.Position", mc.KEY_POSITION, G_E, S_D)
