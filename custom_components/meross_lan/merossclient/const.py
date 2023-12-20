import collections

# MQTT topics
TOPIC_DISCOVERY = "/appliance/+/publish"
TOPIC_REQUEST = "/appliance/{}/subscribe"
TOPIC_RESPONSE = "/appliance/{}/publish"

METHOD_PUSH = "PUSH"
METHOD_GET = "GET"
METHOD_GETACK = "GETACK"
METHOD_SET = "SET"
METHOD_SETACK = "SETACK"
METHOD_ERROR = "ERROR"
# map acknowledge to relative command method
METHOD_ACK_MAP = {
    METHOD_GET: METHOD_GETACK,
    METHOD_SET: METHOD_SETACK,
}
# list methods usually carrying parsable state
METHOD_PARSE_SET = (
    METHOD_GETACK,
    METHOD_PUSH,
)

NS_APPLIANCE_SYSTEM_ALL = "Appliance.System.All"
NS_APPLIANCE_SYSTEM_ABILITY = "Appliance.System.Ability"
NS_APPLIANCE_SYSTEM_HARDWARE = "Appliance.System.Hardware"
NS_APPLIANCE_SYSTEM_FIRMWARE = "Appliance.System.Firmware"
NS_APPLIANCE_SYSTEM_CLOCK = "Appliance.System.Clock"
NS_APPLIANCE_SYSTEM_REPORT = "Appliance.System.Report"
NS_APPLIANCE_SYSTEM_ONLINE = "Appliance.System.Online"
NS_APPLIANCE_SYSTEM_DEBUG = "Appliance.System.Debug"
NS_APPLIANCE_SYSTEM_TIME = "Appliance.System.Time"
NS_APPLIANCE_SYSTEM_DNDMODE = "Appliance.System.DNDMode"
NS_APPLIANCE_SYSTEM_RUNTIME = "Appliance.System.Runtime"
NS_APPLIANCE_SYSTEM_POSITION = "Appliance.System.Position"
NS_APPLIANCE_CONFIG_KEY = "Appliance.Config.Key"
NS_APPLIANCE_CONFIG_WIFI = "Appliance.Config.Wifi"
NS_APPLIANCE_CONFIG_WIFIX = "Appliance.Config.WifiX"
NS_APPLIANCE_CONFIG_WIFILIST = "Appliance.Config.WifiList"
NS_APPLIANCE_CONFIG_TRACE = "Appliance.Config.Trace"
NS_APPLIANCE_CONFIG_INFO = "Appliance.Config.Info"
NS_APPLIANCE_CONFIG_OVERTEMP = "Appliance.Config.OverTemp"
NS_APPLIANCE_DIGEST_TRIGGERX = "Appliance.Digest.TriggerX"
NS_APPLIANCE_DIGEST_TIMERX = "Appliance.Digest.TimerX"
NS_APPLIANCE_CONTROL_MULTIPLE = "Appliance.Control.Multiple"
NS_APPLIANCE_CONTROL_BIND = "Appliance.Control.Bind"
NS_APPLIANCE_CONTROL_UNBIND = "Appliance.Control.Unbind"
NS_APPLIANCE_CONTROL_UPGRADE = "Appliance.Control.Upgrade"
NS_APPLIANCE_CONTROL_TOGGLE = "Appliance.Control.Toggle"
NS_APPLIANCE_CONTROL_TOGGLEX = "Appliance.Control.ToggleX"
NS_APPLIANCE_CONTROL_TRIGGER = "Appliance.Control.Trigger"
NS_APPLIANCE_CONTROL_TRIGGERX = "Appliance.Control.TriggerX"
NS_APPLIANCE_CONTROL_TIMERX = "Appliance.Control.TimerX"
NS_APPLIANCE_CONTROL_CONSUMPTIONCONFIG = "Appliance.Control.ConsumptionConfig"
NS_APPLIANCE_CONTROL_CONSUMPTIONX = "Appliance.Control.ConsumptionX"
NS_APPLIANCE_CONTROL_CONSUMPTIONH = "Appliance.Control.ConsumptionH"
NS_APPLIANCE_CONTROL_ELECTRICITY = "Appliance.Control.Electricity"
NS_APPLIANCE_CONTROL_OVERTEMP = "Appliance.Control.OverTemp"
# Light Abilities
NS_APPLIANCE_CONTROL_LIGHT = "Appliance.Control.Light"
NS_APPLIANCE_CONTROL_LIGHT_EFFECT = "Appliance.Control.Light.Effect"
# Humidifier abilities
NS_APPLIANCE_CONTROL_SPRAY = "Appliance.Control.Spray"
# Unknown abilities
NS_APPLIANCE_CONTROL_PHYSICALLOCK = "Appliance.Control.PhysicalLock"

