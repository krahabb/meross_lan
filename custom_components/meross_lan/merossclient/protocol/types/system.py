"""
A collection of typing definitions for payloads in Appliance.System.*
"""

from . import TypedDict


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


class Debug(TypedDict):
    system: Debug_System
    network: Debug_Network
    cloud: Debug_Cloud
