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
        Literal,
        Mapping,
        NotRequired,
        Protocol,
        TypedDict,
        Unpack,
    )

    from .. import types as mt
    from ..types import (
        JsonDict,
        JsonList,
        JsonMapping,
        MerossPayloadType,
        MerossRequestType,
    )

    type NamespacesMapType = Mapping[str, "Namespace"]
    EMPTY_DICT: Final[JsonDict]
    EMPTY_LIST: Final[list[JsonDict]]

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
            kwargs["key_idx"] = mc.KEY_ID
        case (_, "RollerShutter", *_):
            kwargs["key_idx"] = mc.KEY_CHANNEL
            kwargs["payload_get"] = PayloadType.LIST_IDX
        case (_, "GarageDoor", *_):
            kwargs["key_idx"] = mc.KEY_CHANNEL
        case (_, "Control", "Screen", *_):
            kwargs["key_idx"] = mc.KEY_CHANNEL
            kwargs["payload_get"] = PayloadType.LIST_IDX_STRICT
        case (_, "Control", "Sensor", *_):
            if kwargs.get("map") is HUB_NAMESPACES:
                kwargs["key_idx"] = mc.KEY_SUBID
            else:
                kwargs["key_idx"] = mc.KEY_CHANNEL
            kwargs["payload_get"] = PayloadType.LIST_IDX_STRICT
        case (_, "Control", "Thermostat", *_):
            kwargs["key_idx"] = mc.KEY_CHANNEL
            kwargs["payload_get"] = PayloadType.LIST_IDX_STRICT
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
                -1,
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
                -1,
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


EMPTY_DICT = _immutabledict()
EMPTY_LIST = _immutablelist()  # type: ignore


class _PayloadType:

    if TYPE_CHECKING:

        class BuildType(Protocol):
            def __call__(self, ns: "Namespace", /, *idxs) -> "MerossPayloadType": ...

        build: Final[BuildType]
        indexed: Final[bool]

    __slots__ = (
        "build",
        "indexed",
        "__dict__",
    )

    def __init__(
        self,
        build: "BuildType",
        indexed: bool,
        /,
    ):
        self.build = build
        self.indexed = indexed

    def build_get(self, ns: "Namespace", /, *idxs) -> "MerossRequestType":
        return ns, mc.METHOD_GET, self.build(ns, *idxs)


