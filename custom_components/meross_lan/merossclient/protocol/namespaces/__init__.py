"""
Descriptors for namespaces management.
This file contains the knowledge about how namespaces work (their syntax and behaviors).
"""

from copy import deepcopy
import enum
from functools import cached_property
from typing import TYPE_CHECKING, override

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

    # Use Mapping so it is not mutable but still subscriptable for type checking purposes.
    # The actual type is a custom dict to allow dynamic Namespace creation.
    type NamespacesMapType = Mapping[str, "Namespace"]
    EMPTY_DICT: Final[JsonDict]
    EMPTY_LIST: Final[list[JsonDict]]

    NAMESPACES: Final[NamespacesMapType]


def _slug_split(split: str) -> str:
    """Helper to camelCase a namespace split. This is mostly used to infer
    the 'key' of a namespace starting from its (last) split."""
    if split[-1] == "X":
        return f"{split[0].lower()}{split[1:-1]}x"
    else:
        return f"{split[0].lower()}{split[1:]}"


def _heuristic_args(name: str, /) -> "Namespace.Args":
    """Apply some euristics based on the namespace name to deduce
    missing arguments in the namespace definition.
    Beware kwargs is modified in place and returned."""

    kwargs: "Namespace.Args" = {
        "grammar": Grammar.UNKNOWN,
        "payload_set": PayloadType.UNKNOWN,
        "payload_del": PayloadType.UNKNOWN,
        "payload_psh": PayloadType.UNKNOWN,
    }

    match name.split("."):
        case (_, "Hub", *_):
            # This is not always true: some 'hub' namespaces don't get indexed by 'id' (nor by 'subId')
            kwargs["index_type"] = IndexType.id
        case (_, "RollerShutter", *_):
            kwargs["index_type"] = IndexType.channel
            kwargs["payload_get"] = PayloadType.LIST_IDX
        case (_, "GarageDoor", *_):
            kwargs["index_type"] = IndexType.channel
        case (_, "Control", "Screen", *_):
            kwargs["index_type"] = IndexType.channel
            kwargs["payload_get"] = PayloadType.LIST_IDX_STRICT
        case (_, "Control", "Sensor", *_):
            kwargs["index_type"] = IndexType.subId
            kwargs["payload_get"] = PayloadType.LIST_IDX_STRICT
        case (_, "Control", "Thermostat", *_):
            kwargs["index_type"] = IndexType.channel
            kwargs["payload_get"] = PayloadType.LIST_IDX_STRICT
        case _:
            kwargs["payload_get"] = PayloadType.UNKNOWN
    return kwargs


class _NamespacesMap(dict):
    """
    Default general map of Namespace(s).
    This map is populated with a set of static (known) definitions but could also be
    updated at runtime when a new undefined namespace enter the device message pipe.
    BEWARE:
    Since the ns definitions are split in multiple modules, we need to be sure those are
    loaded so that the corresponding Namespace instances are created and registered in this map.
    """

    # TODO: implement __missing__ semantics
    def __getitem__(self, name: str) -> "Namespace":
        try:
            return dict.__getitem__(self, name)
        except KeyError:
            return Namespace(
                name,
                _slug_split(name.split(".")[-1]),
                -1,
                _heuristic_args(name),
            )

    def get(self, name: str) -> "Namespace | None":
        try:
            return dict.__getitem__(self, name)
        except KeyError:
            return None


NAMESPACES = _NamespacesMap()


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


class _IndexType(tuple):

    if TYPE_CHECKING:
        cache: Final[dict[tuple, "IndexValue"]]
        class_value: Final[type["IndexValue"]]

    # __slots__ = ("cache",)
    cache = {}  # type: ignore

    def __new__(cls, class_value, keys):
        return tuple.__new__(cls, keys)

    def __init__(self, class_value: type["IndexValue"], keys: tuple[str, ...]):
        self.cache = {}
        self.class_value = class_value


class IndexValue(_immutabledict):

    if TYPE_CHECKING:
        type: Final["IndexType"]
        value: Final[str | int | tuple | None]  # type: ignore[polymorphic]
        slug: Final[str | int | None]  # type: ignore[polymorphic]

    __slots__ = ("type", "value", "slug", "_hash")

    def __init__(self, index_type: "IndexType", _dict: "JsonDict", *values):
        super().__init__(_dict)
        self.type = index_type
        self._hash = hash(self.value)
        index_type.cache[values] = self

    def matches(self, payload: dict):
        # Just a conceptual definition..sort of
        # This should actually not be called anyway
        return all(payload.get(k) == v for k, v in self.items())

    # IndexValue is immutable and hashable based on its content (type and value)
    # so that it can be used as dict key in the parsers attribute of NamespaceHandler.
    # This check should be as fast as possible since it'll be accessed
    # when looking up parsers
    def __eq__(self, other):
        return self.value == other or (
            # assuming no subclassing and the fact that our indexes have different
            # value types ('id' vs 'channel' vs 'subId/channel')
            type(other) is IndexValue
            and self.value == other.value
        )

    def __hash__(self):
        return self._hash

    def __repr__(self):
        return f"IndexValue(type={self.type}, value={self.value}, slug='{self.slug}')"


