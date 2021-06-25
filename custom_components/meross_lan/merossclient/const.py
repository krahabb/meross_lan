
from typing import OrderedDict


METHOD_PUSH = "PUSH"
METHOD_GET = "GET"
METHOD_GETACK = "GETACK"
METHOD_SET = "SET"
METHOD_SETACK = "SETACK"
METHOD_ERROR = "ERROR"

NS_APPLIANCE_SYSTEM_ALL = "Appliance.System.All"
NS_APPLIANCE_SYSTEM_ABILITY = "Appliance.System.Ability"
NS_APPLIANCE_SYSTEM_CLOCK = "Appliance.System.Clock"
NS_APPLIANCE_SYSTEM_REPORT = "Appliance.System.Report"
NS_APPLIANCE_SYSTEM_ONLINE = "Appliance.System.Online"
NS_APPLIANCE_SYSTEM_DEBUG = "Appliance.System.Debug"
NS_APPLIANCE_SYSTEM_TIME = "Appliance.System.Time"
NS_APPLIANCE_CONFIG_TRACE = "Appliance.Config.Trace"
NS_APPLIANCE_CONFIG_WIFILIST = "Appliance.Config.WifiList"
NS_APPLIANCE_CONTROL_TOGGLE = "Appliance.Control.Toggle"
NS_APPLIANCE_CONTROL_TOGGLEX = "Appliance.Control.ToggleX"
NS_APPLIANCE_CONTROL_TRIGGER = "Appliance.Control.Trigger"
NS_APPLIANCE_CONTROL_TRIGGERX = "Appliance.Control.TriggerX"
NS_APPLIANCE_CONTROL_CONSUMPTIONCONFIG = "Appliance.Control.ConsumptionConfig"
NS_APPLIANCE_CONTROL_CONSUMPTIONX = "Appliance.Control.ConsumptionX"
NS_APPLIANCE_CONTROL_ELECTRICITY = "Appliance.Control.Electricity"
# Light Abilities
NS_APPLIANCE_CONTROL_LIGHT = "Appliance.Control.Light"
# Humidifier abilities
NS_APPLIANCE_SYSTEM_DND = "Appliance.System.DNDMode"
NS_APPLIANCE_CONTROL_SPRAY = "Appliance.Control.Spray"
# Garage door opener
NS_APPLIANCE_GARAGEDOOR_STATE = "Appliance.GarageDoor.State"
# Roller shutter
NS_APPLIANCE_ROLLERSHUTTER_STATE = 'Appliance.RollerShutter.State'
NS_APPLIANCE_ROLLERSHUTTER_POSITION = 'Appliance.RollerShutter.Position'
# Hub
NS_APPLIANCE_DIGEST_HUB = 'Appliance.Digest.Hub'
NS_APPLIANCE_HUB_SUBDEVICELIST = 'Appliance.Hub.SubdeviceList'
NS_APPLIANCE_HUB_EXCEPTION = 'Appliance.Hub.Exception'
NS_APPLIANCE_HUB_BATTERY = 'Appliance.Hub.Battery'
NS_APPLIANCE_HUB_TOGGLEX = 'Appliance.Hub.ToggleX'
NS_APPLIANCE_HUB_ONLINE = 'Appliance.Hub.Online'
#
NS_APPLIANCE_HUB_SENSOR_ALL = 'Appliance.Hub.Sensor.All'
NS_APPLIANCE_HUB_SENSOR_TEMPHUM = 'Appliance.Hub.Sensor.TempHum'
NS_APPLIANCE_HUB_SENSOR_ALERT = 'Appliance.Hub.Sensor.Alert'
# MTS100
NS_APPLIANCE_HUB_MTS100_ALL = 'Appliance.Hub.Mts100.All'
NS_APPLIANCE_HUB_MTS100_TEMPERATURE = 'Appliance.Hub.Mts100.Temperature'
NS_APPLIANCE_HUB_MTS100_MODE = 'Appliance.Hub.Mts100.Mode'


