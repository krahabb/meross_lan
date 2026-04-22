"""
A collection of typing definitions for payloads in Appliance.Config.*
"""

from . import NotRequired, TypedDict
from .. import types as mt


class calibrateCfg(TypedDict):
    """Appliance.Config.DeviceCfg subpayload appearing on ms130."""

    humi: int  # humidity calibration value (percentage with one decimal, so 596 means 59.6%)
    temp: int  # temperature calibration value (degrees celsius with two decimals, so 2150 means 21.50°C)


class ms130Cfg_bl(TypedDict):
    """Backlight configuration subpayload for ms130 device configuration."""

    bri: int  # backlight brightness, 1 for low, 2 for medium, 3 for high
    lv: int  # backlight light 'level' (? dunno exact meaning), ranges from 1 to 18 and maps to Lux values)
    sleep: int  # backlight sleep time in seconds, from 3 to 30


class ms130Cfg(TypedDict):
    """Appliance.Config.DeviceCfg subpayload appearing on ms130."""

    bl: ms130Cfg_bl


class mstCfg_calibration(TypedDict):
    # Calibration procedure seems to start by setting onoff to 1 together
    # with waCon indicating a liters (or ml) amount.
    # But this is just speculation
    waCon: int  # water consumption (seems to be)
    onoff: int  # 1 for on, 2 for off
    lmTime: int


class mstCfg(TypedDict):
    dura: int  # duration of watering in seconds
    wfm: int  # water flow measurement
    calibration: mstCfg_calibration


class timeCfg(TypedDict):
    """Appliance.Config.DeviceCfg subpayload appearing on ms130."""

    am: int  # 1 for 12h format, 2 for 24h format


class unitCfg(TypedDict):
    """Appliance.Config.DeviceCfg subpayload appearing on ms130."""

    tempUnit: NotRequired[int]  # 1 for Celsius, 2 for Fahrenheit (ms130)
    unitType: NotRequired[int]  # 1 for US customary, 2 for metric (mst100)


class DeviceCfg(mt.SubIdPayload):
    """Appliance.Config.DeviceCfg payload structure.
    Note: some fields are not always present, they depend on the model and/or firmware version.
    """

    calibrateCfg: calibrateCfg
    timeCfg: timeCfg
    unitCfg: unitCfg
    ms130Cfg: ms130Cfg
    mstCfg: mstCfg
    # still to be inspected:
    coverCfg: mt.JsonDict
    mrs200Cfg: mt.JsonDict
    mts300Cfg: mt.JsonDict
    switchInputCfg: mt.JsonDict
    switchOutputCfg: mt.JsonDict
    overVoltageCfg: mt.JsonDict
    networkCfg: mt.JsonDict
    coverCfg: mt.JsonDict


class DeviceCfgRequest(mt.SubIdPayload):
    """
    Request format for Appliance.Config.DeviceCfg payloads.
    Note: the 'cfgs' field looks like optional and can be used to specify a list of configuration keys to request.
    It looks like, when missing, the device returns the full set (device dependant).
    """

    cfgs: NotRequired[list[str]]


class Wifi(TypedDict):
    ssid: str  # base64 encoded
    password: str  # base64 encoded or encrypted (WifiX)

    bssid: str
    channel: int
    signal: int
    encryption: int
    cipher: int


type WifiList = list[Wifi]