class NoneIndexValue(IndexValue):

    if TYPE_CHECKING:
        value: Final[None]  # type: ignore[override]
        slug: Final[None]  # type: ignore[override]

    def __init__(self, index_type: "IndexType"):
        self.value = None
        self.slug = None
        self.matches = lambda payload: True
        IndexValue.__init__(self, index_type, EMPTY_DICT)


class SimpleIndexValue(IndexValue):

    if TYPE_CHECKING:
        value: Final[int | str]  # type: ignore[override]
        slug: Final[int | str]  # type: ignore[override]

    def __init__(self, index_type: "IndexType", value: int | str):
        self.value = value
        self.slug = value
        self.matches = lambda payload: payload.get(index_type[0]) == value
        IndexValue.__init__(self, index_type, {index_type[0]: self.value}, value)


class SubIdIndexValue(IndexValue):
    """This kind of payload indexing is very articulated since it may have the following patterns:
    - no 'subId' but only 'channel' (typically for hub namespaces related to the hub itself and not to
    subdevices, e.g. Appliance.Control.Alarm or for 'hybrid' namespaces which appear on non hub devices
    and thus only support 'channel' indexing but then appear on the hub with 'subId/channel'
    indexing like Appliance.Config.DeviceCfg).
    For this case the index value will be the channel alone.
    - for mst200 subdevices Appliance.Control.Water introduces what seems a 'custom' indexing
    where the channel is not carried in a simple 'channel' key but in a list of channels under 'channels' key.
    For this case we assume that the list will always contain only one channel. Nevertheless, our
    IndexValue must refer to a single parser channel so we'll always reduce the representation
    to a (subId, channel) tuple.
    """

    if TYPE_CHECKING:
        value: Final[int | tuple[str, int]]  # type: ignore[override]
        slug: Final[int | str]  # type: ignore[override]

    def __init__(
        self, index_type, subid: str | None, channel: int | None, channels: int | None
    ):
        if subid:
            # "channel" and "channels" should be mutually exclusive.
            if channels is None:
                assert channel is not None
                _dict = {index_type[0]: subid, index_type[1]: channel}
                self.value = (subid, channel)
                self.slug = f"{subid}_{channel}"
            else:
                assert channel is None
                _dict = {index_type[0]: subid, index_type[2]: [channels]}
                self.value = (subid, channels)
                self.slug = f"{subid}_{channels}"
        else:
            assert channels is None and channel is not None
            _dict = {index_type[1]: channel}
            self.value = channel
            self.slug = channel
            self.matches = lambda payload: payload.get(index_type[1]) == channel
        IndexValue.__init__(self, index_type, _dict, subid, channel, channels)

    @override
    def matches(self, payload: dict):
        assert (
            type(self.value) is tuple
        ), "matches should be overridden for non subId indexes"
        try:
            return (
                payload[mc.KEY_SUBID] == self.value[0]
                and payload[mc.KEY_CHANNEL] == self.value[1]
            )
        except KeyError as ke:
            if ke.args[0] != mc.KEY_CHANNEL:
                return False
            # This is the case of a payload with 'channels' instead of 'channel' key (mst200 subdevice)
            try:
                return payload[mc.KEY_SUBID] == self.value[0] and payload[
                    mc.KEY_CHANNELS
                ] == [self.value[1]]
            except KeyError:
                return False


