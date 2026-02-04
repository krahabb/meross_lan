"""
Descriptors for thermostats specific namespaces management (Appliance.Control.Thermostat.XXX)
"""

from .. import const as mc, namespaces as mn

T: "mn.ns.Args" = {"is_thermostat": True}
GETSET = T | mn.G_LIS | mn.S_LI | mn.IDX_C
GETSETPSH = GETSET | mn.PSH
GETSETPSQ = GETSET | mn.PSQ
GETPSH = T | mn.G_LIS | mn.PSH | mn.IDX_C

Appliance_Control_Thermostat_Alarm = mn.ns(
    "Appliance.Control.Thermostat.Alarm", mc.KEY_ALARM, GETPSH
)
Appliance_Control_Thermostat_AlarmConfig = mn.ns(
    "Appliance.Control.Thermostat.AlarmConfig", mc.KEY_ALARMCONFIG, GETSET
)
Appliance_Control_Thermostat_Calibration = mn.ns(
    "Appliance.Control.Thermostat.Calibration", mc.KEY_CALIBRATION, GETSET
)
Appliance_Control_Thermostat_CompressorDelay = mn.ns(
    "Appliance.Control.Thermostat.CompressorDelay", mc.KEY_DELAY, GETSET
)
Appliance_Control_Thermostat_CtlRange = mn.ns(
    "Appliance.Control.Thermostat.CtlRange", mc.KEY_CTLRANGE, GETSET
)
Appliance_Control_Thermostat_DeadZone = mn.ns(
    "Appliance.Control.Thermostat.DeadZone", mc.KEY_DEADZONE, GETSET
)
Appliance_Control_Thermostat_Frost = mn.ns(
    "Appliance.Control.Thermostat.Frost", mc.KEY_FROST, GETSET
)
Appliance_Control_Thermostat_HoldAction = mn.ns(
    "Appliance.Control.Thermostat.HoldAction", mc.KEY_HOLDACTION, GETSETPSH
)
Appliance_Control_Thermostat_Mode = mn.ns(
    "Appliance.Control.Thermostat.Mode", mc.KEY_MODE, GETSETPSH
)
Appliance_Control_Thermostat_ModeB = mn.ns(
    "Appliance.Control.Thermostat.ModeB", mc.KEY_MODEB, GETSETPSH
)
Appliance_Control_Thermostat_ModeC = mn.ns(
    "Appliance.Control.Thermostat.ModeC", mc.KEY_CONTROL, GETSETPSH
)
Appliance_Control_Thermostat_Overheat = mn.ns(
    "Appliance.Control.Thermostat.Overheat", mc.KEY_OVERHEAT, GETSETPSH
)
Appliance_Control_Thermostat_Schedule = mn.ns(
    "Appliance.Control.Thermostat.Schedule", mc.KEY_SCHEDULE, GETSETPSH
)
Appliance_Control_Thermostat_ScheduleB = mn.ns(
    "Appliance.Control.Thermostat.ScheduleB", mc.KEY_SCHEDULEB, GETSETPSH
)
Appliance_Control_Thermostat_Sensor = mn.ns(
    "Appliance.Control.Thermostat.Sensor", mc.KEY_SENSOR, GETSETPSH
)
Appliance_Control_Thermostat_SummerMode = mn.ns(
    "Appliance.Control.Thermostat.SummerMode", mc.KEY_SUMMERMODE, GETSETPSH
)
Appliance_Control_Thermostat_System = mn.ns(
    "Appliance.Control.Thermostat.System", mc.KEY_CONTROL, GETSETPSQ
)
Appliance_Control_Thermostat_Timer = mn.ns(
    "Appliance.Control.Thermostat.Timer", mc.KEY_TIMER, GETSETPSH
)
Appliance_Control_Thermostat_WindowOpened = mn.ns(
    "Appliance.Control.Thermostat.WindowOpened", mc.KEY_WINDOWOPENED, GETPSH
)