# Garage door opener
NS_APPLIANCE_GARAGEDOOR_STATE = "Appliance.GarageDoor.State"
NS_APPLIANCE_GARAGEDOOR_CONFIG = "Appliance.GarageDoor.Config"
NS_APPLIANCE_GARAGEDOOR_MULTIPLECONFIG = "Appliance.GarageDoor.MultipleConfig"
# Roller shutter
NS_APPLIANCE_ROLLERSHUTTER_STATE = "Appliance.RollerShutter.State"
NS_APPLIANCE_ROLLERSHUTTER_POSITION = "Appliance.RollerShutter.Position"
NS_APPLIANCE_ROLLERSHUTTER_CONFIG = "Appliance.RollerShutter.Config"
# Hub
NS_APPLIANCE_DIGEST_HUB = "Appliance.Digest.Hub"
NS_APPLIANCE_HUB_SUBDEVICELIST = "Appliance.Hub.SubdeviceList"
NS_APPLIANCE_HUB_REPORT = "Appliance.Hub.Report"
NS_APPLIANCE_HUB_EXCEPTION = "Appliance.Hub.Exception"
NS_APPLIANCE_HUB_BATTERY = "Appliance.Hub.Battery"
NS_APPLIANCE_HUB_TOGGLEX = "Appliance.Hub.ToggleX"
NS_APPLIANCE_HUB_ONLINE = "Appliance.Hub.Online"
NS_APPLIANCE_HUB_PAIRSUBDEV = "Appliance.Hub.PairSubDev"
NS_APPLIANCE_HUB_SENSITIVITY = "Appliance.Hub.Sensitivity"
# miscellaneous
NS_APPLIANCE_HUB_SUBDEVICE_MOTORADJUST = "Appliance.Hub.SubDevice.MotorAdjust"
NS_APPLIANCE_HUB_SUBDEVICE_BEEP = "Appliance.Hub.SubDevice.Beep"
# MS100 and other sensors
NS_APPLIANCE_HUB_SENSOR_ALL = "Appliance.Hub.Sensor.All"
NS_APPLIANCE_HUB_SENSOR_TEMPHUM = "Appliance.Hub.Sensor.TempHum"
NS_APPLIANCE_HUB_SENSOR_ALERT = "Appliance.Hub.Sensor.Alert"
NS_APPLIANCE_HUB_SENSOR_ADJUST = "Appliance.Hub.Sensor.Adjust"
NS_APPLIANCE_HUB_SENSOR_LATEST = "Appliance.Hub.Sensor.Latest"
NS_APPLIANCE_HUB_SENSOR_SMOKE = "Appliance.Hub.Sensor.Smoke"
NS_APPLIANCE_HUB_SENSOR_WATERLEAK = "Appliance.Hub.Sensor.WaterLeak"
NS_APPLIANCE_HUB_SENSOR_MOTION = "Appliance.Hub.Sensor.Motion"
NS_APPLIANCE_HUB_SENSOR_DOORWINDOW = "Appliance.Hub.Sensor.DoorWindow"
# MTS100
NS_APPLIANCE_HUB_MTS100_ALL = "Appliance.Hub.Mts100.All"
NS_APPLIANCE_HUB_MTS100_TEMPERATURE = "Appliance.Hub.Mts100.Temperature"
NS_APPLIANCE_HUB_MTS100_MODE = "Appliance.Hub.Mts100.Mode"
NS_APPLIANCE_HUB_MTS100_ADJUST = "Appliance.Hub.Mts100.Adjust"
NS_APPLIANCE_HUB_MTS100_SCHEDULE = "Appliance.Hub.Mts100.Schedule"
NS_APPLIANCE_HUB_MTS100_SCHEDULEB = "Appliance.Hub.Mts100.ScheduleB"
NS_APPLIANCE_HUB_MTS100_TIMESYNC = "Appliance.Hub.Mts100.TimeSync"
NS_APPLIANCE_HUB_MTS100_SUPERCTL = "Appliance.Hub.Mts100.SuperCtl"
# Smart cherub HP110A
NS_APPLIANCE_MCU_HP110_FIRMWARE = "Appliance.Mcu.Hp110.Firmware"
NS_APPLIANCE_MCU_HP110_FAVORITE = "Appliance.Mcu.Hp110.Favorite"
NS_APPLIANCE_MCU_HP110_PREVIEW = "Appliance.Mcu.Hp110.Preview"
NS_APPLIANCE_MCU_HP110_LOCK = "Appliance.Mcu.Hp110.Lock"
NS_APPLIANCE_CONTROL_MP3 = "Appliance.Control.Mp3"
# MTS200 smart thermostat
NS_APPLIANCE_CONTROL_THERMOSTAT_CALIBRATION = "Appliance.Control.Thermostat.Calibration"
NS_APPLIANCE_CONTROL_THERMOSTAT_DEADZONE = "Appliance.Control.Thermostat.DeadZone"
NS_APPLIANCE_CONTROL_THERMOSTAT_FROST = "Appliance.Control.Thermostat.Frost"
NS_APPLIANCE_CONTROL_THERMOSTAT_HOLDACTION = "Appliance.Control.Thermostat.HoldAction"
NS_APPLIANCE_CONTROL_THERMOSTAT_MODE = "Appliance.Control.Thermostat.Mode"
NS_APPLIANCE_CONTROL_THERMOSTAT_OVERHEAT = "Appliance.Control.Thermostat.Overheat"
NS_APPLIANCE_CONTROL_THERMOSTAT_SCHEDULE = "Appliance.Control.Thermostat.Schedule"
NS_APPLIANCE_CONTROL_THERMOSTAT_SENSOR = "Appliance.Control.Thermostat.Sensor"
NS_APPLIANCE_CONTROL_THERMOSTAT_SUMMERMODE = "Appliance.Control.Thermostat.SummerMode"
NS_APPLIANCE_CONTROL_THERMOSTAT_WINDOWOPENED = (
    "Appliance.Control.Thermostat.WindowOpened"
)
# MOD100-MOD150 diffuser
NS_APPLIANCE_CONTROL_DIFFUSER_SPRAY = "Appliance.Control.Diffuser.Spray"
NS_APPLIANCE_CONTROL_DIFFUSER_LIGHT = "Appliance.Control.Diffuser.Light"
NS_APPLIANCE_CONTROL_DIFFUSER_SENSOR = "Appliance.Control.Diffuser.Sensor"
# screen brigtness (actually seen on MTS200)
NS_APPLIANCE_CONTROL_SCREEN_BRIGHTNESS = "Appliance.Control.Screen.Brightness"

