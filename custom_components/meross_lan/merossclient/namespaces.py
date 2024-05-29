"""
Descriptors for namespaces management.
This file contains the knowledge about how namespaces work (their syntax and behaviors).
"""

from functools import cached_property
import re
import typing

from . import const as mc

if typing.TYPE_CHECKING:
    from . import MerossRequestType


class _NamespacesMap(dict):
    """
    Map a namespace to the main key carrying the asociated payload.
    This map is incrementally built at runtime (so we don't waste time manually coding this)
    whenever we use it
    """

    def __getitem__(self, namespace: str) -> "Namespace":
        try:
            return super().__getitem__(namespace)
        except KeyError:
            return Namespace(namespace)


NAMESPACES: dict[str, "Namespace"] = _NamespacesMap()


class Namespace:
    """
    Namespace descriptor helper class. This is used to build a definition
    of namespace behaviors and syntax.
    """

    DEFAULT_PUSH_PAYLOAD: typing.Final = {}

    has_get: bool | None
    """ns supports method GET"""
    has_push: bool | None
    """ns supports method PUSH"""
    need_channel: bool
    """ns needs the channel index in standard GET queries"""
    payload_get_inner: list | dict | None
    """when set it depicts the structure of the inner payload in GET queries"""

    __slots__ = (
        "name",
        "has_get",
        "has_push",
        "need_channel",
        "payload_get_inner",
        "payload_type",
        "__dict__",
    )

    def __init__(
        self,
        name: str,
        key: str | None = None,
        payload_get: list | dict | None = None,
        *,
        has_get: bool | None = None,
        has_push: bool | None = None,
    ) -> None:
        self.name = name
        if key:
            self.key = key
        self.payload_get_inner = payload_get
        self.payload_type = type(payload_get)
        self.need_channel = bool(payload_get)
        self.has_get = has_get
        self.has_push = has_push
        NAMESPACES[name] = self

    @cached_property
    def key(self) -> str:
        key = self.name.split(".")[-1]
        # mainly camelCasing the last split of the namespace
        # with special care for also the last char which looks
        # lowercase when it's a X (i.e. ToggleX -> togglex)
        if key[-1] == "X":
            return "".join((key[0].lower(), key[1:-1], "x"))
        else:
            return "".join((key[0].lower(), key[1:]))

    @cached_property
    def is_hub(self):
        return re.match(r"Appliance\.Hub\.(.*)", self.name)

    @cached_property
    def is_thermostat(self):
        return re.match(r"Appliance\.Control\.Thermostat\.(.*)", self.name)

    @cached_property
    def payload_get(self):
        """
        Returns a default structured payload for method GET.
        When we query a device 'namespace' with a GET method the request payload
        is usually 'well structured' (more or less). We have a dictionary of
        well-known payloads else we'll use some heuristics
        """
        if self.payload_get_inner is None:
            match self.name.split("."):
                case (_, "Hub", *_):
                    self.payload_get_inner = []
                case (_, "RollerShutter", *_):
                    self.payload_get_inner = []
                case (_, _, "Thermostat", *_):
                    self.payload_get_inner = [{mc.KEY_CHANNEL: 0}]
                case _:
                    self.payload_get_inner = {}
            self.payload_type = type(self.payload_get_inner)
            self.payload_channel = bool(self.payload_get_inner)
        return {self.key: self.payload_get_inner}

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


def _namespace_push(
    name: str,
    key: str | None = None,
):
    # these namespaces do not provide the GET/GETACK methods
    # hence querying works by issuing an empty PUSH. SET/SETACK might work though
    return Namespace(name, key, None, has_get=False, has_push=True)


def _namespace_get(
    name: str,
    key: str | None = None,
    payload_get: list | dict | None = None,
):
    return Namespace(name, key, payload_get, has_get=True, has_push=False)


def _namespace_get_push(
    name: str,
    key: str | None = None,
    payload_get: list | dict | None = None,
):
    return Namespace(name, key, payload_get, has_get=True, has_push=True)


# We predefine grammar for some widely used and well known namespaces either to skip 'euristics'
# and time consuming evaluation.
# Moreover, for some namespaces, the euristics about 'namespace key' and payload structure are not
# good so we must fix those beforehand.
Appliance_System_Ability = _namespace_get(
    mc.NS_APPLIANCE_SYSTEM_ABILITY, mc.KEY_ABILITY, {}
)
Appliance_System_All = _namespace_get(mc.NS_APPLIANCE_SYSTEM_ALL, mc.KEY_ALL, {})
Appliance_System_Clock = _namespace_push(mc.NS_APPLIANCE_SYSTEM_CLOCK, mc.KEY_CLOCK)
Appliance_System_Debug = _namespace_get(mc.NS_APPLIANCE_SYSTEM_DEBUG, mc.KEY_DEBUG, {})
Appliance_System_DNDMode = _namespace_get(
    mc.NS_APPLIANCE_SYSTEM_DNDMODE, mc.KEY_DNDMODE, {}
)

