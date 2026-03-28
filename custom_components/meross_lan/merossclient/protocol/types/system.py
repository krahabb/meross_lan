"""
A collection of typing definitions for payloads in Appliance.System.*
"""

from . import (
    Any,
    NotRequired,
    TypedDict,
    control,
    diffuser,
    garagedoor,
    hub,
    thermostat,
)
from .. import types as mt


class Debug_System(TypedDict):
    version: str  # 2.3.8
    sysUpTime: str  # 118h19m27s
    UTC: int  # 57253149
    localTimeOffset: int  # 0
    localTime: str  # Mon Oct 25 15:39:09 1971
    suncalc: str  # 5:40;17:47
    memTotal: int  # 409600
    memFree: int  # 50972
    memMini: int  # 43248


class Debug_DisconnectDetail(TypedDict):
    totalCount: int  # 0
    detials: list


class Debug_Network(TypedDict):
    linkStatus: str  # disconnect
    channel: int  # 0
    ssid: str  # MEROSS_STA
    gatewayMac: str  # The station is in disconnect status
    innerIp: str  # 0.0.0.0
    wifiDisconnectCount: int  # 0
    wifiDisconnectDetail: Debug_DisconnectDetail


class Debug_Cloud(TypedDict):
    linkStatus: str  # disconnect
    activeServer: str  #
    mainServer: str  #
    mainPort: int  # 0
    secondServer: str  #
    secondPort: int  # 0
    userId: int  # 0
    sysConnectTime: str  # N/A
    sysOnlineTime: str  # N/A
    sysDisconnectCount: int  # 0
    iotDisconnectDetail: Debug_DisconnectDetail


class _Debug_Hub_SubDevice_Data(TypedDict):
    hardware: str
    firmware: str
    online: NotRequired[int]


class _Debug_Hub_SubDevice(TypedDict):
    id: str
    # This dictionary, beside the subdev id, has only one of the next listed keys.
    # It might look like this key is the same as the one presented in Hub.Digest
    ms100: NotRequired[_Debug_Hub_SubDevice_Data]
    mts100v3: NotRequired[_Debug_Hub_SubDevice_Data]


class Debug_Hub(TypedDict):
    channel: int
    subdevice: list[_Debug_Hub_SubDevice]


class Debug(TypedDict):
    system: Debug_System
    network: Debug_Network
    cloud: Debug_Cloud
    hub: NotRequired[Debug_Hub]


class Hardware(TypedDict):
    """Appliance.System.Hardware"""

    type: str  # msh450
    subType: str  # un
    version: str  # 9.0.0
    chipType: str  # rtl8720cm
    uuid: str
    macAddress: str


class Firmware(TypedDict):
    """Appliance.System.Firmware"""

    version: str  # 9.1.17
    compileTime: str  # 2025/08/28-09:11:02
    encrypt: int  # 1
    wifiMac: str
    innerIp: str
    server: str
    port: int
    secondServer: str  # lately NotRequired
    secondPort: int  # lately NotRequired
    userId: int


type Time_Timerule = list[int]  # [timestamp, offset, dst]


class Time(TypedDict):
    """Appliance.System.Time"""

    timestamp: NotRequired[int]
    timezone: str
    timeRule: list[Time_Timerule]


class Online(TypedDict):
    """Appliance.System.Online"""

    status: int  # see mc.STATUS_ symbols
    bindId: NotRequired[str]  # VStpNGCZi8AGQPRG
    who: NotRequired[int]  # 1


class All_System(TypedDict):
    hardware: Hardware
    firmware: Firmware
    time: Time
    online: Online


# This keys are all optional depending on the device layout.
# Here we'll set them as available to ease type-checking.
# TODO: detail type-hints
All_Digest = TypedDict(
    "All_Digest",
    {
        "diffuser": diffuser.Digest,
        "fan": list[control.Fan],
        "garageDoor": list[garagedoor.State],
        "hub": hub.Digest,
        "thermostat": thermostat.Digest,
        "togglex": list[mt.ChannelOnOff],
        "triggerx": mt.JsonList,
        "timerx": mt.JsonList,
        "light": control.Light,
        "light.effect": list[control.Light_Effect],
        "spray": list[control.Spray],
    },
)


class All_Control(TypedDict):

    toggle: mt.JsonDict
    trigger: Any
    timer: Any


class All(TypedDict):
    system: All_System
    digest: All_Digest
    control: All_Control  # 'legacy' key only seen in mss210 (same meaning as 'digest though')
