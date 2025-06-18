"""
A collection of typing definitions for payloads
in Appliance.Control.Thermostat
"""

from . import ChannelPayload, NotRequired, TypedDict


class CommonTemperature_C(ChannelPayload):
    """Common base TypedDict carried in various Appliance.Control.Thermostat.* namespaces.
    "calibration": {"channel": 0, "value": 0, "min": -80, "max": 80, "lmTime": 1697010767}
    "deadZone": {"channel":0,"value":300,"min":50,"max":2000}
    "frost": {"channel": 0, "onoff": 1, "value": 500, "min": 500, "max": 1500, "warning": 0}
    "overheat": {"warning": 0, "value": 335, "onoff": 1, "min": 200, "max": 700,
        "lmTime": 1674121910, "currentTemp": 355, "channel": 0}
    """

    value: int  # usually a temperature
    max: int  # min/max for value
    min: int
    lmTime: int


class CommonTemperatureExt_C(CommonTemperature_C):
    """{"channel": 0, "onoff": 1, "value": 500, "min": 500, "max": 1500, "warning": 0}"""

    onoff: int  # to enable/disable the feature
    warning: int


class Calibration_C(CommonTemperature_C):
    """{"channel": 0, "value": 0, "min": -80, "max": 80, "lmTime": 1697010767}"""

    humiValue: NotRequired[int]  # only mts300


class DeadZone_C(CommonTemperature_C):
    """{"channel":0,"value":300,"min":50,"max":2000}"""

    pass


class Frost_C(CommonTemperatureExt_C):
    """{"channel": 0, "onoff": 1, "value": 500, "min": 500, "max": 1500, "warning": 0}"""

    pass


class HoldAction_C(ChannelPayload):
    mode: int  # 0: permanent, 1: until next schedule, 2: on timer (in 'time' field)
    expire: NotRequired[int]  # seen in a PUSH on mts200
    time: NotRequired[
        int
    ]  # associated with mode 2: it is the 'hold' duration in minutes


class Overheat_C(CommonTemperatureExt_C):
    """
    {"warning": 0, "value": 335, "onoff": 1, "min": 200, "max": 700,
        "lmTime": 1674121910, "currentTemp": 355, "channel": 0}
    """

    currentTemp: int  # external sensor temp


class ModeC_fan(TypedDict):
    fMode: int  # 0
    speed: int  # 0
    hTime: int  # 99999


class ModeC_more(TypedDict):
    hdStatus: int  # 0
    humi: int  # 495
    cStatus: int  # 0
    hStatus: int  # 0
    fStatus: int  # 0
    aStatus: int  # 0


class ModeC_targetTemp(TypedDict):
    heat: int  # 2100
    cold: int  # 2400


class ModeC_C(ChannelPayload):
    """
    {
        "fan": {
            "fMode": 0,
            "speed": 0,
            "hTime": 99999
        },
        "sensorTemp": 2200,
        "currentTemp": 2200,
        "more": {
            "hdStatus": 0,
            "humi": 495,
            "cStatus": 0,
            "hStatus": 0,
            "fStatus": 0,
            "aStatus": 0
        },
        "channel": 0,
        "mode": 3,
        "work": 2,
        "targetTemp": {
            "heat": 2100,
            "cold": 2400
        }
    }
    "sensorTemp" is the temperature of the built-in sensor of the device.
    "currentTemp" is the actual temperature used by the device for cooling and heating (the device supports the external sensor mode).
    "mode" is the current working mode; 0: off ; 1: heat ; 2: cool ; 3: auto.
    For example, if the device operates in auto mode and the target temperature is set to 2100-2400,
    then the device will not work when 21℃<currentTemp<24℃, heat up when the temperature is below 21℃,
    and cool down when the temperature is above 24℃.
    "cStatus" is the refrigeration working status; 0: Idle; Level 1 Colding; 2: Level 2 Colding;
    "humi" is the current humidity
    "cStatus" is the working status of heating. 0:Idle; 1: First-level Heating; 2: Grade 2 Heating; 3: Third-level Heating
    "fStatus" is the status of the fan; 0: Idle; 1: Low/ON; 2: Middle; 3: High
    "aStatus" is the auxiliary heating working state; 0:Idle; 1: First-level AUX 2: Secondary AUX; 3: Three-stage AUX
    "hdStatus" is the dehumidification/humidification working status, 0:Idle; 1: Dehumidification in progress; 2: Humidifying
    "work" is in the state where schedule is enabled; 1: manual  2: schedule
    "fMode" is the fan mode; 0: Auto; 1: ON (Hold);
    "speed" is wind speed; 0: Auto; 1: ON; 2: Middle; 3: High
    "hTime" is hold time, with the unit being minutes. 99999 indicates permanently
    """

    fan: ModeC_fan
    sensorTemp: int
    currentTemp: int
    more: ModeC_more
    mode: int
    work: int
    targetTemp: ModeC_targetTemp
