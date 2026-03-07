"""
A collection of typing definitions for payloads in Appliance.Hub.*
"""

from . import NotRequired, TypedDict


class IdPayload(TypedDict):
    id: str


class SubIdPayload(TypedDict):
    subId: str
    channel: int


class Battery(IdPayload):
    """Appliance.Hub.Battery"""

    value: int


class _Online(TypedDict):
    status: int
    lastActiveTime: int


class Online(_Online, IdPayload):
    """Appliance.Hub.Online"""


class _ToggleX(TypedDict):
    onoff: int  # 1: on, 0: off


class ToggleX(_ToggleX, IdPayload):
    """Appliance.Hub.ToggleX"""

    pass


class _Mts100_Mode(TypedDict):
    """Appliance.Hub.Mts100.Mode"""

    state: int  # MTS100_MODE_* constants


class Mts100_Mode(_Mts100_Mode, IdPayload):
    """Appliance.Hub.Mts100.Mode"""

    pass


class _Mts100_Temperature(TypedDict):

    room: int
    currentSet: int
    custom: int
    comfort: int
    economy: int
    max: int
    min: int
    heating: int  # 1: on, 0: off
    away: int
    openWindow: int  # 1: open, 0: closed


class Mts100_Temperature(_Mts100_Temperature, IdPayload):
    """Appliance.Hub.Mts100.Temperature"""

    pass


class Mts100_All(IdPayload):
    """Appliance.Hub.Mts100.All"""

    online: _Online
    scheduleBMode: int
    togglex: NotRequired[ToggleX]
    mode: NotRequired[_Mts100_Mode]
    temperature: NotRequired[_Mts100_Temperature]


class Sensor_Adjust(IdPayload):
    """Appliance.Hub.Sensor.Adjust"""

    temperature: int
    humidity: int


class _Sensor_LatestSample(TypedDict):
    # TODO: maybe generalize this for other usages
    sample: int
    time: int


class Sensor_Latest(IdPayload):
    """Appliance.Hub.Sensor.Latest"""

    temperature: _Sensor_LatestSample
    humidity: _Sensor_LatestSample


class Sensor_TempHum(IdPayload):
    """Appliance.Hub.Sensor.TempHum"""

    latestTemperature: int
    latestHumidity: int
    latestTime: int
    sample: list[list[int]]  # [temperature, humidity, startTime, endTime]


class _smokeAlarm(TypedDict):
    status: int
    lmTime: int
    interConn: int


class Sensor_Smoke(_smokeAlarm, IdPayload):
    """Appliance.Hub.Sensor.Smoke"""

    pass


class Sensor_All(IdPayload):
    """Appliance.Hub.Sensor.All"""

    online: _Online


class _Sensor_AllSample(TypedDict):
    latest: int
    latestSampleTime: int
    max: int
    min: int


class Sensor_All_ms100(Sensor_All):
    """Appliance.Hub.Sensor.All for ms100 subdevice."""

    temperature: _Sensor_AllSample
    humidity: _Sensor_AllSample


class Sensor_All_ms130(Sensor_All_ms100):
    """Appliance.Hub.Sensor.All for ms130 subdevice."""

    pass


class Sensor_All_gs559(Sensor_All):
    """Appliance.Hub.Sensor.All for ms100 subdevice."""

    smokeAlarm: _smokeAlarm


class SubDevice_Beep(IdPayload):
    """Appliance.Hub.SubDevice.Beep"""

    onoff: int  # 1: on, 0: off


class SubDevice_Version(IdPayload):
    """Appliance.Hub.SubDevice.Version"""

    hardware: str
    firmware: str


class Digest_SubDevice(_Online, ToggleX, IdPayload):
    """Common fields for subdevices in hub digest."""

    pass


class _ms100(TypedDict):
    latestTime: int
    latestTemperature: int
    latestHumidity: int
    voltage: int


class Digest_ms100(Digest_SubDevice):
    """Digest payload for ms100 subdevice."""

    ms100: _ms100


class _tempHumi(TypedDict):
    latestTime: int
    temp: int
    humi: int


class Digest_ms130(Digest_SubDevice):
    """Digest payload for ms130 subdevice."""

    tempHumi: _tempHumi


class _mts100v3(TypedDict):
    mode: int


class Digest_mts100v3(Digest_SubDevice):
    """Digest payload for mts100v3 subdevice."""

    scheduleBMode: int
    mts100v3: _mts100v3


class _mts150(TypedDict):
    mode: int
    # TODO: more keys


class Digest_mts150(Digest_SubDevice):
    """Digest payload for mts150 subdevice."""

    scheduleBMode: int
    mts150: _mts150


class Digest_gs559(Digest_SubDevice):
    """Digest payload for smoke subdevice."""

    smokeAlarm: _smokeAlarm


class _mst(TypedDict):
    ts: int
    dura: int
    wflow: int


class Digest_mst100(Digest_SubDevice):
    """Digest payload for mst subdevice."""

    mst: _mst


class Digest_Hub(TypedDict):
    """Appliance.Digest.Hub"""

    hubId: int
    mode: int
    nvdmChl: NotRequired[int]
    workChl: NotRequired[int]
    curChl: NotRequired[int]
    subdevice: list[Digest_SubDevice]