# misc keys for json payloads
KEY_HEADER = 'header'
KEY_MESSAGEID = 'messageId'
KEY_NAMESPACE = 'namespace'
KEY_METHOD = 'method'
KEY_PAYLOADVERSION = 'payloadVersion'
KEY_FROM = 'from'
KEY_TIMESTAMP = 'timestamp'
KEY_TIMESTAMPMS = 'timestampMs'
KEY_SIGN = 'sign'
KEY_PAYLOAD = 'payload'
KEY_ERROR = 'error'
KEY_CODE = 'code'
KEY_ALL = 'all'
KEY_SYSTEM = 'system'
KEY_HARDWARE = 'hardware'
KEY_TYPE = 'type'
KEY_VERSION = 'version'
KEY_UUID = 'uuid'
KEY_MACADDRESS = 'macAddress'
KEY_FIRMWARE = 'firmware'
KEY_INNERIP = 'innerIp'
KEY_CONTROL = 'control'
KEY_DIGEST = 'digest'
KEY_ABILITY = 'ability'
KEY_ONLINE = 'online'
KEY_TIME = 'time'
KEY_TIMEZONE = 'timezone'
KEY_STATUS = 'status'
KEY_CHANNEL = 'channel'
KEY_TOGGLE = 'toggle'
KEY_TOGGLEX = 'togglex'
KEY_ONOFF = 'onoff'
KEY_LIGHT = 'light'
KEY_CAPACITY = 'capacity'
KEY_RGB = 'rgb'
KEY_LUMINANCE = 'luminance'
KEY_TEMPERATURE = 'temperature'
KEY_HUB = 'hub'
KEY_BATTERY = 'battery'
KEY_VALUE = 'value'
KEY_HUBID = 'hubId'
KEY_SUBDEVICE = 'subdevice'
KEY_ID = 'id'
KEY_LATEST = 'latest'
KEY_TEMPHUM = 'tempHum'
KEY_LATESTTEMPERATURE = 'latestTemperature'
KEY_LATESTHUMIDITY = 'latestHumidity'
KEY_ELECTRICITY = 'electricity'
KEY_POWER = 'power'
KEY_CURRENT = 'current'
KEY_VOLTAGE = 'voltage'
KEY_CONSUMPTIONX = 'consumptionx'
KEY_DATE = 'date'
KEY_GARAGEDOOR = 'garageDoor'
KEY_STATE = 'state'
KEY_POSITION = 'position'
KEY_OPEN = 'open'
KEY_MODE = 'mode'
KEY_ROOM = 'room'
KEY_CURRENTSET = 'currentSet'
KEY_MIN = 'min'
KEY_MAX = 'max'
KEY_CUSTOM = 'custom'
KEY_COMFORT = 'comfort'
KEY_ECONOMY = 'economy'
KEY_HEATING = 'heating'
KEY_AWAY = 'away'
KEY_OPENWINDOW = 'openWindow'

# online status
STATUS_UNKNOWN = -1
STATUS_NOTONLINE = 0
STATUS_ONLINE = 1
STATUS_OFFLINE = 2
STATUS_UPGRADING = 3

# well known device types
TYPE_UNKNOWN = 'unknown'
TYPE_MSH300 = 'msh300' # WiFi Hub
TYPE_MS100 = 'ms100' # Smart temp/humidity sensor over Hub
TYPE_MTS100 = 'mts100'
TYPE_MTS100V3 = 'mts100v3'
TYPE_MSS310 = 'mss310' # smart plug with energy meter
TYPE_MSL100 = 'msl100' # smart bulb
TYPE_MSL120 = 'msl120' # smart bulb with color/temp

# common device type classes
CLASS_MSH = 'msh'
CLASS_MSS = 'mss'
CLASS_MSL = 'msl'
CLASS_MTS = 'mts'
TYPE_NAME_MAP = OrderedDict()
TYPE_NAME_MAP[TYPE_MSL120] = "Smart RGB Bulb"
TYPE_NAME_MAP[TYPE_MSL100] = "Smart Bulb"
TYPE_NAME_MAP[CLASS_MSL] = "Smart Light"
TYPE_NAME_MAP[CLASS_MSH] = "Smart Hub"
TYPE_NAME_MAP[TYPE_MSS310] = "Smart Plug"
TYPE_NAME_MAP[CLASS_MSS] = "Smart Switch"
TYPE_NAME_MAP[CLASS_MTS] = "Smart Thermostat"
TYPE_NAME_MAP[TYPE_MS100] = "Smart Temp/Humidity Sensor"
"""
    GP constant strings
"""
MANUFACTURER = "Meross"