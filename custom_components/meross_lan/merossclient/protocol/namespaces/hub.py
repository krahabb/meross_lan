"""
Descriptors for hub specific namespaces management.
"""

from .. import const as mc, namespaces as mn

GET_ID = mn.IDX_ID | mn.G_LI
SET_ID = mn.IDX_ID | mn.S_LI
GETSET_ID = mn.IDX_ID | mn.G_LI | mn.S_LI
GETPSH_ID = GET_ID | mn.PSH

GET_SUBID = mn.IDX_SUB | mn.G_LIS
GETSET_SUBID = GET_SUBID | mn.S_LI
GETSETPSH_SUBID = GETSET_SUBID | mn.PSH
GETSETPSQ_SUBID = GETSET_SUBID | mn.PSQ


Appliance_Config_WaterPlan = mn.ns(
    "Appliance.Config.WaterPlan", mc.KEY_CONFIG, -1, GETSET_SUBID, mn.EXP
)  # mst100 (used to read/write watering schedules)
Appliance_Control_Water = mn.ns(
    "Appliance.Control.Water", mc.KEY_CONTROL, 50, GETSETPSH_SUBID
)  # mst100
Appliance_Control_WaterEvent = mn.ns(
    "Appliance.Control.WaterEvent", mc.KEY_CONTROL, -1, mn.IDX_SUB, mn.PSH, mn.EXP
)  # mst100 (used to report events after each watering cycle is completed)
Appliance_Control_WaterEvent_Skip = mn.ns(
    "Appliance.Control.WaterEvent.Skip", mc.KEY_CONTROL, -1, GETSET_SUBID, mn.EXP
)  # mst100 (used in the app to skip watering on specific days according to the schedule)
Appliance_Control_WaterPlan_Skip = mn.ns(
    "Appliance.Control.WaterPlan.Skip", mc.KEY_CONTROL, -1, GETSET_SUBID, mn.EXP
)  # mst100 (allows the device to query cloud server about whether to skip execution on a specific day based on weather conditions)

