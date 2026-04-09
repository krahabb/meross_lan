"""
A collection of typing definitions for payloads in Appliance.Config.*
"""

from . import TypedDict
from .. import types as mt


class mstCfg_calibration(TypedDict):
    waCon: int  # water consumption
    onoff: int
    lmTime: int


class mstCfg(TypedDict):
    dura: int  # duration of watering in seconds
    wfm: int  # water flow measurement
    calibration: mstCfg_calibration


class DeviceCfg(mt.SubIdPayload):
    """Appliance.Config.DeviceCfg payload structure.
    Note: some fields are not always present, they depend on the model and/or firmware version.
    """

    coverCfg: mt.JsonDict
    unitCfg: mt.JsonDict
    calibrateCfg: mt.JsonDict
    timeCfg: mt.JsonDict
    ms130Cfg: mt.JsonDict
    mrs200Cfg: mt.JsonDict
    mstCfg: mstCfg
    mts300Cfg: mt.JsonDict
    switchInputCfg: mt.JsonDict
    switchOutputCfg: mt.JsonDict
    overVoltageCfg: mt.JsonDict
    networkCfg: mt.JsonDict
    coverCfg: mt.JsonDict


class Wifi(TypedDict):
    ssid: str  # base64 encoded
    password: str  # base64 encoded or encrypted (WifiX)

    bssid: str
    channel: int
    signal: int
    encryption: int
    cipher: int


type WifiList = list[Wifi]