NS_APPLIANCE_MCU_FIRMWARE = "Appliance.Mcu.Firmware"
NS_APPLIANCE_MCU_UPGRADE = "Appliance.Mcu.Upgrade"

# misc keys for json payloads
KEY_HEADER = "header"
KEY_MESSAGEID = "messageId"
KEY_NAMESPACE = "namespace"
KEY_METHOD = "method"
KEY_PAYLOADVERSION = "payloadVersion"
KEY_FROM = "from"
KEY_TRIGGERSRC = "triggerSrc"
KEY_TIMESTAMP = "timestamp"
KEY_TIMESTAMPMS = "timestampMs"
KEY_SIGN = "sign"
KEY_PAYLOAD = "payload"
KEY_ERROR = "error"
KEY_CODE = "code"
KEY_ALL = "all"
KEY_SYSTEM = "system"
KEY_HARDWARE = "hardware"
KEY_TYPE = "type"
KEY_SUBTYPE = "subType"
KEY_DESCRIPTION = "description"
KEY_VERSION = "version"
KEY_UUID = "uuid"
KEY_MACADDRESS = "macAddress"
KEY_FIRMWARE = "firmware"
KEY_WIFIMAC = "wifiMac"
KEY_INNERIP = "innerIp"
KEY_SERVER = "server"
KEY_PORT = "port"
KEY_SECONDSERVER = "secondServer"
KEY_SECONDPORT = "secondPort"
KEY_USERID = "userId"
KEY_CONTROL = "control"
KEY_DIGEST = "digest"
KEY_ABILITY = "ability"
KEY_ONLINE = "online"
KEY_TRIGGERX = "triggerx"
KEY_TIMERX = "timerx"
KEY_CLOCK = "clock"
KEY_TIME = "time"
KEY_TIMEZONE = "timezone"
KEY_TIMERULE = "timeRule"
KEY_STATUS = "status"
KEY_INFO = "info"
KEY_HOMEKIT = "homekit"
KEY_MODEL = "model"
KEY_SN = "sn"
KEY_CATEGORY = "category"
KEY_SETUPID = "setupId"
KEY_SETUPCODE = "setupCode"
KEY_TOKEN = "token"
KEY_RUNTIME = "runtime"
KEY_SIGNAL = "signal"
KEY_LMTIME = "lmTime"
KEY_LMTIME_ = "lmtime"
KEY_CHANNEL = "channel"
KEY_TOGGLE = "toggle"
KEY_TOGGLEX = "togglex"
KEY_ONOFF = "onoff"
KEY_LIGHT = "light"
KEY_EFFECT = "effect"
KEY_EFFECTNAME = "effectName"
KEY_ID_ = "Id"
KEY_CAPACITY = "capacity"
KEY_RGB = "rgb"
KEY_LUMINANCE = "luminance"
KEY_TEMPERATURE = "temperature"
KEY_HUMIDITY = "humidity"
KEY_SPRAY = "spray"
KEY_HUB = "hub"
KEY_BATTERY = "battery"
KEY_VALUE = "value"
KEY_HUBID = "hubId"
KEY_SUBDEVICE = "subdevice"
KEY_SUBDEVICELIST = "subdeviceList"
KEY_ID = "id"
KEY_LASTACTIVETIME = "lastActiveTime"
KEY_SYNCEDTIME = "syncedTime"
KEY_LATESTSAMPLETIME = "latestSampleTime"
KEY_LATEST = "latest"
KEY_TEMPHUM = "tempHum"
KEY_LATESTTEMPERATURE = "latestTemperature"
KEY_LATESTHUMIDITY = "latestHumidity"
KEY_SMOKEALARM = "smokeAlarm"
KEY_INTERCONN = "interConn"
KEY_DOORWINDOW = "doorWindow"
KEY_WATERLEAK = "waterLeak"
KEY_LATESTWATERLEAK = "latestWaterLeak"
KEY_SCHEDULE = "schedule"
KEY_SCHEDULEBMODE = "scheduleBMode"
KEY_SCHEDULEUNITTIME = "scheduleUnitTime"
KEY_ELECTRICITY = "electricity"
KEY_POWER = "power"
KEY_CURRENT = "current"
KEY_VOLTAGE = "voltage"
KEY_CONSUMPTIONX = "consumptionx"
KEY_CONSUMPTIONH = "consumptionH"
KEY_CONSUMPTIONCONFIG = "consumptionconfig"
KEY_OVERTEMP = "overTemp"
KEY_ENABLE = "enable"
KEY_DATE = "date"
KEY_GARAGEDOOR = "garageDoor"
KEY_STATE = "state"
KEY_POSITION = "position"
KEY_CONFIG = "config"
KEY_SIGNALOPEN = "signalOpen"
KEY_SIGNALCLOSE = "signalClose"
KEY_SIGNALDURATION = "signalDuration"
KEY_BUZZERENABLE = "buzzerEnable"
KEY_DOORENABLE = "doorEnable"
KEY_DOOROPENDURATION = "doorOpenDuration"
KEY_DOORCLOSEDURATION = "doorCloseDuration"
KEY_OPEN = "open"
KEY_EXECUTE = "execute"
KEY_MODE = "mode"
KEY_ROOM = "room"
KEY_CURRENTSET = "currentSet"
KEY_MIN = "min"
KEY_MAX = "max"
KEY_CUSTOM = "custom"
KEY_COMFORT = "comfort"
KEY_ECONOMY = "economy"
KEY_HEATING = "heating"
KEY_AWAY = "away"
KEY_OPENWINDOW = "openWindow"
KEY_THERMOSTAT = "thermostat"
KEY_CURRENTTEMP = "currentTemp"
KEY_HEATTEMP = "heatTemp"
KEY_COOLTEMP = "coolTemp"
KEY_ECOTEMP = "ecoTemp"
KEY_MANUALTEMP = "manualTemp"
KEY_TARGETTEMP = "targetTemp"
KEY_WINDOWOPENED = "windowOpened"
KEY_CALIBRATION = "calibration"
KEY_DEADZONE = "deadZone"
KEY_FROST = "frost"
KEY_OVERHEAT = "overheat"
KEY_HOLDACTION = "holdAction"
KEY_SENSOR = "sensor"
KEY_SUMMERMODE = "summerMode"
KEY_WARNING = "warning"
KEY_DIFFUSER = "diffuser"
KEY_DNDMODE = "DNDMode"
KEY_ADJUST = "adjust"
KEY_BRIGHTNESS = "brightness"
KEY_OPERATION = "operation"
KEY_STANDBY = "standby"
KEY_MP3 = "mp3"
KEY_SONG = "song"
KEY_MUTE = "mute"
KEY_VOLUME = "volume"
KEY_DEBUG = "debug"
KEY_NETWORK = "network"
KEY_SSID = "ssid"
KEY_GATEWAYMAC = "gatewayMac"
KEY_CLOUD = "cloud"
KEY_ACTIVESERVER = "activeServer"
KEY_MAINSERVER = "mainServer"
KEY_MAINPORT = "mainPort"
KEY_SYSCONNECTTIME = "sysConnectTime"
KEY_SYSONLINETIME = "sysOnlineTime"
KEY_SYSDISCONNECTCOUNT = "sysDisconnectCount"
KEY_NONCE = "nonce"
KEY_PARAMS = "params"
KEY_APISTATUS = "apiStatus"
KEY_SYSSTATUS = "sysStatus"
KEY_DATA = "data"
KEY_KEY = "key"
KEY_EMAIL = "email"
KEY_PASSWORD = "password"
KEY_USERID_ = "userid"
KEY_DEVNAME = "devName"
KEY_DEVICETYPE = "deviceType"
KEY_CLUSTER = "cluster"
KEY_DOMAIN = "domain"
KEY_RESERVEDDOMAIN = "reservedDomain"
KEY_SUBDEVICEID = "subDeviceId"
KEY_SUBDEVICENAME = "subDeviceName"