Appliance_Control_ConsumptionX = _namespace_get_push(
    mc.NS_APPLIANCE_CONTROL_CONSUMPTIONX, mc.KEY_CONSUMPTIONX, []
)
Appliance_Control_Diffuser_Sensor = _namespace_get_push(
    mc.NS_APPLIANCE_CONTROL_DIFFUSER_SENSOR, mc.KEY_SENSOR, {}
)
Appliance_Control_Electricity = _namespace_get_push(
    mc.NS_APPLIANCE_CONTROL_ELECTRICITY, mc.KEY_ELECTRICITY, {}
)
Appliance_Control_FilterMaintenance = _namespace_push(
    mc.NS_APPLIANCE_CONTROL_FILTERMAINTENANCE, mc.KEY_FILTER
)
Appliance_Control_Light = _namespace_get_push(mc.NS_APPLIANCE_CONTROL_LIGHT)
Appliance_Control_Light_Effect = _namespace_get(
    mc.NS_APPLIANCE_CONTROL_LIGHT_EFFECT, mc.KEY_EFFECT, []
)
Appliance_Control_Mp3 = _namespace_get_push(mc.NS_APPLIANCE_CONTROL_MP3, mc.KEY_MP3, {})
Appliance_Control_PhysicalLock = _namespace_push(
    mc.NS_APPLIANCE_CONTROL_PHYSICALLOCK, mc.KEY_LOCK
)
Appliance_Control_Screen_Brightness = _namespace_get(
    mc.NS_APPLIANCE_CONTROL_SCREEN_BRIGHTNESS, mc.KEY_BRIGHTNESS, [{mc.KEY_CHANNEL: 0}]
)
Appliance_Control_Sensor_History = _namespace_get_push(
    mc.NS_APPLIANCE_CONTROL_SENSOR_HISTORY, mc.KEY_HISTORY, [{mc.KEY_CHANNEL: 0}]
)
Appliance_Control_Spray = _namespace_get_push(
    mc.NS_APPLIANCE_CONTROL_SPRAY, mc.KEY_SPRAY, {}
)
Appliance_Control_TempUnit = _namespace_get_push(
    mc.NS_APPLIANCE_CONTROL_TEMPUNIT, mc.KEY_TEMPUNIT, [{mc.KEY_CHANNEL: 0}]
)
Appliance_Control_Thermostat_CompressorDelay = _namespace_get(
    mc.NS_APPLIANCE_CONTROL_THERMOSTAT_COMPRESSORDELAY,
    mc.KEY_DELAY,
    [{mc.KEY_CHANNEL: 0}],
)
Appliance_Control_TimerX = _namespace_get(
    mc.NS_APPLIANCE_CONTROL_TIMERX, mc.KEY_TIMERX, {}
)
Appliance_Control_Toggle = _namespace_get_push(
    mc.NS_APPLIANCE_CONTROL_TOGGLE, mc.KEY_TOGGLE, {}
)
Appliance_Control_ToggleX = _namespace_get_push(
    mc.NS_APPLIANCE_CONTROL_TOGGLEX, mc.KEY_TOGGLEX, []
)
Appliance_Control_TriggerX = _namespace_get(
    mc.NS_APPLIANCE_CONTROL_TRIGGERX, mc.KEY_TRIGGERX, {}
)
Appliance_Control_Unbind = _namespace_push(mc.NS_APPLIANCE_CONTROL_UNBIND)

Appliance_Digest_TimerX = _namespace_get(
    mc.NS_APPLIANCE_DIGEST_TIMERX, mc.KEY_DIGEST, []
)
Appliance_Digest_TriggerX = _namespace_get(
    mc.NS_APPLIANCE_DIGEST_TRIGGERX, mc.KEY_DIGEST, []
)

Appliance_GarageDoor_Config = _namespace_get(
    mc.NS_APPLIANCE_GARAGEDOOR_CONFIG, mc.KEY_CONFIG, {}
)
Appliance_GarageDoor_MultipleConfig = _namespace_get(
    mc.NS_APPLIANCE_GARAGEDOOR_MULTIPLECONFIG, mc.KEY_CONFIG, [{mc.KEY_CHANNEL: 0}]
)
Appliance_GarageDoor_State = _namespace_get_push(mc.NS_APPLIANCE_GARAGEDOOR_STATE)
# Appliance.Hub. namespace typically handled with euristics except these
Appliance_Hub_Mts100_ScheduleB = _namespace_get_push(
    mc.NS_APPLIANCE_HUB_MTS100_SCHEDULEB, mc.KEY_SCHEDULE, []
)
Appliance_Hub_Sensor_Smoke = _namespace_get_push(
    mc.NS_APPLIANCE_HUB_SENSOR_SMOKE, mc.KEY_SMOKEALARM, []
)
Appliance_Hub_SubDevice_MotorAdjust = _namespace_get_push(
    mc.NS_APPLIANCE_HUB_SUBDEVICE_MOTORADJUST, mc.KEY_ADJUST, []
)

Appliance_RollerShutter_Adjust = _namespace_push(mc.NS_APPLIANCE_ROLLERSHUTTER_ADJUST)
