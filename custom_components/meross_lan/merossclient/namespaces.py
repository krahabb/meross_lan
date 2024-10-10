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
            return Namespace(namespace, experimental=True)


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

    name: str
    """The namespace name"""
    key: str
    """The root key of the payload"""
    key_channel: str
    """The key used to index items in list payloads"""
    has_get: bool | None
    """ns supports method GET - is None when we have no clue"""
    has_push: bool | None
    """ns supports method PUSH - is None when we have no clue"""
    need_channel: bool
    """ns needs the channel index in standard GET queries"""
    payload_get_inner: list | dict | None
    """when set it depicts the structure of the inner payload in GET queries"""
    experimental: bool
    """True if the namespace definition/behavior is somewhat unknown"""

    __slots__ = (
        "name",
        "key",
        "key_channel",
        "has_get",
        "has_push",
        "need_channel",
        "payload_get_inner",
        "payload_type",
        "experimental",
        "__dict__",
    )

    def __init__(
        self,
        name: str,
        key: str | None = None,
        payload_get: list | dict | None = None,
        *,
        key_channel: str | None = None,
        has_get: bool | None = None,
        has_push: bool | None = None,
        experimental: bool = False,
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
                case (
                    (_, "Control", "Thermostat", *_)
                    | (_, "Control", "Screen", *_)
                    | (_, "Control", "Sensor", *_)
                ):
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

        self.key_channel = key_channel or (mc.KEY_ID if self.is_hub else mc.KEY_CHANNEL)
        self.has_get = has_get
        self.has_push = has_push
        self.experimental = experimental
        NAMESPACES[name] = self

    @cached_property
    def is_sensor(self):
        """Namespace payload indexed on hub/subdevice by key 'subId' or
        by 'channel' for regular devices."""
        return re.match(r"Appliance\.Control\.Sensor\.(.*)", self.name)

    @cached_property
    def is_hub(self):
        """Namespace payload indexed on subdevice by key 'id'."""
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


def ns_build_from_message(namespace: str, method: str, payload: dict):
    ns_key = None
    key_channel = None
    payload_get = None
    if payload:
        ns_payload = None
        # we hope the first key in the payload is the 'namespace key'
        for ns_key, ns_payload in payload.items():
            break

        if type(ns_payload) is list:
            payload_get = _LIST
            if ns_payload:
                ns_payload = ns_payload[0]
                for key_channel in (mc.KEY_SUBID, mc.KEY_ID, mc.KEY_CHANNEL):
                    if key_channel in ns_payload:
                        payload_get = _LIST_C
                        break
                else:
                    # let the Namespace ctor euristics
                    key_channel = None
        elif type(ns_payload) is dict:
            payload_get = _DICT

    return Namespace(
        namespace,
        ns_key,
        payload_get,
        key_channel=key_channel,
        has_push=True if method == mc.METHOD_PUSH else None,
        experimental=True,
    )


def _ns_unknown(
    name: str,
    key: str | None = None,
):
    """Builds a definition for a namespace without specific knowledge of supported methods"""
    return Namespace(name, key, None, experimental=True)


def _ns_push(
    name: str,
    key: str | None = None,
):
    """Builds a definition for a namespace supporting only PUSH queries (no GET)"""
    return Namespace(name, key, None, has_get=False, has_push=True)


def _ns_get(
    name: str,
    key: str | None = None,
    payload_get: list | dict | None = None,
):
    """Builds a definition for a namespace supporting only GET queries (no PUSH)"""
    return Namespace(name, key, payload_get, has_get=True, has_push=False)


def _ns_get_push(
    name: str,
    key: str | None = None,
    payload_get: list | dict | None = None,
):
    """Builds a definition for a namespace supporting GET queries (which also PUSHes updates)"""
    return Namespace(name, key, payload_get, has_get=True, has_push=True)


def _ns_set(
    name: str,
    key: str | None = None,
    payload_get: list | dict | None = None,
):
    """Builds a definition for a namespace supporting only SET"""
    return Namespace(name, key, payload_get, has_get=False, has_push=False)


def _ns_no_query(
    name: str,
    key: str | None = None,
):
    """Builds a definition for a namespace not supporting GET,PUSH"""
    return Namespace(name, key, None, has_get=False, has_push=False)


# We predefine grammar for some widely used and well known namespaces either to skip 'euristics'
# and time consuming evaluation.
# Moreover, for some namespaces, the euristics about 'namespace key' and payload structure are not
# good so we must fix those beforehand.
Appliance_System_Ability = _ns_get("Appliance.System.Ability", mc.KEY_ABILITY, _DICT)
Appliance_System_All = _ns_get("Appliance.System.All", mc.KEY_ALL, _DICT)
Appliance_System_Clock = _ns_push("Appliance.System.Clock", mc.KEY_CLOCK)
Appliance_System_Debug = _ns_get("Appliance.System.Debug", mc.KEY_DEBUG, _DICT)
Appliance_System_DNDMode = _ns_get("Appliance.System.DNDMode", mc.KEY_DNDMODE, _DICT)
Appliance_System_Firmware = _ns_get("Appliance.System.Firmware", mc.KEY_FIRMWARE, _DICT)
Appliance_System_Hardware = _ns_get("Appliance.System.Hardware", mc.KEY_HARDWARE, _DICT)
Appliance_System_Online = _ns_get_push("Appliance.System.Online", mc.KEY_ONLINE, _DICT)
Appliance_System_Report = _ns_push("Appliance.System.Report", mc.KEY_REPORT)
Appliance_System_Runtime = _ns_get("Appliance.System.Runtime", mc.KEY_RUNTIME, _DICT)
Appliance_System_Time = _ns_get_push("Appliance.System.Time", mc.KEY_TIME, _DICT)
Appliance_System_Position = _ns_get("Appliance.System.Position", mc.KEY_POSITION, _DICT)

Appliance_Config_Key = _ns_set("Appliance.Config.Key", mc.KEY_KEY, _DICT)
Appliance_Config_OverTemp = _ns_get("Appliance.Config.OverTemp", mc.KEY_OVERTEMP, _DICT)
Appliance_Config_Trace = _ns_get("Appliance.Config.Trace")
Appliance_Config_Wifi = _ns_get("Appliance.Config.Wifi")
Appliance_Config_WifiList = _ns_get("Appliance.Config.WifiList")
Appliance_Config_WifiX = _ns_get("Appliance.Config.WifiX")


Appliance_Control_Bind = _ns_get("Appliance.Control.Bind", mc.KEY_BIND, _DICT)
Appliance_Control_ConsumptionConfig = _ns_get(
    "Appliance.Control.ConsumptionConfig", mc.KEY_CONFIG, _DICT
)
Appliance_Control_ConsumptionH = _ns_get(
    "Appliance.Control.ConsumptionH", mc.KEY_CONSUMPTIONH, _LIST_C
)
Appliance_Control_ConsumptionX = _ns_get_push(
    "Appliance.Control.ConsumptionX", mc.KEY_CONSUMPTIONX, _LIST
)
Appliance_Control_Diffuser_Light = _ns_get_push(
    "Appliance.Control.Diffuser.Light", mc.KEY_LIGHT, _DICT
)
Appliance_Control_Diffuser_Sensor = _ns_get_push(
    "Appliance.Control.Diffuser.Sensor", mc.KEY_SENSOR, _DICT
)
Appliance_Control_Diffuser_Spray = _ns_get_push(
    "Appliance.Control.Diffuser.Spray", mc.KEY_SPRAY, _DICT
)
Appliance_Control_Electricity = _ns_get_push(
    "Appliance.Control.Electricity", mc.KEY_ELECTRICITY, _DICT
)
Appliance_Control_ElectricityX = _ns_get_push(
    "Appliance.Control.ElectricityX", mc.KEY_ELECTRICITY, _DICT
)  # this is actually confirmed over Refoss EM06
Appliance_Control_Fan = _ns_get("Appliance.Control.Fan", mc.KEY_FAN)
Appliance_Control_FilterMaintenance = _ns_push(
    "Appliance.Control.FilterMaintenance", mc.KEY_FILTER
)
Appliance_Control_Light = _ns_get_push("Appliance.Control.Light")
Appliance_Control_Light_Effect = _ns_get(
    "Appliance.Control.Light.Effect", mc.KEY_EFFECT, _LIST
)
Appliance_Control_Mp3 = _ns_get_push("Appliance.Control.Mp3", mc.KEY_MP3, _DICT)
Appliance_Control_Multiple = _ns_get(
    "Appliance.Control.Multiple", mc.KEY_MULTIPLE, _LIST
)
Appliance_Control_OverTemp = _ns_get(
    "Appliance.Control.OverTemp", mc.KEY_OVERTEMP, _LIST
)
Appliance_Control_PhysicalLock = _ns_push("Appliance.Control.PhysicalLock", mc.KEY_LOCK)
Appliance_Control_Presence_Config = _ns_get(
    "Appliance.Control.Presence.Config", mc.KEY_CONFIG, _LIST_C
)
Appliance_Control_Presence_Study = _ns_push(
    "Appliance.Control.Presence.Study", mc.KEY_CONFIG
)
Appliance_Control_Spray = _ns_get_push("Appliance.Control.Spray", mc.KEY_SPRAY, _DICT)
Appliance_Control_TempUnit = _ns_get_push(
    "Appliance.Control.TempUnit", mc.KEY_TEMPUNIT, _LIST_C
)
Appliance_Control_TimerX = _ns_get("Appliance.Control.TimerX", mc.KEY_TIMERX, _DICT)
Appliance_Control_Toggle = _ns_get_push(
    "Appliance.Control.Toggle", mc.KEY_TOGGLE, _DICT
)
Appliance_Control_ToggleX = _ns_get_push(
    "Appliance.Control.ToggleX", mc.KEY_TOGGLEX, _LIST
)
Appliance_Control_Trigger = _ns_get("Appliance.Control.Trigger", mc.KEY_TRIGGER, _DICT)
Appliance_Control_TriggerX = _ns_get(
    "Appliance.Control.TriggerX", mc.KEY_TRIGGERX, _DICT
)
Appliance_Control_Unbind = _ns_push("Appliance.Control.Unbind")
Appliance_Control_Upgrade = _ns_get("Appliance.Control.Upgrade")

Appliance_Control_Sensor_Latest = _ns_get_push(
    "Appliance.Control.Sensor.Latest", mc.KEY_LATEST, _LIST_C
)  # carrying miscellaneous sensor values (temp/humi)
Appliance_Control_Sensor_History = _ns_get_push(
    "Appliance.Control.Sensor.History", mc.KEY_HISTORY, _LIST_C
)  # history of sensor values
Appliance_Control_Sensor_LatestX = _ns_get_push(
    "Appliance.Control.Sensor.LatestX", mc.KEY_LATEST, _LIST_C
)  # Appearing on both regular devices (ms600) and hub/subdevices (ms130)
Appliance_Control_Sensor_LatestX.experimental = True
Appliance_Control_Sensor_HistoryX = _ns_get_push(
    "Appliance.Control.Sensor.HistoryX", mc.KEY_HISTORY, _LIST_C
)  # history of sensor values
Appliance_Control_Sensor_HistoryX.experimental = True
# MTS200-960 smart thermostat
Appliance_Control_Screen_Brightness = _ns_get_push(
    "Appliance.Control.Screen.Brightness"
)
Appliance_Control_Thermostat_Alarm = _ns_get_push("Appliance.Control.Thermostat.Alarm")
Appliance_Control_Thermostat_AlarmConfig = _ns_get(
    "Appliance.Control.Thermostat.AlarmConfig"
)
Appliance_Control_Thermostat_Calibration = _ns_get(
    "Appliance.Control.Thermostat.Calibration"
)
Appliance_Control_Thermostat_CompressorDelay = _ns_get(
    "Appliance.Control.Thermostat.CompressorDelay", mc.KEY_DELAY, _LIST_C
)
Appliance_Control_Thermostat_CtlRange = _ns_get("Appliance.Control.Thermostat.CtlRange")
Appliance_Control_Thermostat_DeadZone = _ns_get("Appliance.Control.Thermostat.DeadZone")
Appliance_Control_Thermostat_Frost = _ns_get("Appliance.Control.Thermostat.Frost")
Appliance_Control_Thermostat_HoldAction = _ns_get_push(
    "Appliance.Control.Thermostat.HoldAction"
)
Appliance_Control_Thermostat_Mode = _ns_get_push("Appliance.Control.Thermostat.Mode")
Appliance_Control_Thermostat_ModeB = _ns_get_push("Appliance.Control.Thermostat.ModeB")
Appliance_Control_Thermostat_Overheat = _ns_get_push(
    "Appliance.Control.Thermostat.Overheat"
)
Appliance_Control_Thermostat_Schedule = _ns_get_push(
    "Appliance.Control.Thermostat.Schedule"
)
Appliance_Control_Thermostat_ScheduleB = _ns_get_push(
    "Appliance.Control.Thermostat.ScheduleB"
)
Appliance_Control_Thermostat_Sensor = _ns_get_push(
    "Appliance.Control.Thermostat.Sensor"
)
Appliance_Control_Thermostat_SummerMode = _ns_get_push(
    "Appliance.Control.Thermostat.SummerMode"
)
Appliance_Control_Thermostat_Timer = _ns_get_push("Appliance.Control.Thermostat.Timer")
Appliance_Control_Thermostat_WindowOpened = _ns_get_push(
    "Appliance.Control.Thermostat.WindowOpened"
)

Appliance_Digest_TimerX = _ns_get("Appliance.Digest.TimerX", mc.KEY_DIGEST, _LIST)
Appliance_Digest_TriggerX = _ns_get("Appliance.Digest.TriggerX", mc.KEY_DIGEST, _LIST)

Appliance_Encrypt_Suite = _ns_get("Appliance.Encrypt.Suite")
Appliance_Encrypt_ECDHE = _ns_no_query("Appliance.Encrypt.ECDHE")

Appliance_GarageDoor_Config = _ns_get(
    "Appliance.GarageDoor.Config", mc.KEY_CONFIG, _DICT
)
Appliance_GarageDoor_MultipleConfig = _ns_get(
    "Appliance.GarageDoor.MultipleConfig", mc.KEY_CONFIG, _LIST_C
)
Appliance_GarageDoor_State = _ns_get_push("Appliance.GarageDoor.State")

Appliance_Digest_Hub = _ns_get("Appliance.Digest.Hub", mc.KEY_HUB, _LIST)

Appliance_Hub_Battery = _ns_get_push("Appliance.Hub.Battery", mc.KEY_BATTERY, _LIST)
Appliance_Hub_Exception = _ns_get_push(
    "Appliance.Hub.Exception", mc.KEY_EXCEPTION, _LIST
)
Appliance_Hub_Online = _ns_get_push("Appliance.Hub.Online", mc.KEY_ONLINE, _LIST)
Appliance_Hub_PairSubDev = _ns_get_push("Appliance.Hub.PairSubDev")
Appliance_Hub_Report = _ns_get_push("Appliance.Hub.Report")
Appliance_Hub_Sensitivity = _ns_get_push("Appliance.Hub.Sensitivity")
Appliance_Hub_SubdeviceList = _ns_get_push("Appliance.Hub.SubdeviceList")
Appliance_Hub_ToggleX = _ns_get_push("Appliance.Hub.ToggleX", mc.KEY_TOGGLEX, _LIST)

Appliance_Hub_Mts100_Adjust = _ns_get(
    "Appliance.Hub.Mts100.Adjust", mc.KEY_ADJUST, _LIST
)
Appliance_Hub_Mts100_All = _ns_get("Appliance.Hub.Mts100.All", mc.KEY_ALL, _LIST)
Appliance_Hub_Mts100_Mode = _ns_get_push(
    "Appliance.Hub.Mts100.Mode", mc.KEY_MODE, _LIST
)
Appliance_Hub_Mts100_Schedule = _ns_get_push(
    "Appliance.Hub.Mts100.Schedule", mc.KEY_SCHEDULE, _LIST
)
Appliance_Hub_Mts100_ScheduleB = _ns_get_push(
    "Appliance.Hub.Mts100.ScheduleB", mc.KEY_SCHEDULE, _LIST
)
Appliance_Hub_Mts100_Temperature = _ns_get_push(
    "Appliance.Hub.Mts100.Temperature", mc.KEY_TEMPERATURE, _LIST
)
Appliance_Hub_Mts100_TimeSync = _ns_get_push("Appliance.Hub.Mts100.TimeSync")
Appliance_Hub_Mts100_SuperCtl = _ns_get_push("Appliance.Hub.Mts100.SuperCtl")

Appliance_Hub_Sensor_Adjust = _ns_get(
    "Appliance.Hub.Sensor.Adjust", mc.KEY_ADJUST, _LIST
)
Appliance_Hub_Sensor_Alert = _ns_get_push("Appliance.Hub.Sensor.Alert")
Appliance_Hub_Sensor_All = _ns_get("Appliance.Hub.Sensor.All", mc.KEY_ALL, _LIST)
Appliance_Hub_Sensor_DoorWindow = _ns_get_push(
    "Appliance.Hub.Sensor.DoorWindow", mc.KEY_DOORWINDOW, _LIST
)
Appliance_Hub_Sensor_Latest = _ns_get_push(
    "Appliance.Hub.Sensor.Latest", mc.KEY_LATEST, _LIST
)
Appliance_Hub_Sensor_Motion = _ns_get_push("Appliance.Hub.Sensor.Motion")
Appliance_Hub_Sensor_Smoke = _ns_get_push(
    "Appliance.Hub.Sensor.Smoke", mc.KEY_SMOKEALARM, _LIST
)
Appliance_Hub_Sensor_TempHum = _ns_get_push("Appliance.Hub.Sensor.TempHum")
Appliance_Hub_Sensor_WaterLeak = _ns_get_push("Appliance.Hub.Sensor.WaterLeak")

Appliance_Hub_SubDevice_Beep = _ns_get_push("Appliance.Hub.SubDevice.Beep")
Appliance_Hub_SubDevice_MotorAdjust = _ns_get_push(
    "Appliance.Hub.SubDevice.MotorAdjust", mc.KEY_ADJUST, _LIST
)
Appliance_Hub_SubDevice_Version = _ns_get_push(
    "Appliance.Hub.SubDevice.Version", mc.KEY_VERSION, _LIST
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