# 'well-know' syntax for METHOD_GET
PAYLOAD_GET = {
    NS_APPLIANCE_SYSTEM_ALL: {KEY_ALL: {}},
    NS_APPLIANCE_SYSTEM_ABILITY: {KEY_ABILITY: {}},
    NS_APPLIANCE_SYSTEM_DNDMODE: {KEY_DNDMODE: {}},
    NS_APPLIANCE_DIGEST_TRIGGERX: {KEY_DIGEST: []},
    NS_APPLIANCE_DIGEST_TIMERX: {KEY_DIGEST: []},
    NS_APPLIANCE_CONTROL_TOGGLEX: {KEY_TOGGLEX: []},
    NS_APPLIANCE_CONTROL_TOGGLE: {KEY_TOGGLE: []},
    NS_APPLIANCE_CONTROL_CONSUMPTIONX: {KEY_CONSUMPTIONX: []},
    NS_APPLIANCE_CONTROL_ELECTRICITY: {KEY_ELECTRICITY: {}},
    NS_APPLIANCE_CONTROL_TRIGGERX: {KEY_TRIGGERX: {}},
    NS_APPLIANCE_CONTROL_TIMERX: {KEY_TIMERX: {}},
    NS_APPLIANCE_CONTROL_LIGHT: {KEY_LIGHT: {}},
    NS_APPLIANCE_CONTROL_LIGHT_EFFECT: {KEY_EFFECT: []},
    NS_APPLIANCE_CONTROL_SPRAY: {KEY_SPRAY: {}},
    NS_APPLIANCE_CONTROL_MP3: {KEY_MP3: {}},
    NS_APPLIANCE_CONTROL_OVERTEMP: {KEY_OVERTEMP: [{KEY_CHANNEL: 0}]},
    NS_APPLIANCE_ROLLERSHUTTER_POSITION: {KEY_POSITION: []},
    NS_APPLIANCE_ROLLERSHUTTER_STATE: {KEY_STATE: []},
    NS_APPLIANCE_ROLLERSHUTTER_CONFIG: {KEY_CONFIG: []},
    NS_APPLIANCE_GARAGEDOOR_CONFIG: {KEY_CONFIG: {}},
    NS_APPLIANCE_GARAGEDOOR_MULTIPLECONFIG: {KEY_CONFIG: [{KEY_CHANNEL: 0}]},
    NS_APPLIANCE_HUB_BATTERY: {KEY_BATTERY: []},
    NS_APPLIANCE_HUB_SENSOR_ALL: {KEY_ALL: []},
    NS_APPLIANCE_HUB_SENSOR_SMOKE: {
        KEY_SMOKEALARM: []
    },  # guessing: 'smoke' is wrong for sure
    NS_APPLIANCE_HUB_MTS100_ALL: {KEY_ALL: []},
    NS_APPLIANCE_HUB_MTS100_SCHEDULEB: {KEY_SCHEDULE: []},
    NS_APPLIANCE_HUB_SUBDEVICE_MOTORADJUST: {
        KEY_ADJUST: []
    },  # unconfirmed but 'motoradjust' is wrong for sure
    NS_APPLIANCE_CONTROL_THERMOSTAT_CALIBRATION: {KEY_CALIBRATION: [{KEY_CHANNEL: 0}]},
    NS_APPLIANCE_CONTROL_THERMOSTAT_DEADZONE: {KEY_DEADZONE: [{KEY_CHANNEL: 0}]},
    NS_APPLIANCE_CONTROL_THERMOSTAT_FROST: {KEY_FROST: [{KEY_CHANNEL: 0}]},
    NS_APPLIANCE_CONTROL_THERMOSTAT_HOLDACTION: {KEY_HOLDACTION: [{KEY_CHANNEL: 0}]},
    NS_APPLIANCE_CONTROL_THERMOSTAT_MODE: {KEY_MODE: []},
    NS_APPLIANCE_CONTROL_THERMOSTAT_OVERHEAT: {KEY_OVERHEAT: [{KEY_CHANNEL: 0}]},
    NS_APPLIANCE_CONTROL_THERMOSTAT_SCHEDULE: {KEY_SCHEDULE: [{KEY_CHANNEL: 0}]},
    NS_APPLIANCE_CONTROL_THERMOSTAT_SENSOR: {KEY_SENSOR: [{KEY_CHANNEL: 0}]},
    NS_APPLIANCE_CONTROL_THERMOSTAT_SUMMERMODE: {KEY_SUMMERMODE: [{KEY_CHANNEL: 0}]},
    NS_APPLIANCE_CONTROL_THERMOSTAT_WINDOWOPENED: {
        KEY_WINDOWOPENED: [{KEY_CHANNEL: 0}]
    },
    NS_APPLIANCE_CONTROL_SCREEN_BRIGHTNESS: {KEY_BRIGHTNESS: [{KEY_CHANNEL: 0}]},
    NS_APPLIANCE_CONTROL_DIFFUSER_SENSOR: {KEY_SENSOR: {}},
}
# error codes as reported by Meross device protocol
ERROR_INVALIDKEY = 5001