class PayloadType(_PayloadType, enum.Enum):
    """Depicts the payload structure in method queries."""

    @staticmethod
    def _build_unsupported(ns: "Namespace", /, *idxs) -> "MerossPayloadType":
        raise NotImplementedError("Unsupported payload type")

    UNSUPPORTED = (_build_unsupported, False)
    """Method is not supported."""
    UNKNOWN = lambda *args: EMPTY_DICT, False
    """Method is supported but payload type is unknown."""
    EMPTY = lambda *args: EMPTY_DICT, False
    """Payload for assigned method is an empty dict."""
    DICT = lambda ns, *idxs: {ns.key: EMPTY_DICT}, False
    """Command GET with {ns_key: {}} returns the state requested."""
    DICT_IDX = (
        lambda ns, *idxs: {
            ns.key: ({ns.key_idx: idx for idx in idxs} if idxs else EMPTY_DICT)
        },
        True,
    )
    """Command GET with {ns_key: {}} returns all the (channels) state (key_idx must be defined)."""
    DICT_IDX_65535 = (
        lambda ns, *idxs: {
            ns.key: ({ns.key_idx: idx for idx in idxs} if idxs else {ns.key_idx: 65535})
        },
        True,
    )
    """Command GET with channel 65535 in dict returns all the channels (only refoss devices ?). Else DICT_C_STRICT."""
    DICT_IDX_STRICT = (
        lambda ns, *idxs: {
            ns.key: ({ns.key_idx: idx for idx in idxs} if idxs else {ns.key_idx: 0})
        },
        True,
    )
    """Command GET with index in dict returns the channel state requested."""
    LIST_IDX = (
        lambda ns, *idxs: {
            ns.key: ([{ns.key_idx: idx} for idx in idxs] if idxs else EMPTY_LIST)
        },
        True,
    )
    """Command GET with an empty list returns all the (channels) state (key_idx must be defined)."""
    LIST_IDX_STRICT = (
        lambda ns, *idxs: {
            ns.key: (
                [{ns.key_idx: idx} for idx in idxs] if idxs else [{mc.KEY_CHANNEL: 0}]
            )
        },
        True,
    )
    """Command GET with indexed dicts in a list returns the states requested."""
    LIST_IDX_DATA_STRICT = (
        lambda ns, *idxs: {
            ns.key: (
                [{ns.key_idx: idx, mc.KEY_DATA: []} for idx in idxs]
                if idxs
                else [_immutabledict({mc.KEY_CHANNEL: 0, mc.KEY_DATA: []})]
            )
        },
        True,
    )
    """Command GET for *.LatestX (and maybe *.HistoryX) ns."""
    # Payload types for PUSH verb
    PUSH = lambda *args: EMPTY_DICT, False
    """Namespace supports async PUSH of the state."""
    PUSH_QUERY = lambda *args: EMPTY_DICT, False
    """Namespace supports PUSH by client triggering (i.e. client query with method PUSH and EMPTY payload)."""


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

        key: Final[str]
        """The root key of the payload"""
        payload_item_size: Final[int]
        """The average size of an item in the payload list/dict. This might be used to estimate the size of the response."""
        # These indicate support and format for the corresponding verb. None means not supported.
        payload_get: Final[PayloadType]
        """If not None Namespace supports GET verb with this payload type."""
        payload_set: Final[PayloadType]
        """If not None Namespace supports SET verb with this payload type."""
        payload_del: Final[PayloadType]
        """If not None Namespace supports DELETE verb with this payload type."""
        payload_psh: Final[PayloadType]
        """If not None Namespace supports PUSH verb with this payload type."""
        key_idx: Final[str]
        """The key used to index items in list payloads. If None/empty no indexing is used."""
        indexed: Final[bool]
        """Indicates if the namespace uses indexed payloads for any verb. This is typically true for 'index based' namespaces."""
        key_digest: Final[str | None]
        """Indicates the root key in Appliance.System.All payload 'digest' if any."""
        grammar: Final[Grammar]
        """The grammar stability level for this namespace."""

        class Args(TypedDict):
            """Args are often mututally exclusive and allow for a cascading of heuristics.
            We'll use a set of simple dicts working as small chunks and allowing to build
            concise yet complex definitions (See GET, NO_GET, etc definitions)."""

            payload_get: NotRequired[PayloadType | None]
            payload_set: NotRequired[PayloadType | None]
            payload_del: NotRequired[PayloadType | None]
            payload_psh: NotRequired[PayloadType | None]
            key_idx: NotRequired[str]
            key_digest: NotRequired[str | None]  # True allowed (triggers euristics)
            grammar: NotRequired[Grammar]
            map: NotRequired[NamespacesMapType]

    __slots__ = (
        "key",
        "payload_item_size",
        "payload_get",
        "payload_set",
        "payload_del",
        "payload_psh",
        "key_idx",
        "indexed",
        "key_digest",
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
                -1,
                _heuristic_args(name, {"map": map}),
            )
        return Namespace(
            name,
            Namespace.infer_key(name, payload),
            -1,
            _heuristic_args(name, {"map": map}),
        )

    def __new__(
        cls,
        name: str,
        key: str,
        payload_item_size: int,
        *args: "Args",
    ):
        return str.__new__(cls, name)

    def __init__(
        self, name: str, key: str, payload_item_size: int, *args: "Namespace.Args"
    ):
        # We accept multiple args dicts so that we can build complex definitions
        # by composing small 'chunks' like ARGS_GET, ARGS_NO_GET, etc.
        # This also allows us to centralize here the defaults for parameters
        kwargs: "Namespace.Args" = {
            "key_idx": mc.KEY_,
            "key_digest": None,
            "grammar": Grammar.STABLE,
            "map": NAMESPACES,
        }
        for _extra in args:
            kwargs.update(_extra)

        self.key = key  # type: ignore
        self.payload_item_size = payload_item_size
        self.payload_get = kwargs.get("payload_get") or PayloadType.UNSUPPORTED
        self.payload_set = kwargs.get("payload_set") or PayloadType.UNSUPPORTED
        self.payload_del = kwargs.get("payload_del") or PayloadType.UNSUPPORTED
        self.payload_psh = kwargs.get("payload_psh") or PayloadType.UNSUPPORTED
        assert (
            self.payload_psh in PUSH_PAYLOADS
        ), f"Namespace {self} has invalid payload_psh {self.payload_psh}"
        self.key_idx = kwargs["key_idx"]
        if self.payload_get.indexed or self.payload_set.indexed:
            if not self.key_idx:
                raise ValueError(
                    f"Namespace {self} uses indexed payloads but has no key_idx defined."
                )
            self.indexed = True
        else:
            self.indexed = False
        self.key_digest = kwargs["key_digest"]
        if self.key_digest is True:
            # We have a digest but we don't know the root key. We'll try to guess it with some euristics.
            # This is typically true for 'Control' namespaces where the digest structure is not consistent.
            # A little heuristic
            ns_split = name.split(".")

            if len(ns_split) == 4:
                self.key_digest = ns_split[2].lower()
                self.get_digest = self._get_digest_2
            else:
                assert len(ns_split) == 3
                # Appliance.GarageDoor.State is a weird case where the digest key is 'garageDoor'
                self.key_digest = (
                    self.key
                    if ns_split[1] in ("Control", "Digest")
                    else _slug_split(ns_split[1])
                )
                self.get_digest = self._get_digest_1

        self.grammar = kwargs["grammar"]

        kwargs["map"][name] = self  # type: ignore

    @cached_property
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
            case PayloadType.LIST_IDX:
                return self.request_set_list_c
            case PayloadType.DICT_IDX:
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

    def request_set_dict_c(self, payload, *idxs) -> "MerossRequestType":
        for idx in idxs:
            # 'idxs' is expected to be the eventual channel (or any index key) value
            # if empty we assume the payload is already properly structured.
            payload[self.key_idx] = idx
        return (self, mc.METHOD_SET, {self.key: payload})

    def request_set_list_c(self, payload, *idxs) -> "MerossRequestType":
        for idx in idxs:
            payload[self.key_idx] = idx
        return (self, mc.METHOD_SET, {self.key: [payload]})

    def get_digest[_T: "JsonMapping | JsonList"](self, digest: "mt.system.All_Digest") -> _T:  # type: ignore
        """Retrieves the namespace payload/state from the device digest."""
        raise NotImplementedError("Namespace has no digest key defined.")

    def _get_digest_1(self, digest):
        """Helper to get the namespace digest from the device digest."""
        return digest[self.key_digest]

    def _get_digest_2(self, digest):
        """Specialized get_digest for namespaces with 2 levels of digest."""
        return digest[self.key_digest][self.key]