class IndexType(_IndexType, enum.Enum):
    none = NoneIndexValue, ()
    channel = SimpleIndexValue, (mc.KEY_CHANNEL,)
    id = SimpleIndexValue, (mc.KEY_ID,)
    subId = SubIdIndexValue, (mc.KEY_SUBID, mc.KEY_CHANNEL, mc.KEY_CHANNELS)
    Id = SimpleIndexValue, (mc.KEY_ID_,)

    def __call__(self, *values):
        """IndexValue Factory method:
        builds or retrieve an IndexValue instance for this IndexType with the given key value(s).
        The values tuple is used to lookup a matching definition in a static cache for this IndexType. If no match is found,
        a new IndexValue instance is created with the given value(s) and stored in the cache for future retrieval.
        The values tuple must be a fully qualified set of values that matches the IndexType definition.
        The items in the tuple will be used to populate a prototype dict for the IndexValue instance.
        The tuple cardinality must match the number of keys that define the IndexType.
        For IndexType.subId the tuple must be (subId, channel, channels) where channel and channels are mutually exclusive
        and only one of them can be not None.
        """
        try:
            return self.cache[values]
        except KeyError:
            return self.class_value(self, *values)
        except Exception as e:
            raise

    def index(self, payload: "JsonMapping"):
        """IndexValue Factory method:
        extracts the key values of this index type from the payload dict and
        returns the corresponding IndexValue instance from the cache, creating it if necessary.
        Uses __call__ to actually build the IndexValue instance if not found in cache.
        """
        return self(*(payload.get(k) for k in self))

    def value(self, payload: "JsonMapping"):
        return self.index(payload).value

    def slug(self, payload: "JsonMapping"):
        return self.index(payload).slug


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

    def build_get(self, ns: "Namespace", /, *idxs: IndexValue) -> "MerossRequestType":
        return ns, mc.METHOD_GET, self.build(ns, *idxs)