# online status
STATUS_UNKNOWN = -1
STATUS_NOTONLINE = 0
STATUS_ONLINE = 1
STATUS_OFFLINE = 2
STATUS_UPGRADING = 3

# light bulb capacity enums
LIGHT_CAPACITY_RGB = 1
LIGHT_CAPACITY_TEMPERATURE = 2
LIGHT_CAPACITY_LUMINANCE = 4
LIGHT_CAPACITY_RGB_LUMINANCE = 5
LIGHT_CAPACITY_TEMPERATURE_LUMINANCE = 6
LIGHT_CAPACITY_EFFECT = 8  # not tested but looks like msl320 carries this flag

# spray mode enums
SPRAY_MODE_OFF = 0
SPRAY_MODE_CONTINUOUS = 1
SPRAY_MODE_INTERMITTENT = 2

# rollershutter states
ROLLERSHUTTER_STATE_IDLE = 0
ROLLERSHUTTER_STATE_OPENING = 1
ROLLERSHUTTER_STATE_CLOSING = 2

MTS_TEMP_SCALE = 10  # native mts temperatures expressed in tenth of °C

# mts100 (and the likes..) valves mode
MTS100_MODE_CUSTOM = 0
MTS100_MODE_HEAT = 1
MTS100_MODE_COOL = 2
MTS100_MODE_ECO = 4
MTS100_MODE_AUTO = 3