ns = Namespace  # shortcut for declarations

# When using ARGS_ symbols we're explicitly stating that the namespace
# doesn't support the missing verbs.

EXP: "ns.Args" = {"grammar": Grammar.EXPERIMENTAL}

IDX_C: "ns.Args" = {"key_idx": mc.KEY_CHANNEL}
IDX_ID: "ns.Args" = {"key_idx": mc.KEY_ID}
IDX_SUB: "ns.Args" = {"key_idx": mc.KEY_SUBID}

G_E: "ns.Args" = {"payload_get": PayloadType.EMPTY}
G_D: "ns.Args" = {"payload_get": PayloadType.DICT}
G_DI: "ns.Args" = {"payload_get": PayloadType.DICT_IDX}
G_DIS: "ns.Args" = {"payload_get": PayloadType.DICT_IDX_STRICT}
G_DI65535: "ns.Args" = {"payload_get": PayloadType.DICT_IDX_65535}
G_LI: "ns.Args" = {"payload_get": PayloadType.LIST_IDX}
G_LIS: "ns.Args" = {"payload_get": PayloadType.LIST_IDX_STRICT}
G_LIDS: "ns.Args" = {"payload_get": PayloadType.LIST_IDX_DATA_STRICT}

S_E: "ns.Args" = {"payload_set": PayloadType.EMPTY}
S_D: "ns.Args" = {"payload_set": PayloadType.DICT}
S_DI: "ns.Args" = {"payload_set": PayloadType.DICT_IDX}
S_LI: "ns.Args" = {"payload_set": PayloadType.LIST_IDX}

