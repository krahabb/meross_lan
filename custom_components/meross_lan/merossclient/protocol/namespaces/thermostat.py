"""
Descriptors for thermostats specific namespaces management (Appliance.Control.Thermostat.XXX)
"""

from . import (
    ARGS_GETPUSH,
    ARGS_GETSET,
    ARGS_GETSETPUSH,
    ARGS_GETSETPUSHQ,
    ns,
)
from .. import const as mc

IS_THERMOSTAT: "ns.Args" = {"is_thermostat": True}

Appliance_Control_Thermostat_Alarm = ns(
    "Appliance.Control.Thermostat.Alarm", mc.KEY_ALARM, ARGS_GETPUSH | IS_THERMOSTAT
)
Appliance_Control_Thermostat_AlarmConfig = ns(
    "Appliance.Control.Thermostat.AlarmConfig",
    mc.KEY_ALARMCONFIG,
    ARGS_GETSET | IS_THERMOSTAT,
)
Appliance_Control_Thermostat_Calibration = ns(
    "Appliance.Control.Thermostat.Calibration",
    mc.KEY_CALIBRATION,
    ARGS_GETSET | IS_THERMOSTAT,
)
Appliance_Control_Thermostat_CompressorDelay = ns(
    "Appliance.Control.Thermostat.CompressorDelay",
    mc.KEY_DELAY,
    ARGS_GETSET | IS_THERMOSTAT,
)
Appliance_Control_Thermostat_CtlRange = ns(
    "Appliance.Control.Thermostat.CtlRange",
    mc.KEY_CTLRANGE,
    ARGS_GETSET | IS_THERMOSTAT,
)
Appliance_Control_Thermostat_DeadZone = ns(
    "Appliance.Control.Thermostat.DeadZone",
    mc.KEY_DEADZONE,
    ARGS_GETSET | IS_THERMOSTAT,
)
Appliance_Control_Thermostat_Frost = ns(
    "Appliance.Control.Thermostat.Frost", mc.KEY_FROST, ARGS_GETSET | IS_THERMOSTAT
)
Appliance_Control_Thermostat_HoldAction = ns(
    "Appliance.Control.Thermostat.HoldAction",
    mc.KEY_HOLDACTION,
    ARGS_GETSETPUSH | IS_THERMOSTAT,
)
Appliance_Control_Thermostat_Mode = ns(
    "Appliance.Control.Thermostat.Mode", mc.KEY_MODE, ARGS_GETSETPUSH | IS_THERMOSTAT
)
Appliance_Control_Thermostat_ModeB = ns(
    "Appliance.Control.Thermostat.ModeB", mc.KEY_MODEB, ARGS_GETSETPUSH | IS_THERMOSTAT
)
Appliance_Control_Thermostat_ModeC = ns(
    "Appliance.Control.Thermostat.ModeC",
    mc.KEY_CONTROL,
    ARGS_GETSETPUSH | IS_THERMOSTAT,
)
Appliance_Control_Thermostat_Overheat = ns(
    "Appliance.Control.Thermostat.Overheat",
    mc.KEY_OVERHEAT,
    ARGS_GETSETPUSH | IS_THERMOSTAT,
)
Appliance_Control_Thermostat_Schedule = ns(
    "Appliance.Control.Thermostat.Schedule",
    mc.KEY_SCHEDULE,
    ARGS_GETSETPUSH | IS_THERMOSTAT,
)
Appliance_Control_Thermostat_ScheduleB = ns(
    "Appliance.Control.Thermostat.ScheduleB",
    mc.KEY_SCHEDULEB,
    ARGS_GETSETPUSH | IS_THERMOSTAT,
)
Appliance_Control_Thermostat_Sensor = ns(
    "Appliance.Control.Thermostat.Sensor", mc.KEY_SENSOR, ARGS_GETPUSH | IS_THERMOSTAT
)
Appliance_Control_Thermostat_SummerMode = ns(
    "Appliance.Control.Thermostat.SummerMode",
    mc.KEY_SUMMERMODE,
    ARGS_GETSETPUSH | IS_THERMOSTAT,
)
Appliance_Control_Thermostat_System = ns(
    "Appliance.Control.Thermostat.System", mc.KEY_CONTROL, ARGS_GETSETPUSHQ | IS_THERMOSTAT
)
Appliance_Control_Thermostat_Timer = ns(
    "Appliance.Control.Thermostat.Timer", mc.KEY_TIMER, ARGS_GETSETPUSH | IS_THERMOSTAT
)
Appliance_Control_Thermostat_WindowOpened = ns(
    "Appliance.Control.Thermostat.WindowOpened",
    mc.KEY_WINDOWOPENED,
    ARGS_GETPUSH | IS_THERMOSTAT,
)