# I don't have an MTS200 to test so these are inferred from a user trace
MTS200_MODE_HEAT = 0
MTS200_MODE_COOL = 1
MTS200_MODE_ECO = 2
MTS200_MODE_AUTO = 3
MTS200_MODE_CUSTOM = 4
# looks like with 'summerMode' disabled (i.e. in winter) the MTS reports '1'
MTS200_SUMMERMODE_HEAT = 1
MTS200_SUMMERMODE_COOL = 0

# MTS200 external sensor status (overheat protection)
MTS200_OVERHEAT_WARNING_OK = 0
MTS200_OVERHEAT_WARNING_OVERHEATING = 1
MTS200_OVERHEAT_WARNING_NOTCONNECTED = 2
MTS200_OVERHEAT_WARNING_MAP = {
    MTS200_OVERHEAT_WARNING_OK: "ok",
    MTS200_OVERHEAT_WARNING_OVERHEATING: "overheating",
    MTS200_OVERHEAT_WARNING_NOTCONNECTED: "disconnected",
}

# diffuser mode enums
DIFFUSER_SPRAY_MODE_OFF = 2  # or 255 ? or 'any' ?
DIFFUSER_SPRAY_MODE_ECO = 0
DIFFUSER_SPRAY_MODE_FULL = 1
DIFFUSER_LIGHT_MODE_RAINBOW = 0  # color modes taken from 'homebridge-meross' plugin
DIFFUSER_LIGHT_MODE_COLOR = 1
DIFFUSER_LIGHT_MODE_TEMPERATURE = 2
DIFFUSER_LIGHT_EFFECT_MAP = {
    DIFFUSER_LIGHT_MODE_RAINBOW: "Rainbow",
    DIFFUSER_LIGHT_MODE_COLOR: "Color",
    DIFFUSER_LIGHT_MODE_TEMPERATURE: "Temperature",
}

