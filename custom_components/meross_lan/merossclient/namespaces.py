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

# singletons for default payloads (TODO:should be immutable though)
_DICT: typing.Final = {}
_LIST: typing.Final = []
_LIST_C: typing.Final = [{mc.KEY_CHANNEL: 0}]


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
        "key",
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
        else:
            key = name.split(".")[-1]
            # mainly camelCasing the last split of the namespace
            # with special care for also the last char which looks
            # lowercase when it's a X (i.e. ToggleX -> togglex)
            if key[-1] == "X":
                self.key = "".join((key[0].lower(), key[1:-1], "x"))
            else:
                self.key = "".join((key[0].lower(), key[1:]))

        if payload_get is None:
            match name.split("."):
                case (_, "Hub", *_):
                    self.payload_get_inner = _LIST
                    self.payload_type = list
                    self.need_channel = False
                case (_, "RollerShutter", *_):
                    self.payload_get_inner = _LIST
                    self.payload_type = list
                    self.need_channel = False
                case (_, _, "Thermostat", *_):
                    self.payload_get_inner = [{mc.KEY_CHANNEL: 0}]
                    self.payload_type = list
                    self.need_channel = True
                case _:
                    self.payload_get_inner = _DICT
                    self.payload_type = dict
                    self.need_channel = False
        else:
            self.payload_get_inner = payload_get
            self.payload_type = type(payload_get)
            self.need_channel = bool(payload_get)
        self.has_get = has_get
        self.has_push = has_push
        NAMESPACES[name] = self

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


def _ns_push(
    name: str,
    key: str | None = None,
):
    # these namespaces do not provide the GET/GETACK methods
    # hence querying works by issuing an empty PUSH. SET/SETACK might work though
    return Namespace(name, key, None, has_get=False, has_push=True)


def _ns_get(
    name: str,
    key: str | None = None,
    payload_get: list | dict | None = None,
):
    return Namespace(name, key, payload_get, has_get=True, has_push=False)


def _ns_get_push(
    name: str,
    key: str | None = None,
    payload_get: list | dict | None = None,
):
    return Namespace(name, key, payload_get, has_get=True, has_push=True)


# We predefine grammar for some widely used and well known namespaces either to skip 'euristics'
# and time consuming evaluation.
# Moreover, for some namespaces, the euristics about 'namespace key' and payload structure are not
# good so we must fix those beforehand.
Appliance_System_Ability = _ns_get(
    mc.NS_APPLIANCE_SYSTEM_ABILITY, mc.KEY_ABILITY, _DICT
)
Appliance_System_All = _ns_get(mc.NS_APPLIANCE_SYSTEM_ALL, mc.KEY_ALL, _DICT)
Appliance_System_Clock = _ns_push(mc.NS_APPLIANCE_SYSTEM_CLOCK, mc.KEY_CLOCK)
Appliance_System_Debug = _ns_get(mc.NS_APPLIANCE_SYSTEM_DEBUG, mc.KEY_DEBUG, _DICT)
Appliance_System_DNDMode = _ns_get(
    mc.NS_APPLIANCE_SYSTEM_DNDMODE, mc.KEY_DNDMODE, _DICT
)

