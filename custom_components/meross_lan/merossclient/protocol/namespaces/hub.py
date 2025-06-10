"""
Descriptors for hub specific namespaces management.
This file contains the knowledge about how namespaces work (their syntax and behaviors).
Namespaces specific for Hubs are stored in a dedicated map (HUB_NAMESPACES) so that they can also override
namespaces already defined in the default (NAMESPACES) map.
When code lookups HUB_NAMESPACES it will fallback to NAMESPACES if no match so that
standard namespaces are available for Hubs but preserving their default behavior can be easily accessed through
only HUB_NAMESPACES
"""

from typing import TYPE_CHECKING

from . import (
    ARGS_GET,
    ARGS_GETPUSH,
    ARGS_GETSET,
    ARGS_GETSETPUSH,
    HUB_NAMESPACES,
    IS_SENSOR,
    P_LIST,
    ns,
)
from .. import const as mc

MAP_HUB: "ns.Args" = {"map": HUB_NAMESPACES}
IS_HUB_ID: "ns.Args" = {"is_hub_id": True}
IS_HUB_SUBID: "ns.Args" = {"is_hub_subid": True}

Hub_Config_DeviceCfg = ns(
    "Appliance.Config.DeviceCfg", mc.KEY_CONFIG, ARGS_GETSETPUSH | IS_HUB_SUBID
)  # ms130
Hub_Config_Sensor_Association = ns(
    "Appliance.Config.Sensor.Association",
    mc.KEY_CONFIG,
    ARGS_GETSETPUSH | IS_SENSOR | MAP_HUB,
)  # Not seen really..just an extrapolation for Hub(s)
Hub_Control_Sensor_HistoryX = ns(
    "Appliance.Control.Sensor.HistoryX",
    mc.KEY_HISTORY,
    ARGS_GET | IS_SENSOR | MAP_HUB,
)
Hub_Control_Sensor_LatestX = ns(
    "Appliance.Control.Sensor.LatestX",
    mc.KEY_LATEST,
    ARGS_GETPUSH | IS_SENSOR | MAP_HUB,
)

Appliance_Digest_Hub = ns(
    "Appliance.Digest.Hub", mc.KEY_HUB, ARGS_GET | P_LIST | MAP_HUB
)
Appliance_Hub_Battery = ns(
    "Appliance.Hub.Battery", mc.KEY_BATTERY, ARGS_GETPUSH | IS_HUB_ID
)
Appliance_Hub_Exception = ns(
    "Appliance.Hub.Exception", mc.KEY_EXCEPTION, ARGS_GETPUSH | IS_HUB_ID
)
Appliance_Hub_Online = ns(
    "Appliance.Hub.Online", mc.KEY_ONLINE, ARGS_GETPUSH | IS_HUB_ID
)
Appliance_Hub_PairSubDev = ns(
    "Appliance.Hub.PairSubDev", None, ARGS_GETPUSH | IS_HUB_ID
)
Appliance_Hub_Report = ns("Appliance.Hub.Report", None, ARGS_GETPUSH | IS_HUB_ID)
Appliance_Hub_Sensitivity = ns(
    "Appliance.Hub.Sensitivity", None, ARGS_GETPUSH | IS_HUB_ID
)
Appliance_Hub_SubdeviceList = ns(
    "Appliance.Hub.SubdeviceList", None, ARGS_GETPUSH | IS_HUB_ID
)
Appliance_Hub_ToggleX = ns(
    "Appliance.Hub.ToggleX", mc.KEY_TOGGLEX, ARGS_GETSETPUSH | IS_HUB_ID
)
Appliance_Hub_Mts100_Adjust = ns(
    "Appliance.Hub.Mts100.Adjust", mc.KEY_ADJUST, ARGS_GETSET | IS_HUB_ID
)
Appliance_Hub_Mts100_All = ns(
    "Appliance.Hub.Mts100.All", mc.KEY_ALL, ARGS_GET | IS_HUB_ID
)
Appliance_Hub_Mts100_Mode = ns(
    "Appliance.Hub.Mts100.Mode", mc.KEY_MODE, ARGS_GETSETPUSH | IS_HUB_ID
)
Appliance_Hub_Mts100_Schedule = ns(
    "Appliance.Hub.Mts100.Schedule", mc.KEY_SCHEDULE, ARGS_GETSETPUSH | IS_HUB_ID
)
Appliance_Hub_Mts100_ScheduleB = ns(
    "Appliance.Hub.Mts100.ScheduleB", mc.KEY_SCHEDULE, ARGS_GETSETPUSH | IS_HUB_ID
)
Appliance_Hub_Mts100_Temperature = ns(
    "Appliance.Hub.Mts100.Temperature", mc.KEY_TEMPERATURE, ARGS_GETSETPUSH | IS_HUB_ID
)
Appliance_Hub_Mts100_TimeSync = ns(
    "Appliance.Hub.Mts100.TimeSync", None, ARGS_GETPUSH | IS_HUB_ID
)
Appliance_Hub_Mts100_SuperCtl = ns(
    "Appliance.Hub.Mts100.SuperCtl", None, ARGS_GETPUSH | IS_HUB_ID
)
Appliance_Hub_Sensor_Adjust = ns(
    "Appliance.Hub.Sensor.Adjust", mc.KEY_ADJUST, ARGS_GETSET | IS_HUB_ID
)
Appliance_Hub_Sensor_Alert = ns(
    "Appliance.Hub.Sensor.Alert", None, ARGS_GETPUSH | IS_HUB_ID
)
Appliance_Hub_Sensor_All = ns(
    "Appliance.Hub.Sensor.All", mc.KEY_ALL, ARGS_GET | IS_HUB_ID
)
Appliance_Hub_Sensor_DoorWindow = ns(
    "Appliance.Hub.Sensor.DoorWindow", mc.KEY_DOORWINDOW, ARGS_GETPUSH | IS_HUB_ID
)
Appliance_Hub_Sensor_Latest = ns(
    "Appliance.Hub.Sensor.Latest", mc.KEY_LATEST, ARGS_GETPUSH | IS_HUB_ID
)
Appliance_Hub_Sensor_Motion = ns(
    "Appliance.Hub.Sensor.Motion", None, ARGS_GETPUSH | IS_HUB_ID
)
Appliance_Hub_Sensor_Smoke = ns(
    "Appliance.Hub.Sensor.Smoke", mc.KEY_SMOKEALARM, ARGS_GETPUSH | IS_HUB_ID
)
Appliance_Hub_Sensor_TempHum = ns(
    "Appliance.Hub.Sensor.TempHum", mc.KEY_TEMPHUM, ARGS_GETPUSH | IS_HUB_ID
)
Appliance_Hub_Sensor_WaterLeak = ns(
    "Appliance.Hub.Sensor.WaterLeak", mc.KEY_WATERLEAK, ARGS_GETPUSH | IS_HUB_ID
)
Appliance_Hub_SubDevice_Beep = ns(
    "Appliance.Hub.SubDevice.Beep", None, ARGS_GETPUSH | IS_HUB_ID
)
Appliance_Hub_SubDevice_MotorAdjust = ns(
    "Appliance.Hub.SubDevice.MotorAdjust", mc.KEY_ADJUST, ARGS_GETPUSH | IS_HUB_ID
)
Appliance_Hub_SubDevice_Version = ns(
    "Appliance.Hub.SubDevice.Version", mc.KEY_VERSION, ARGS_GETPUSH | IS_HUB_ID
)