D_DI: "ns.Args" = {"payload_del": PayloadType.DICT_IDX}
D_LI: "ns.Args" = {"payload_del": PayloadType.LIST_IDX}

PSH: "ns.Args" = {"payload_psh": PayloadType.PUSH}
PSQ: "ns.Args" = {"payload_psh": PayloadType.PUSH_QUERY}

DIG: "ns.Args" = {"key_digest": True}  # type: ignore[sentinel]

# We predefine grammar for some widely used and well known namespaces either to skip 'euristics'
# and time consuming evaluation.
# Moreover, for some namespaces, the euristics about 'namespace key' and payload structure are not
# good so we must fix those beforehand.
Appliance_Config_Alarm = ns(
    "Appliance.Config.Alarm", mc.KEY_CONFIG, 44, G_LI, S_LI, PSQ, IDX_C, EXP
)
Appliance_Config_DeviceCfg = ns(
    "Appliance.Config.DeviceCfg", mc.KEY_CONFIG, -1, G_LIS, S_LI, IDX_C, PSH
)
Appliance_Config_Info = ns("Appliance.Config.Info", mc.KEY_INFO, -1, G_E, S_D, PSQ)
Appliance_Config_Key = ns("Appliance.Config.Key", mc.KEY_KEY, -1, S_D)
Appliance_Config_Matter = ns("Appliance.Config.Matter", mc.KEY_CONFIG, -1, PSQ)
Appliance_Config_NtpSite = ns("Appliance.Config.NtpSite", mc.KEY_CONFIG, -1)
Appliance_Config_OverTemp = ns(
    "Appliance.Config.OverTemp", mc.KEY_OVERTEMP, 40, G_E, S_D
)
Appliance_Config_StandbyKiller = ns(
    "Appliance.Config.StandbyKiller", mc.KEY_CONFIG, -1, G_LIS, S_LI, PSQ, IDX_C
)  # according to Meross app could also support subId indexing
Appliance_Config_Trace = ns("Appliance.Config.Trace", "trace", -1, G_D)
Appliance_Config_Wifi = ns("Appliance.Config.Wifi", mc.KEY_WIFI, -1, S_D)
Appliance_Config_WifiList = ns("Appliance.Config.WifiList", "wifiList", -1, G_E)
Appliance_Config_WifiX = ns("Appliance.Config.WifiX", mc.KEY_WIFI, -1, S_D)

Appliance_Config_Sensor_Association = ns(
    "Appliance.Config.Sensor.Association",
    mc.KEY_CONFIG,
    30,
    G_LIS,
    S_LI,
    PSQ,
    IDX_C,
)