Appliance_Control_ConsumptionX = _ns_get_push(
    mc.NS_APPLIANCE_CONTROL_CONSUMPTIONX, mc.KEY_CONSUMPTIONX, _LIST
)
Appliance_Control_Diffuser_Sensor = _ns_get_push(
    mc.NS_APPLIANCE_CONTROL_DIFFUSER_SENSOR, mc.KEY_SENSOR, _DICT
)
Appliance_Control_Electricity = _ns_get_push(
    mc.NS_APPLIANCE_CONTROL_ELECTRICITY, mc.KEY_ELECTRICITY, _DICT
)
Appliance_Control_FilterMaintenance = _ns_push(
    mc.NS_APPLIANCE_CONTROL_FILTERMAINTENANCE, mc.KEY_FILTER
)
Appliance_Control_Light = _ns_get_push(mc.NS_APPLIANCE_CONTROL_LIGHT)
Appliance_Control_Light_Effect = _ns_get(
    mc.NS_APPLIANCE_CONTROL_LIGHT_EFFECT, mc.KEY_EFFECT, _LIST
)
Appliance_Control_Mp3 = _ns_get_push(mc.NS_APPLIANCE_CONTROL_MP3, mc.KEY_MP3, _DICT)
Appliance_Control_PhysicalLock = _ns_push(
    mc.NS_APPLIANCE_CONTROL_PHYSICALLOCK, mc.KEY_LOCK
)
Appliance_Control_Screen_Brightness = _ns_get(
    mc.NS_APPLIANCE_CONTROL_SCREEN_BRIGHTNESS, mc.KEY_BRIGHTNESS, _LIST_C
)
Appliance_Control_Sensor_History = _ns_get_push(
    mc.NS_APPLIANCE_CONTROL_SENSOR_HISTORY, mc.KEY_HISTORY, _LIST_C
)
Appliance_Control_Spray = _ns_get_push(
    mc.NS_APPLIANCE_CONTROL_SPRAY, mc.KEY_SPRAY, _DICT
)
Appliance_Control_TempUnit = _ns_get_push(
    mc.NS_APPLIANCE_CONTROL_TEMPUNIT, mc.KEY_TEMPUNIT, _LIST_C
)
Appliance_Control_Thermostat_CompressorDelay = _ns_get(
    mc.NS_APPLIANCE_CONTROL_THERMOSTAT_COMPRESSORDELAY, mc.KEY_DELAY, _LIST_C
)
Appliance_Control_TimerX = _ns_get(mc.NS_APPLIANCE_CONTROL_TIMERX, mc.KEY_TIMERX, _DICT)
Appliance_Control_Toggle = _ns_get_push(
    mc.NS_APPLIANCE_CONTROL_TOGGLE, mc.KEY_TOGGLE, _DICT
)
Appliance_Control_ToggleX = _ns_get_push(
    mc.NS_APPLIANCE_CONTROL_TOGGLEX, mc.KEY_TOGGLEX, _LIST
)
Appliance_Control_TriggerX = _ns_get(
    mc.NS_APPLIANCE_CONTROL_TRIGGERX, mc.KEY_TRIGGERX, _DICT
)
Appliance_Control_Unbind = _ns_push(mc.NS_APPLIANCE_CONTROL_UNBIND)

Appliance_Digest_TimerX = _ns_get(mc.NS_APPLIANCE_DIGEST_TIMERX, mc.KEY_DIGEST, _LIST)
Appliance_Digest_TriggerX = _ns_get(
    mc.NS_APPLIANCE_DIGEST_TRIGGERX, mc.KEY_DIGEST, _LIST
)

Appliance_GarageDoor_Config = _ns_get(
    mc.NS_APPLIANCE_GARAGEDOOR_CONFIG, mc.KEY_CONFIG, _DICT
)
Appliance_GarageDoor_MultipleConfig = _ns_get(
    mc.NS_APPLIANCE_GARAGEDOOR_MULTIPLECONFIG, mc.KEY_CONFIG, _LIST_C
)
Appliance_GarageDoor_State = _ns_get_push(mc.NS_APPLIANCE_GARAGEDOOR_STATE)
# Appliance.Hub. namespace typically handled with euristics except these
Appliance_Hub_Mts100_ScheduleB = _ns_get_push(
    mc.NS_APPLIANCE_HUB_MTS100_SCHEDULEB, mc.KEY_SCHEDULE, _LIST
)
Appliance_Hub_Sensor_Smoke = _ns_get_push(
    mc.NS_APPLIANCE_HUB_SENSOR_SMOKE, mc.KEY_SMOKEALARM, _LIST
)
Appliance_Hub_SubDevice_MotorAdjust = _ns_get_push(
    mc.NS_APPLIANCE_HUB_SUBDEVICE_MOTORADJUST, mc.KEY_ADJUST, _LIST
)

Appliance_RollerShutter_Adjust = _ns_push(mc.NS_APPLIANCE_ROLLERSHUTTER_ADJUST)