# cherub machine
HP110A_LIGHT_EFFECT_MAP = {
    0: "Color",
    1: "Scene 1",
    2: "Scene 2",
    3: "Scene 3",
    4: "Scene 4",
}
HP110A_MP3_SONG_MIN = 1
HP110A_MP3_SONG_MAX = 11
HP110A_MP3_SONG_MAP = {
    1: "Cicada Chirping",
    2: "Rain Sound",
    3: "Ripple Sound",
    4: "Birdsong",
    5: "Lullaby",
    6: "Fan Sound",
    7: "Crystal Ball",
    8: "Music Box",
    9: "White Noise",
    10: "Thunder",
    11: "Ocean Wave",
}

# well known device types and classes
# when registering type names put the CLASS name
# after the corresponding (specialized) TYPE name
# so we'll correctly find a defined TYPE name
# by best effort matching on the iteration
# if no type defined the class definition can
# provide a general device description
# see how TYPE_NAME_MAP is used in code
TYPE_UNKNOWN = "unknown"
TYPE_NAME_MAP = collections.OrderedDict()

CLASS_MSH = "msh"
TYPE_MSH300 = "msh300"  # WiFi Hub
TYPE_NAME_MAP[CLASS_MSH] = "Smart Hub"

CLASS_MSS = "mss"
TYPE_MSS310 = "mss310"  # smart plug with energy meter
TYPE_NAME_MAP[TYPE_MSS310] = "Smart Plug"
TYPE_MSS560 = "mss560"
TYPE_NAME_MAP[TYPE_MSS560] = "Smart Dimmer Switch"
TYPE_MSS570 = "mss570"
TYPE_NAME_MAP[TYPE_MSS570] = TYPE_NAME_MAP[TYPE_MSS560]
TYPE_NAME_MAP[CLASS_MSS] = "Smart Switch"