class PayloadType(_PayloadType, enum.Enum):
    """Depicts the payload structure in namespace queries."""

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
        lambda ns, *idxs: {ns.key: (idxs[0] if idxs else EMPTY_DICT)},
        True,
    )
    """Command GET with {ns_key: {}} returns all the (channels) state (index must be defined)."""
    DICT_IDX_65535 = (
        lambda ns, *idxs: {ns.key: idxs[0] if idxs else {mc.KEY_CHANNEL: 65535}},
        True,
    )
    """Command GET with channel 65535 in dict returns all the channels (only refoss devices ?). Else DICT_C_STRICT."""
    DICT_IDX_STRICT = (
        lambda ns, *idxs: {ns.key: idxs[0] if idxs else {mc.KEY_CHANNEL: 0}},
        True,
    )
    """Command GET with index in dict returns the channel state requested."""
    LIST_IDX = (
        lambda ns, *idxs: {ns.key: [*idxs]},
        True,
    )
    """Command GET with an empty list returns all the (channels) state (index must be defined)."""
    LIST_IDX_STRICT = (
        lambda ns, *idxs: {ns.key: ([*idxs] if idxs else [{mc.KEY_CHANNEL: 0}])},
        True,
    )
    """Command GET with indexed dicts in a list returns the states requested."""
    LIST_IDX_DATA_STRICT = (
        # FIXME
        lambda ns, *idxs: {
            ns.key: (
                [{mc.KEY_CHANNEL: idx, mc.KEY_DATA: []} for idx in idxs]
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
        index_type: Final[IndexType]
        """The key used to index items in list payloads. If None/empty no indexing is used.
        Special care need to be used when a namespace is declared to be indexed by 'subId'
        since these namespaces might also carry only 'channel' payloads (for non hub devices or
        for features related to the hub itself and not to a subdevice).
        Typical example is Appliance.Control.Alarm where we might configure the hub alarm (only 'channel' == 0)
        and/or a subdevice alarm feature (both 'subId/channel' are present even though 'channel' is almost always == 0)."""
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
            index_type: NotRequired[IndexType]
            key_digest: NotRequired[str | None]  # True allowed (triggers euristics)
            grammar: NotRequired[Grammar]

    __slots__ = (
        "key",
        "payload_item_size",
        "payload_get",
        "payload_set",
        "payload_del",
        "payload_psh",
        "index_type",
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
    def from_message(name: str, method: str, payload: "MerossPayloadType", /):
        if method == mc.METHOD_ERROR:
            return Namespace(
                name,
                _slug_split(name.split(".")[-1]),
                -1,
                _heuristic_args(name),
            )
        return Namespace(
            name,
            Namespace.infer_key(name, payload),
            -1,
            _heuristic_args(name),
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
            "index_type": IndexType.none,
            "key_digest": None,
            "grammar": Grammar.STABLE,
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
        self.index_type = kwargs["index_type"]
        if self.payload_get.indexed or self.payload_set.indexed:
            if not self.index_type:
                raise ValueError(
                    f"Namespace {self} uses indexed payloads but has no index defined."
                )

        self.key_digest = kwargs["key_digest"]
        if self.key_digest is True:
            # We have a digest but we don't know the root key. We'll try to guess it with some euristics.
            # This is typically true for 'Appliance.Control.*' namespaces where the digest structure is consistent.
            ns_split = name.split(".")
            if len(ns_split) == 4:
                # Typically Appliance.Control.Thermostat.*
                self.key_digest = ns_split[2].lower()
                # ns digest is in ["all"]["digest"][{self.key_digest}][{self.key}]
                self.get_digest = self._get_digest_2
            else:
                assert len(ns_split) == 3
                # Appliance.Control.* ns digest is typically in ["all"]["digest"][{self.key}]
                # but we have to be sure about the key to look up
                # Appliance.GarageDoor.State is a weird case ["all"]["digest"]["garageDoor"]
                self.key_digest = (
                    self.key
                    if ns_split[1] in ("Control", "Digest")
                    else _slug_split(ns_split[1])
                )
                self.get_digest = self._get_digest_1
        elif self.key_digest:
            # key_digest explicitly set in constructor args
            self.get_digest = self._get_digest_1

        self.grammar = kwargs["grammar"]

        NAMESPACES[name] = self  # type: ignore

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

    def request_set_empty(self, /) -> "MerossRequestType":
        return self, mc.METHOD_SET, EMPTY_DICT

    def request_set_dict(self, payload, /) -> "MerossRequestType":
        return self, mc.METHOD_SET, {self.key: payload}

    def request_set_dict_c(self, payload, /) -> "MerossRequestType":
        return (self, mc.METHOD_SET, {self.key: payload})

    def request_set_list_c(self, payload, /) -> "MerossRequestType":
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

    def __repr__(self):
        return f"Namespace({self}, key={self.key}, index={self.index_type})"


ns = Namespace  # shortcut for declarations

# When using ARGS_ symbols we're explicitly stating that the namespace
# doesn't support the missing verbs.

EXP: "ns.Args" = {"grammar": Grammar.EXPERIMENTAL}

IDX_C: "ns.Args" = {"index_type": IndexType.channel}  # Channel index
IDX_ID_: "ns.Args" = {"index_type": IndexType.Id}  # Item (effect) Id
IDX_ID: "ns.Args" = {
    "index_type": IndexType.id
}  # Hub subdevice id (but also trigger,timer)
IDX_SUB: "ns.Args" = {"index_type": IndexType.subId}  # Hub subdevice id

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
    "Appliance.Config.Alarm", mc.KEY_CONFIG, 44, G_LI, S_LI, PSQ, IDX_SUB
)
Appliance_Config_DeviceCfg = ns(
    "Appliance.Config.DeviceCfg", mc.KEY_CONFIG, 100, G_LIS, S_LI, PSH, IDX_SUB
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
    "Appliance.Config.Sensor.Association", mc.KEY_CONFIG, 30, G_LIS, S_LI, PSQ, IDX_SUB
)

Appliance_Control_Alarm = ns(
    "Appliance.Control.Alarm", mc.KEY_ALARM, 40, G_LI, S_LI, IDX_SUB
)  # mst100/ms130 actually only seen in hub
Appliance_Control_AlertConfig = ns(
    "Appliance.Control.AlertConfig", mc.KEY_CONFIG, 70, G_LIS, S_LI, PSH, IDX_SUB
)  # mts300 support the full set of verbs - em06 also exposes it but that's likely different
Appliance_Control_AlertReport = ns(
    "Appliance.Control.AlertReport", mc.KEY_ALERT, -1, G_LIS, S_LI, IDX_SUB
)  # no trace of handlingthis ns in Meross App
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
    "Appliance.Control.Light.Effect",
    mc.KEY_EFFECT,
    1550,
    G_E,
    S_LI,
    D_LI,
    IDX_ID_,
    {"key_digest": mc.KEY_LIGHT_EFFECT},
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
    "Appliance.Control.Sensor.Association", mc.KEY_CONTROL, -1, G_LI, IDX_SUB
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
    "Appliance.Control.Sensor.HistoryX", mc.KEY_HISTORY, 1000, G_LIDS, D_LI, IDX_SUB
)  # cannot get query to work...it might look like LatestX
Appliance_Control_Sensor_LatestX = ns(
    "Appliance.Control.Sensor.LatestX", mc.KEY_LATEST, 220, G_LIDS, PSH, IDX_SUB
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
    "Appliance.Control.ToggleX", mc.KEY_TOGGLEX, 50, G_DI, S_DI, PSH, IDX_C, DIG
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

# This is to ease full loading of NAMESPACES map since definitions are split among multiple files.
from . import hub, thermostat  # noqa: F401