Appliance_Control_Alarm = ns(
    "Appliance.Control.Alarm", mc.KEY_ALARM, 40, G_LI, S_LI, IDX_C
)  # mst100/ms130 actually only seen in hub
Appliance_Control_AlertConfig = ns(
    "Appliance.Control.AlertConfig", mc.KEY_CONFIG, -1, G_LIS, S_LI, PSQ, IDX_C
)  # mts300 support the full set of verbs - em06 also exposes it but that's likely different
Appliance_Control_AlertReport = ns(
    "Appliance.Control.AlertReport", mc.KEY_ALERT, -1, G_LIS, S_LI, IDX_C, EXP
)
Appliance_Control_Beep = ns(
    "Appliance.Control.Beep", mc.KEY_ALARM, -1, G_LIS, S_LI, IDX_C
)
Appliance_Control_Bind = ns("Appliance.Control.Bind", mc.KEY_BIND, -1)
Appliance_Control_ChangeWifi = ns(
    "Appliance.Control.ChangeWiFi", mc.KEY_, -1
)  # unknown payload
Appliance_Control_CloudEvent = ns(
    "Appliance.Control.CloudEvent", mc.KEY_, -1
)  # unknown payload
Appliance_Control_ConsumptionConfig = ns(
    "Appliance.Control.ConsumptionConfig", mc.KEY_CONFIG, -1, G_E, PSH
)
Appliance_Control_ConsumptionH = ns(
    "Appliance.Control.ConsumptionH", mc.KEY_CONSUMPTIONH, 1900, G_LIS, D_LI, IDX_C
)
Appliance_Control_ConsumptionX = ns(
    "Appliance.Control.ConsumptionX", mc.KEY_CONSUMPTIONX, 53, G_E, PSH
)
Appliance_Control_Diffuser_Light = ns(
    "Appliance.Control.Diffuser.Light", mc.KEY_LIGHT, 110, G_E, S_LI, PSQ, IDX_C, DIG
)
Appliance_Control_Diffuser_Sensor = ns(
    "Appliance.Control.Diffuser.Sensor", mc.KEY_, 100, G_E, PSH
)  # this ns has no ns_key in payload response
Appliance_Control_Diffuser_Spray = ns(
    "Appliance.Control.Diffuser.Spray", mc.KEY_SPRAY, 55, G_E, S_LI, PSH, IDX_C, DIG
)
Appliance_Control_Electricity = ns(
    "Appliance.Control.Electricity", mc.KEY_ELECTRICITY, 130, G_E, PSH
)
Appliance_Control_ElectricityX = ns(
    "Appliance.Control.ElectricityX",
    mc.KEY_ELECTRICITY,
    100,
    G_DI65535,
    PSH,
    IDX_C | EXP,
)
Appliance_Control_Fan = ns(
    "Appliance.Control.Fan", mc.KEY_FAN, 20, G_LIS, S_LI, IDX_C, DIG
)
Appliance_Control_Fan_BtnConfig = ns(
    "Appliance.Control.Fan.BtnConfig", mc.KEY_CONFIG, -1, G_LIS, S_LI, PSQ, IDX_C
)
Appliance_Control_Fan_Config = ns(
    "Appliance.Control.Fan.Config", mc.KEY_CONFIG, -1, G_LIS, S_LI, PSQ, IDX_C
)
Appliance_Control_FilterMaintenance = ns(
    "Appliance.Control.FilterMaintenance", mc.KEY_FILTER, 35, G_LIS, S_LI, PSQ, IDX_C
)
Appliance_Control_Light = ns(
    "Appliance.Control.Light", mc.KEY_LIGHT, -1, G_E, S_DI, IDX_C, DIG
)
Appliance_Control_Light_Effect = ns(
    "Appliance.Control.Light.Effect", mc.KEY_EFFECT, 1550, G_E, S_LI, D_LI, IDX_ID
)
Appliance_Control_Mp3 = ns("Appliance.Control.Mp3", mc.KEY_MP3, 80, G_DI, S_DI, IDX_C)
Appliance_Control_McuUpgrade = ns("Appliance.Control.McuUpgrade", mc.KEY_, -1)
Appliance_Control_Multiple = ns("Appliance.Control.Multiple", mc.KEY_MULTIPLE, -1, S_D)
Appliance_Control_OverTemp = ns("Appliance.Control.OverTemp", mc.KEY_OVERTEMP, -1, PSH)
Appliance_Control_PhysicalLock = ns(
    "Appliance.Control.PhysicalLock", mc.KEY_LOCK, 35, G_LIS, S_LI, PSQ, IDX_C
)
Appliance_Control_Presence_Config = ns(
    "Appliance.Control.Presence.Config", mc.KEY_CONFIG, 260, G_LIS, S_LI, IDX_C
)
Appliance_Control_Presence_Study = ns(
    "Appliance.Control.Presence.Study", mc.KEY_CONFIG, -1, G_LIS, S_LI, PSQ, IDX_C
)
Appliance_Control_Screen_Brightness = ns(
    "Appliance.Control.Screen.Brightness",
    mc.KEY_BRIGHTNESS,
    70,
    G_LIS,
    S_LI,
    PSH,
    IDX_C,
)
# Appliance.Control.Sensor.* appear on both regular devices (ms600) and hub/subdevices (ms130)
# To distinguish the grammar between regular devices and hubs we save different definitions
# in NAMESPACES (for regular devices) and in HUB_NAMESPACES (for hubs).
Appliance_Control_Sensor_Association = ns(
    "Appliance.Control.Sensor.Association", mc.KEY_CONTROL, -1, G_LI, IDX_C
)  # mts300 works: though it seems this ns just returns (in a GET) the list of keys it supports (a kind of grammar).
# We could setup an heuristic handler alone which queries this ns once and then setups some 'config entities'
# working on Appliance.Config.Sensor.Association (which looks like the effective configuration).
Appliance_Control_Sensor_History = ns(
    "Appliance.Control.Sensor.History", mc.KEY_HISTORY, -1, G_LIS, D_LI, IDX_C
)  # history of sensor values
Appliance_Control_Sensor_Latest = ns(
    "Appliance.Control.Sensor.Latest", mc.KEY_LATEST, 80, G_LIS, PSH, IDX_C
)  # carrying miscellaneous sensor values (temp/humi)
Appliance_Control_Sensor_HistoryX = ns(
    "Appliance.Control.Sensor.HistoryX", mc.KEY_HISTORY, -1, G_LIDS, D_LI, IDX_C
)  # cannot get query to work...it might look like LatestX
Appliance_Control_Sensor_LatestX = ns(
    "Appliance.Control.Sensor.LatestX", mc.KEY_LATEST, 220, G_LIDS, PSH, IDX_C
)
Appliance_Control_Spray = ns(
    "Appliance.Control.Spray", mc.KEY_SPRAY, 90, G_D, S_DI, PSH, IDX_C, DIG
)
Appliance_Control_TempUnit = ns(
    "Appliance.Control.TempUnit", mc.KEY_TEMPUNIT, 30, G_LIS, S_LI, IDX_C
)
Appliance_Control_Timer = ns(
    "Appliance.Control.Timer", mc.KEY_TIMER, -1, G_E, S_DI, D_DI, IDX_ID, DIG
)  # digest key likely referring to 'control' key (like Appliance.Control.Toggle)
Appliance_Control_TimerX = ns(
    "Appliance.Control.TimerX", mc.KEY_TIMERX, -1, G_DI, S_DI, D_DI, IDX_ID, DIG
)  # ns indexed by both 'channel' and 'id'
Appliance_Control_Toggle = ns(
    "Appliance.Control.Toggle", mc.KEY_TOGGLE, 40, G_D, S_D, PSH, DIG
)  # digest key points to 'control' key in Appliance.System.All payload
Appliance_Control_ToggleX = ns(
    "Appliance.Control.ToggleX", mc.KEY_TOGGLEX, 55, G_DI, S_DI, PSH, IDX_C, DIG
)
Appliance_Control_Trigger = ns(
    "Appliance.Control.Trigger", mc.KEY_TRIGGER, -1, G_E, S_DI, D_DI, PSH, IDX_ID, DIG
)  # digest key likely referring to 'control' key (like Appliance.Control.Toggle)
Appliance_Control_TriggerX = ns(
    "Appliance.Control.TriggerX",
    mc.KEY_TRIGGERX,
    -1,
    G_DI,
    S_DI,
    D_DI,
    PSH,
    IDX_ID,
    DIG,
)  # ns indexed by both 'channel' and 'id'
Appliance_Control_Unbind = ns("Appliance.Control.Unbind", mc.KEY_, -1, PSQ)
Appliance_Control_Upgrade = ns(
    "Appliance.Control.Upgrade", "upgrade", -1, S_D
)  # TODO? (check app)
Appliance_Control_Weather = ns("Appliance.Control.Weather", mc.KEY_, -1)