Appliance_Digest_Hub = mn.ns("Appliance.Digest.Hub", mc.KEY_HUB, -1, mn.G_D, mn.DIG)
Appliance_Digest_WaterPlan = mn.ns(
    "Appliance.Digest.WaterPlan", mc.KEY_DIGEST, -1, GETSET_SUBID, mn.EXP
)  # mst100 (used to read/write watering schedules)
Appliance_Hub_Battery = mn.ns("Appliance.Hub.Battery", mc.KEY_BATTERY, 40, GETPSH_ID)
Appliance_Hub_Exception = mn.ns(
    "Appliance.Hub.Exception", mc.KEY_EXCEPTION, -1, mn.PSQ, mn.IDX_ID
)
Appliance_Hub_ExtraInfo = mn.ns(
    "Appliance.Hub.ExtraInfo", "extraInfo", -1, mn.G_D
)  # upgrade info for subdevices
Appliance_Hub_Mts100_Adjust = mn.ns(
    "Appliance.Hub.Mts100.Adjust", mc.KEY_ADJUST, 40, GETSET_ID
)
Appliance_Hub_Mts100_All = mn.ns("Appliance.Hub.Mts100.All", mc.KEY_ALL, 350, GET_ID)
Appliance_Hub_Mts100_Config = mn.ns(
    "Appliance.Hub.Mts100.Config", mc.KEY_CONFIG, -1, GETSET_ID
)
Appliance_Hub_Mts100_Mode = mn.ns(
    "Appliance.Hub.Mts100.Mode", mc.KEY_MODE, -1, GETSET_ID, mn.PSH
)
Appliance_Hub_Mts100_Schedule = mn.ns(
    "Appliance.Hub.Mts100.Schedule", mc.KEY_SCHEDULE, -1, GETSET_ID, mn.PSH
)
Appliance_Hub_Mts100_ScheduleB = mn.ns(
    "Appliance.Hub.Mts100.ScheduleB", mc.KEY_SCHEDULE, 500, GETSET_ID, mn.PSH
)
Appliance_Hub_Mts100_Temperature = mn.ns(
    "Appliance.Hub.Mts100.Temperature", mc.KEY_TEMPERATURE, -1, GETSET_ID, mn.PSH
)
Appliance_Hub_Mts100_TimeSync = mn.ns(
    "Appliance.Hub.Mts100.TimeSync", "timeSync", -1, SET_ID, mn.PSH
)
Appliance_Hub_Mts100_SuperCtl = mn.ns(
    "Appliance.Hub.Mts100.SuperCtl", "superCtl", -1, GET_ID, mn.PSH
)
Appliance_Hub_Online = mn.ns("Appliance.Hub.Online", mc.KEY_ONLINE, -1, GETPSH_ID)
Appliance_Hub_PairSubDev = mn.ns("Appliance.Hub.PairSubDev", mc.KEY_, -1, mn.S_E)
Appliance_Hub_Report = mn.ns("Appliance.Hub.Report", "report", -1, GETPSH_ID, mn.EXP)
Appliance_Hub_Sensitivity = mn.ns(
    "Appliance.Hub.Sensitivity", "sensitivity", -1, GETSET_ID, mn.PSH
)
Appliance_Hub_Sensor_Adjust = mn.ns(
    "Appliance.Hub.Sensor.Adjust", mc.KEY_ADJUST, 60, GETSET_ID
)
Appliance_Hub_Sensor_Alert = mn.ns(
    "Appliance.Hub.Sensor.Alert", mc.KEY_ALERT, -1, GETPSH_ID
)
Appliance_Hub_Sensor_All = mn.ns("Appliance.Hub.Sensor.All", mc.KEY_ALL, 250, GET_ID)
Appliance_Hub_Sensor_DoorWindow = mn.ns(
    "Appliance.Hub.Sensor.DoorWindow", mc.KEY_DOORWINDOW, -1, GETPSH_ID
)
Appliance_Hub_Sensor_Latest = mn.ns(
    "Appliance.Hub.Sensor.Latest", mc.KEY_LATEST, -1, GETPSH_ID
)
Appliance_Hub_Sensor_Motion = mn.ns(
    "Appliance.Hub.Sensor.Motion", "motion", -1, GETPSH_ID
)
Appliance_Hub_Sensor_Smoke = mn.ns(
    "Appliance.Hub.Sensor.Smoke", mc.KEY_SMOKEALARM, -1, GETSET_ID
)
Appliance_Hub_Sensor_TempHum = mn.ns(
    "Appliance.Hub.Sensor.TempHum", mc.KEY_TEMPHUM, -1, GETPSH_ID
)
Appliance_Hub_Sensor_WaterLeak = mn.ns(
    "Appliance.Hub.Sensor.WaterLeak", mc.KEY_WATERLEAK, -1, GETPSH_ID
)
Appliance_Hub_SubdeviceList = mn.ns(
    "Appliance.Hub.SubdeviceList", "subdeviceList", -1, mn.PSH
)
Appliance_Hub_SubDevice_Beep = mn.ns(
    "Appliance.Hub.SubDevice.Beep", mc.KEY_ALARM, 35, GETSET_ID
)
Appliance_Hub_SubDevice_Lock = mn.ns(
    "Appliance.Hub.SubDevice.Lock", mc.KEY_LOCK, 35, GETSET_ID
)
Appliance_Hub_SubDevice_MotorAdjust = mn.ns(
    "Appliance.Hub.SubDevice.MotorAdjust",
    mc.KEY_ADJUST,
    -1,
    SET_ID | mn.G_LIS,  # this appears also with "motor_adjust" key in SET
)
Appliance_Hub_SubDevice_Version = mn.ns(
    "Appliance.Hub.SubDevice.Version", mc.KEY_VERSION, 55, GETPSH_ID
)
Appliance_Hub_ToggleX = mn.ns(
    "Appliance.Hub.ToggleX", mc.KEY_TOGGLEX, 35, GETSET_ID, mn.PSH
)