CLASS_MSL = "msl"
TYPE_MSL100 = "msl100"  # smart bulb
TYPE_NAME_MAP[TYPE_MSL100] = "Smart Bulb"
TYPE_MSL120 = "msl120"  # smart bulb with color/temp
TYPE_NAME_MAP[TYPE_MSL120] = "Smart RGB Bulb"
TYPE_NAME_MAP[CLASS_MSL] = "Smart Light"

CLASS_MTS = "mts"
TYPE_MTS100 = "mts100"  # Smart thermostat over hub
TYPE_MTS100V3 = "mts100v3"  # Smart thermostat over hub
TYPE_MTS150 = "mts150"  # Smart thermostat over hub
TYPE_MTS200 = "mts200"  # Smart thermostat over wifi
TYPE_NAME_MAP[CLASS_MTS] = "Smart Thermostat"

CLASS_MOD = "mod"
TYPE_MOD100 = "mod100"  # smart humidifier
TYPE_MOD150 = (
    "mod150"  # smart humidifier (looks the same as mod100...maybe HK version tho)
)
TYPE_NAME_MAP[CLASS_MOD] = "Smart Humidifier"

CLASS_MSG = "msg"
TYPE_NAME_MAP[CLASS_MSG] = "Smart Garage Door"

CLASS_MRS = "mrs"
TYPE_NAME_MAP[CLASS_MRS] = "Smart Roller Shutter"

# do not register class 'ms' since it is rather
# unusual naming and could issue collissions with mss or msl
# just set the known type
TYPE_MS100 = "ms100"  # Smart temp/humidity sensor over Hub
TYPE_NAME_MAP[TYPE_MS100] = "Smart Temp/Humidity Sensor"

TYPE_HP110A = "hp110"
TYPE_NAME_MAP[TYPE_HP110A] = "Smart Cherub Baby Machine"

TYPE_GS559 = "gs559"
TYPE_NAME_MAP[TYPE_GS559] = "Smart Smoke Alarm"

TYPE_MS200 = "ms200"
TYPE_NAME_MAP[TYPE_MS200] = "Smart Door/Window Sensor"

TYPE_MS400 = "ms400"
TYPE_NAME_MAP[TYPE_MS400] = "Smart Water Leak Sensor"

"""
    GP constants
"""
MANUFACTURER = "Meross"
MEROSS_MACADDRESS = "48:e1:e9:xx:xx:xx"
MQTT_DEFAULT_PORT = 443