Appliance_Digest_TimerX = ns("Appliance.Digest.TimerX", mc.KEY_DIGEST, -1, G_E)
Appliance_Digest_TriggerX = ns("Appliance.Digest.TriggerX", mc.KEY_DIGEST, -1, G_E)

Appliance_Encrypt_Suite = ns("Appliance.Encrypt.Suite", mc.KEY_, -1, G_E)
Appliance_Encrypt_ECDHE = ns("Appliance.Encrypt.ECDHE", "ecdhe", -1, S_D)

Appliance_GarageDoor_Config = ns(
    "Appliance.GarageDoor.Config", mc.KEY_CONFIG, 110, G_E, S_D
)
Appliance_GarageDoor_MultipleConfig = ns(
    "Appliance.GarageDoor.MultipleConfig", mc.KEY_CONFIG, 140, G_LIS, S_LI, IDX_C
)
Appliance_GarageDoor_State = ns(
    "Appliance.GarageDoor.State", mc.KEY_STATE, -1, G_DIS, S_DI, IDX_C, DIG, EXP
)

Appliance_Mcu_Firmware = ns("Appliance.Mcu.Firmware", mc.KEY_FIRMWARE, 80, G_E)
Appliance_Mcu_Upgrade = ns("Appliance.Mcu.Upgrade", mc.KEY_UPGRADE, -1, S_D)

