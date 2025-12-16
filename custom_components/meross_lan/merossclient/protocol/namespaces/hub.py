"""
Descriptors for hub specific namespaces management.
This file contains the knowledge about how namespaces work (their syntax and behaviors).
Namespaces specific for Hubs are stored in a dedicated map (HUB_NAMESPACES) so that they can also override
namespaces already defined in the default (NAMESPACES) map.
When code lookups HUB_NAMESPACES it will fallback to NAMESPACES if no match so that
standard namespaces are available for Hubs but preserving their default behavior can be easily accessed through
only HUB_NAMESPACES.
We actually define the symbol HUB_NAMESPACES in the root package since it also uses that for heuristics
but from a design perspective it should be born here.
"""

from . import HUB_NAMESPACES
from .. import const as mc, namespaces as mn

MAP_HUB: "mn.ns.Args" = {"map": HUB_NAMESPACES}
IS_HUB_ID: "mn.ns.Args" = {
    "is_hub_id": True,
    "payload": mn.PayloadType.LIST,
}  # we can override payload in definitions
IS_HUB_SUBID: "mn.ns.Args" = {
    "is_hub_subid": True,
    "payload": mn.PayloadType.LIST_C,
}  # we can override payload in definitions

Appliance_Config_DeviceCfg = mn.ns(
    "Appliance.Config.DeviceCfg", mc.KEY_CONFIG, mn.ARGS_GETSETPUSH | IS_HUB_SUBID
)  # ms130-mst100
Appliance_Config_Sensor_Association = mn.ns(
    "Appliance.Config.Sensor.Association",
    mc.KEY_CONFIG,
    mn.ARGS_GETSETPUSH | mn.IS_SENSOR | IS_HUB_SUBID,
)  # Not seen really..just an extrapolation for Hub(s)
Appliance_Config_WaterPlan = mn.ns(
    "Appliance.Config.WaterPlan",
    mc.KEY_CONFIG,
    mn.ARGS_GETSET | IS_HUB_SUBID | mn.G_EXPERIMENTAL,
)  # mst100 (used to read/write watering schedules)
Appliance_Control_Sensor_HistoryX = mn.ns(
    "Appliance.Control.Sensor.HistoryX",
    mc.KEY_HISTORY,
    mn.ARGS_GET | mn.IS_SENSOR | IS_HUB_SUBID,
)
Appliance_Control_Sensor_LatestX = mn.ns(
    "Appliance.Control.Sensor.LatestX",
    mc.KEY_LATEST,
    mn.ARGS_GETPUSH | mn.IS_SENSOR | IS_HUB_SUBID,
)
Appliance_Control_Water = mn.ns(
    "Appliance.Control.Water",
    mc.KEY_CONTROL,
    mn.ARGS_GETSETPUSH | IS_HUB_SUBID,
)  # mst100
Appliance_Control_WaterEvent = mn.ns(
    "Appliance.Control.WaterEvent",
    mc.KEY_CONTROL,
    mn.ARGS_NO_Q | IS_HUB_SUBID,
)  # mst100 (used to report events after each watering cycle is completed)
Appliance_Control_WaterEvent_Skip = mn.ns(
    "Appliance.Control.WaterEvent.Skip",
    mc.KEY_CONTROL,
    mn.ARGS_GETSET | IS_HUB_SUBID | mn.G_EXPERIMENTAL,
)  # mst100 (used in the app to skip watering on specific days according to the schedule)
Appliance_Control_WaterPlan_Skip = mn.ns(
    "Appliance.Control.WaterPlan.Skip",
    mc.KEY_CONTROL,
    mn.ARGS_NO_Q | IS_HUB_SUBID,
)  # mst100 (allows the device to query cloud server about whether to skip execution on a specific day based on weather conditions)

