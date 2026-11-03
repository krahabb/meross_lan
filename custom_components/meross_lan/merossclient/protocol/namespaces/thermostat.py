"""
Descriptors for thermostats specific namespaces management (Appliance.Control.Thermostat.XXX)
"""

from .. import const as mc, namespaces as mn

Appliance_Control_Thermostat_Alarm = mn.ns(
    "Appliance.Control.Thermostat.Alarm", mc.KEY_ALARM, -1, mn.GP_LIS_C
)
Appliance_Control_Thermostat_AlarmConfig = mn.ns(
    "Appliance.Control.Thermostat.AlarmConfig",
    mc.KEY_ALARMCONFIG,
    -1,
    mn.GS_LIS_C,
    mn.C_CFG,
)
Appliance_Control_Thermostat_Calibration = mn.ns(
    "Appliance.Control.Thermostat.Calibration",
    mc.KEY_CALIBRATION,
    80,
    mn.GS_LIS_C,
    mn.C_CFG,
)
Appliance_Control_Thermostat_CompressorDelay = mn.ns(
    "Appliance.Control.Thermostat.CompressorDelay", mc.KEY_DELAY, -1, mn.GS_LIS_C
)
Appliance_Control_Thermostat_CtlRange = mn.ns(
    "Appliance.Control.Thermostat.CtlRange", mc.KEY_CTLRANGE, 80, mn.GS_LIS_C, mn.C_CON
)
Appliance_Control_Thermostat_DeadZone = mn.ns(
    "Appliance.Control.Thermostat.DeadZone", mc.KEY_DEADZONE, 80, mn.GS_LIS_C, mn.C_CFG
)
Appliance_Control_Thermostat_Frost = mn.ns(
    "Appliance.Control.Thermostat.Frost", mc.KEY_FROST, 80, mn.GS_LIS_C, mn.C_SNS_S
)
Appliance_Control_Thermostat_HoldAction = mn.ns(
    "Appliance.Control.Thermostat.HoldAction",
    mc.KEY_HOLDACTION,
    30,
    mn.GSP_LIS_C,
    mn.C_CFG,
)
Appliance_Control_Thermostat_Mode = mn.ns(
    "Appliance.Control.Thermostat.Mode", mc.KEY_MODE, -1, mn.GSP_LIS_C, mn.DIG
)
Appliance_Control_Thermostat_ModeB = mn.ns(
    "Appliance.Control.Thermostat.ModeB", mc.KEY_MODEB, -1, mn.GSP_LIS_C, mn.DIG
)
Appliance_Control_Thermostat_ModeC = mn.ns(
    "Appliance.Control.Thermostat.ModeC", mc.KEY_CONTROL, 120, mn.GSP_LIS_C
)
Appliance_Control_Thermostat_Overheat = mn.ns(
    "Appliance.Control.Thermostat.Overheat",
    mc.KEY_OVERHEAT,
    140,
    mn.GSP_LIS_C,
    mn.C_SNS_S,
)
Appliance_Control_Thermostat_Schedule = mn.ns(
    "Appliance.Control.Thermostat.Schedule",
    mc.KEY_SCHEDULE,
    550,
    mn.GSP_LIS_C,
    mn.C_CFG,
)
Appliance_Control_Thermostat_ScheduleB = mn.ns(
    "Appliance.Control.Thermostat.ScheduleB",
    mc.KEY_SCHEDULEB,
    550,
    mn.GSP_LIS_C,
    mn.C_CFG,
)
Appliance_Control_Thermostat_Sensor = mn.ns(
    "Appliance.Control.Thermostat.Sensor", mc.KEY_SENSOR, 40, mn.GSP_LIS_C, mn.C_SNS_S
)
Appliance_Control_Thermostat_SummerMode = mn.ns(
    "Appliance.Control.Thermostat.SummerMode",
    mc.KEY_SUMMERMODE,
    22,
    mn.GSP_LIS_C,
    mn.DIG,
    mn.C_CFG,
)
Appliance_Control_Thermostat_System = mn.ns(
    "Appliance.Control.Thermostat.System", mc.KEY_CONTROL, 580, mn.GSPQ_LIS_C, mn.C_CFG
)
Appliance_Control_Thermostat_Timer = mn.ns(
    "Appliance.Control.Thermostat.Timer", mc.KEY_TIMER, 550, mn.GSP_LIS_C, mn.C_CFG
)
Appliance_Control_Thermostat_WindowOpened = mn.ns(
    "Appliance.Control.Thermostat.WindowOpened",
    mc.KEY_WINDOWOPENED,
    132,
    mn.GP_LIS_C,
    mn.DIG,
    mn.C_SNS_S,
)