# Smart cherub HP110A TODO: try implement features for these namespaces
Appliance_Mcu_Hp110_Favorite = ns(
    "Appliance.Mcu.Hp110.Favorite", "favorite", -1, G_DIS, S_DI, IDX_ID
)
Appliance_Mcu_Hp110_Firmware = ns(
    "Appliance.Mcu.Hp110.Firmware", mc.KEY_FIRMWARE, 80, G_E
)
Appliance_Mcu_Hp110_Lock = ns(
    "Appliance.Mcu.Hp110.Lock", mc.KEY_LOCK, -1, G_E, S_D  # TODO: easy implement
)
Appliance_Mcu_Hp110_Preview = ns("Appliance.Mcu.Hp110.Preview", "preview", -1, S_D)


Appliance_RollerShutter_Adjust = ns(
    "Appliance.RollerShutter.Adjust", mc.KEY_ADJUST, 35, S_DI, PSQ, IDX_C
)  # maybe SET supported too and/or GET with EMPTY
Appliance_RollerShutter_Config = ns(
    "Appliance.RollerShutter.Config", mc.KEY_CONFIG, 70, G_LI, S_DI, IDX_C
)
Appliance_RollerShutter_Position = ns(
    "Appliance.RollerShutter.Position", mc.KEY_POSITION, 50, G_LI, S_DI, PSH, IDX_C
)
Appliance_RollerShutter_State = ns(
    "Appliance.RollerShutter.State", mc.KEY_STATE, 40, G_LI, PSH, IDX_C
)

Appliance_System_Ability = ns("Appliance.System.Ability", mc.KEY_ABILITY, -1, G_E)
Appliance_System_All = ns("Appliance.System.All", mc.KEY_ALL, 700, G_E)
Appliance_System_Clock = ns("Appliance.System.Clock", mc.KEY_CLOCK, -1, PSQ)
Appliance_System_Debug = ns("Appliance.System.Debug", mc.KEY_DEBUG, 1600, G_E)
Appliance_System_DNDMode = ns("Appliance.System.DNDMode", mc.KEY_DNDMODE, 30, G_E, S_D)
Appliance_System_Factory = ns("Appliance.System.Factory", "factory", -1, G_D, S_D)
Appliance_System_Firmware = ns("Appliance.System.Firmware", mc.KEY_FIRMWARE, -1, G_E)
Appliance_System_Hardware = ns("Appliance.System.Hardware", mc.KEY_HARDWARE, -1, G_E)
Appliance_System_Log = ns("Appliance.System.Log", mc.KEY_, -1)  # unknown payload
Appliance_System_Online = ns("Appliance.System.Online", mc.KEY_ONLINE, -1, G_E, PSH)
Appliance_System_Report = ns("Appliance.System.Report", mc.KEY_REPORT, -1, PSH)
Appliance_System_Runtime = ns("Appliance.System.Runtime", mc.KEY_RUNTIME, 30, G_E)
Appliance_System_Time = ns("Appliance.System.Time", mc.KEY_TIME, -1, G_E, S_D, PSH)
Appliance_System_Position = ns(
    "Appliance.System.Position", mc.KEY_POSITION, -1, G_E, S_D
)