Appliance_Digest_Hub = mn.ns(
    "Appliance.Digest.Hub", mc.KEY_HUB, mn.ARGS_GET | mn.P_LIST | MAP_HUB
)
Appliance_Digest_WaterPlan = mn.ns(
    "Appliance.Digest.WaterPlan",
    mc.KEY_DIGEST,
    mn.ARGS_GETSET | IS_HUB_SUBID | mn.G_EXPERIMENTAL,
)  # mst100 (used to read/write watering schedules)
Appliance_Hub_Battery = mn.ns(
    "Appliance.Hub.Battery", mc.KEY_BATTERY, mn.ARGS_GETPUSH | IS_HUB_ID
)
Appliance_Hub_Exception = mn.ns(
    "Appliance.Hub.Exception", mc.KEY_EXCEPTION, mn.ARGS_PUSH | IS_HUB_ID | mn.P_LIST_C
)
Appliance_Hub_ExtraInfo = mn.ns(
    "Appliance.Hub.ExtraInfo", "extraInfo", mn.ARGS_GET | MAP_HUB
)
Appliance_Hub_Online = mn.ns(
    "Appliance.Hub.Online", mc.KEY_ONLINE, mn.ARGS_GETPUSH | IS_HUB_ID
)
Appliance_Hub_PairSubDev = mn.ns("Appliance.Hub.PairSubDev", None, mn.ARGS_NO_Q)
Appliance_Hub_Report = mn.ns(
    "Appliance.Hub.Report", None, mn.ARGS_GETPUSH | IS_HUB_ID | mn.G_EXPERIMENTAL
)
Appliance_Hub_Sensitivity = mn.ns(
    "Appliance.Hub.Sensitivity", None, mn.ARGS_GETPUSH | IS_HUB_ID
)
Appliance_Hub_SubdeviceList = mn.ns(
    "Appliance.Hub.SubdeviceList", None, mn.ARGS_GETPUSH | MAP_HUB
)
Appliance_Hub_ToggleX = mn.ns(
    "Appliance.Hub.ToggleX", mc.KEY_TOGGLEX, mn.ARGS_GETSETPUSH | IS_HUB_ID
)
Appliance_Hub_Mts100_Adjust = mn.ns(
    "Appliance.Hub.Mts100.Adjust", mc.KEY_ADJUST, mn.ARGS_GETSET | IS_HUB_ID
)
Appliance_Hub_Mts100_All = mn.ns(
    "Appliance.Hub.Mts100.All", mc.KEY_ALL, mn.ARGS_GET | IS_HUB_ID
)
Appliance_Hub_Mts100_Config = mn.ns(
    "Appliance.Hub.Mts100.Config",
    mc.KEY_CONFIG,
    mn.ARGS_GETSET | IS_HUB_ID,
)
Appliance_Hub_Mts100_Mode = mn.ns(
    "Appliance.Hub.Mts100.Mode", mc.KEY_MODE, mn.ARGS_GETSETPUSH | IS_HUB_ID
)
Appliance_Hub_Mts100_Schedule = mn.ns(
    "Appliance.Hub.Mts100.Schedule", mc.KEY_SCHEDULE, mn.ARGS_GETSETPUSH | IS_HUB_ID
)
Appliance_Hub_Mts100_ScheduleB = mn.ns(
    "Appliance.Hub.Mts100.ScheduleB", mc.KEY_SCHEDULE, mn.ARGS_GETSETPUSH | IS_HUB_ID
)
Appliance_Hub_Mts100_Temperature = mn.ns(
    "Appliance.Hub.Mts100.Temperature",
    mc.KEY_TEMPERATURE,
    mn.ARGS_GETSETPUSH | IS_HUB_ID,
)
Appliance_Hub_Mts100_TimeSync = mn.ns(
    "Appliance.Hub.Mts100.TimeSync", None, mn.ARGS_GETPUSH | IS_HUB_ID
)
Appliance_Hub_Mts100_SuperCtl = mn.ns(
    "Appliance.Hub.Mts100.SuperCtl", None, mn.ARGS_GETPUSH | IS_HUB_ID
)
Appliance_Hub_Sensor_Adjust = mn.ns(
    "Appliance.Hub.Sensor.Adjust", mc.KEY_ADJUST, mn.ARGS_GETSET | IS_HUB_ID
)
Appliance_Hub_Sensor_Alert = mn.ns(
    "Appliance.Hub.Sensor.Alert", None, mn.ARGS_GETPUSH | IS_HUB_ID
)
Appliance_Hub_Sensor_All = mn.ns(
    "Appliance.Hub.Sensor.All", mc.KEY_ALL, mn.ARGS_GET | IS_HUB_ID
)
Appliance_Hub_Sensor_DoorWindow = mn.ns(
    "Appliance.Hub.Sensor.DoorWindow", mc.KEY_DOORWINDOW, mn.ARGS_GETPUSH | IS_HUB_ID
)
Appliance_Hub_Sensor_Latest = mn.ns(
    "Appliance.Hub.Sensor.Latest", mc.KEY_LATEST, mn.ARGS_GETPUSH | IS_HUB_ID
)
Appliance_Hub_Sensor_Motion = mn.ns(
    "Appliance.Hub.Sensor.Motion", None, mn.ARGS_GETPUSH | IS_HUB_ID
)
Appliance_Hub_Sensor_Smoke = mn.ns(
    "Appliance.Hub.Sensor.Smoke", mc.KEY_SMOKEALARM, mn.ARGS_GETPUSH | IS_HUB_ID
)
Appliance_Hub_Sensor_TempHum = mn.ns(
    "Appliance.Hub.Sensor.TempHum", mc.KEY_TEMPHUM, mn.ARGS_GETPUSH | IS_HUB_ID
)
Appliance_Hub_Sensor_WaterLeak = mn.ns(
    "Appliance.Hub.Sensor.WaterLeak", mc.KEY_WATERLEAK, mn.ARGS_GETPUSH | IS_HUB_ID
)
Appliance_Hub_SubDevice_Beep = mn.ns(
    "Appliance.Hub.SubDevice.Beep",
    mc.KEY_ALARM,
    mn.ARGS_GETSET | IS_HUB_ID | mn.G_EXPERIMENTAL,  # no clue yet
)
Appliance_Hub_SubDevice_MotorAdjust = mn.ns(
    "Appliance.Hub.SubDevice.MotorAdjust",
    mc.KEY_ADJUST,
    mn.ARGS_SET | IS_HUB_ID | mn.G_EXPERIMENTAL,  # no clue yet
)
Appliance_Hub_SubDevice_Version = mn.ns(
    "Appliance.Hub.SubDevice.Version", mc.KEY_VERSION, mn.ARGS_GETPUSH | IS_HUB_ID
)
