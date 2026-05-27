"""
A collection of typing definitions for payloads in Appliance.Hub.*
"""

from . import NotRequired, TypedDict
from .. import types as mt


class SubIdPayload(mt.SubIdPayload):
    """Common payload including a 'subId' field.
    This payload structure is becoming common in new devices and subdevices.
    When used in a device payload it misses teh subId field and becomes a standard 'ChannelPayload'.
    """

    subId: str
    channel: int  # typically 0 when used in subdevice payloads
    channels: NotRequired[list[int]]


class Battery(mt.IdPayload):
    """Appliance.Hub.Battery"""

    value: int


class _Online(TypedDict):
    status: int
    lastActiveTime: int


class Online(_Online, mt.IdPayload):
    """Appliance.Hub.Online"""


class _ToggleX(TypedDict):
    onoff: int  # 1: on, 0: off


class ToggleX(_ToggleX, mt.IdPayload):
    """Appliance.Hub.ToggleX"""

    pass


class Mts100_Config_pid(TypedDict):
    grade: int
    p: int
    i: int
    d: NotRequired[int]  # only for mts150p


class Mts100_Config(mt.IdPayload):
    """Appliance.Hub.Mts100.Config"""

    # ns supported in mts150/mts150p
    pid: Mts100_Config_pid


class _Mts100_Mode(TypedDict):
    """Appliance.Hub.Mts100.Mode"""

    state: int  # MTS100_MODE_* constants


class Mts100_Mode(_Mts100_Mode, mt.IdPayload):
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


class Mts100_Temperature(_Mts100_Temperature, mt.IdPayload):
    """Appliance.Hub.Mts100.Temperature"""

    pass


class Mts100_All(mt.IdPayload):
    """Appliance.Hub.Mts100.All"""

    online: _Online
    scheduleBMode: int
    togglex: NotRequired[ToggleX]
    mode: NotRequired[_Mts100_Mode]
    temperature: NotRequired[_Mts100_Temperature]


class Sensor_Adjust(mt.IdPayload):
    """Appliance.Hub.Sensor.Adjust"""

    temperature: int
    humidity: int


class _Sensor_LatestSample(TypedDict):
    sample: int
    time: int


class Sensor_Latest(mt.IdPayload):
    """Appliance.Hub.Sensor.Latest"""

    temperature: _Sensor_LatestSample
    humidity: _Sensor_LatestSample


class _ms100(TypedDict):
    latestTime: int
    latestTemperature: int
    latestHumidity: int
    voltage: int


class Sensor_TempHum(_ms100, mt.IdPayload):
    """Appliance.Hub.Sensor.TempHum"""

    voltage: NotRequired[int]  # not sure
    sample: list[list[int]]  # [temperature, humidity, startTime, endTime]


class _gs559(TypedDict):
    status: int
    lmTime: int
    interConn: int


class Sensor_Smoke(_gs559, mt.IdPayload):
    """Appliance.Hub.Sensor.Smoke"""

    pass


class Sensor_All(mt.IdPayload):
    """Appliance.Hub.Sensor.All"""

    online: _Online


class Sensor_All_gs559(Sensor_All):
    """Appliance.Hub.Sensor.All for gs559(smokeAlarm) subdevice."""

    smokeAlarm: _gs559


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


class SubDevice_Beep(mt.IdPayload):
    """Appliance.Hub.SubDevice.Beep"""

    onoff: int  # 1: on, 0: off


class SubDevice_Lock(mt.IdPayload):
    """Appliance.Hub.SubDevice.Lock"""

    state: int  # 1: on, 0: off
    exception: NotRequired[str]


class SubDevice_Version(mt.IdPayload):
    """Appliance.Hub.SubDevice.Version"""

    hardware: str
    firmware: str


class Water(SubIdPayload):
    """Appliance.Control.Water"""

    dura: NotRequired[int]  # duration in seconds
    lmTime: int
    onoff: int  # 1: on, 2: off


class Digest_SubDevice(_Online, ToggleX, mt.IdPayload):
    """Common fields for subdevices in hub digest."""

    doorWindow: NotRequired[mt.JsonDict]
    ms100: NotRequired[_ms100]
    mst: NotRequired[_mst100 | _mst200]
    mts100: NotRequired[_mts100v3]
    mts100v3: NotRequired[_mts100v3]
    mts150: NotRequired[_mts150]
    smokeAlarm: NotRequired[_gs559]
    tempHum: NotRequired[mt.JsonDict]
    tempHumi: NotRequired[_ms130]
    waterLeak: NotRequired[mt.JsonDict]


class Digest_gs559(Digest_SubDevice):
    """Digest payload for smoke subdevice."""

    smokeAlarm: _gs559


class Digest_ms100(Digest_SubDevice):
    """Digest payload for ms100 subdevice."""

    ms100: _ms100


class _ms130(TypedDict):
    latestTime: int
    temp: int
    humi: int


class Digest_ms130(Digest_SubDevice):
    """Digest payload for ms130 subdevice."""

    tempHumi: _ms130


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


class _mst100(TypedDict):
    ts: int
    dura: int
    wflow: int


class _mst200_C(_mst100):
    channel: int


class _mst200(TypedDict):
    waDet: list[_mst200_C]


class Digest_mst(Digest_SubDevice):
    """Digest payload for mst subdevices."""

    mst: _mst100 | _mst200


class Digest(TypedDict):
    """Appliance.Digest.Hub"""

    hubId: int
    mode: int
    nvdmChl: NotRequired[int]
    workChl: NotRequired[int]
    curChl: NotRequired[int]
    subdevice: list[Digest_SubDevice]
