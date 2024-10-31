"""
    static constants symbols for Meross protocol symbols/semantics
"""

import collections
import re

# MQTT topics
TOPIC_DISCOVERY = "/appliance/+/publish"
TOPIC_REQUEST = "/appliance/{}/subscribe"
TOPIC_RESPONSE = "/appliance/{}/publish"

RE_PATTERN_UUID = re.compile(r"(^|[^a-fA-F0-9])([a-fA-F0-9]{32})($|[^a-fA-F0-9])")
RE_PATTERN_TOPIC_UUID = re.compile(r"/.+/(.*)/.+")
RE_PATTERN_TOPIC_USERID = re.compile(r"(/app/)(\d+)(.*/subscribe)")
"""re pattern to search/extract the uuid from an MQTT topic or the "from" field in message header"""

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
KEY_MULTIPLE = "multiple"
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
KEY_GATEWAY = "gateway"
KEY_SERVER = "server"
KEY_HOST = "host"
KEY_PORT = "port"
KEY_SECONDSERVER = "secondServer"
KEY_SECONDHOST = "secondHost"
KEY_SECONDPORT = "secondPort"
KEY_USERID = "userId"
KEY_REDIRECT = "redirect"
KEY_CONTROL = "control"
KEY_DIGEST = "digest"
KEY_ABILITY = "ability"
KEY_BIND = "bind"
KEY_BINDTIME = "bindTime"
KEY_REPORT = "report"
KEY_ONLINE = "online"
KEY_TRIGGER = "trigger"
KEY_TRIGGERX = "triggerx"
KEY_TIMER = "timer"
KEY_TIMERX = "timerx"
KEY_DOWN = "down"
KEY_CYCLE = "cycle"
KEY_DURATION = "duration"
KEY_OFFDURATION = "offDuration"
KEY_ONDURATION = "onDuration"
KEY_END = "end"
KEY_CYCLE = "cycle"
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
KEY_SECTION = "section"
KEY_LOCK = "lock"
KEY_TOGGLE = "toggle"
KEY_TOGGLEX = "togglex"
KEY_ONOFF = "onoff"
KEY_LIGHT = "light"
KEY_EFFECT = "effect"
KEY_EFFECTNAME = "effectName"
KEY_MEMBER = "member"
KEY_ID_ = "Id"
KEY_CAPACITY = "capacity"
KEY_RGB = "rgb"
KEY_LUMINANCE = "luminance"
KEY_TEMPERATURE = "temperature"
KEY_TEMP = "temp"
KEY_HUMIDITY = "humidity"
KEY_HUMI = "humi"
KEY_SPRAY = "spray"
KEY_FAN = "fan"
KEY_SPEED = "speed"
KEY_MAXSPEED = "maxSpeed"
KEY_FILTER = "filter"
KEY_LIFE = "life"
KEY_PRESENCE = "presence"
KEY_DISTANCE = "distance"
KEY_TIMES = "times"
KEY_WORKMODE = "workMode"
KEY_TESTMODE = "testMode"
KEY_NOBODYTIME = "noBodyTime"
KEY_SENSITIVITY = "sensitivity"
KEY_LEVEL = "level"
KEY_MTHX = "mthx"
KEY_MTH1 = "mth1"
KEY_MTH2 = "mth2"
KEY_MTH3 = "mth3"
KEY_HUB = "hub"
KEY_EXCEPTION = "exception"
KEY_BATTERY = "battery"
KEY_VALUE = "value"
KEY_HUBID = "hubId"
KEY_SUBDEVICE = "subdevice"
KEY_SUBDEVICELIST = "subdeviceList"
KEY_ID = "id"
KEY_SUBID = "subId"
KEY_LASTACTIVETIME = "lastActiveTime"
KEY_SYNCEDTIME = "syncedTime"
KEY_LATESTSAMPLETIME = "latestSampleTime"
KEY_LATEST = "latest"
KEY_TEMPHUM = "tempHum"
KEY_TEMPHUMI = "tempHumi"
KEY_LATESTTEMPERATURE = "latestTemperature"
KEY_LATESTHUMIDITY = "latestHumidity"
KEY_SMOKEALARM = "smokeAlarm"
KEY_INTERCONN = "interConn"
KEY_DOORWINDOW = "doorWindow"
KEY_WATERLEAK = "waterLeak"
KEY_LATESTWATERLEAK = "latestWaterLeak"
KEY_SCHEDULE = "schedule"
KEY_SCHEDULEB = "scheduleB"
KEY_SCHEDULEBMODE = "scheduleBMode"
KEY_SCHEDULEUNITTIME = "scheduleUnitTime"
KEY_ELECTRICITY = "electricity"
KEY_POWER = "power"
KEY_CURRENT = "current"
KEY_VOLTAGE = "voltage"
KEY_FACTOR = "factor"
KEY_CONSUME = "consume"
KEY_MCONSUME = "mConsume"
KEY_CONSUMPTIONX = "consumptionx"
KEY_CONSUMPTIONH = "consumptionH"
KEY_TOTAL = "total"
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
KEY_MODEB = "modeB"
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
KEY_TEMPUNIT = "tempUnit"
KEY_ALARM = "alarm"
KEY_ALARMCONFIG = "alarmConfig"
KEY_CALIBRATION = "calibration"
KEY_CTLRANGE = "ctlRange"
KEY_CTLMAX = "ctlMax"
KEY_CTLMIN = "ctlMin"
KEY_DEADZONE = "deadZone"
KEY_FROST = "frost"
KEY_OVERHEAT = "overheat"
KEY_HOLDACTION = "holdAction"
KEY_HISTORY = "history"
KEY_SENSOR = "sensor"
KEY_SUMMERMODE = "summerMode"
KEY_DELAY = "delay"
KEY_WARNING = "warning"
KEY_WORKING = "working"
KEY_SENSORSTATUS = "sensorStatus"
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
KEY_ACCOUNTCOUNTRYCODE = "accountCountryCode"
KEY_ENCRYPTION = "encryption"
KEY_AGREE = "agree"
KEY_MFACODE = "mfaCode"
KEY_USERID_ = "userid"
KEY_DOMAIN = "domain"
KEY_MQTTDOMAIN = "mqttDomain"
KEY_MFALOCKEXPIRE = "mfaLockExpire"
KEY_DEVNAME = "devName"
KEY_DEVICETYPE = "deviceType"
KEY_CLUSTER = "cluster"
KEY_RESERVEDDOMAIN = "reservedDomain"
KEY_SUBDEVICEID = "subDeviceId"
KEY_SUBDEVICENAME = "subDeviceName"

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
# rollershutter positions
ROLLERSHUTTER_POSITION_STOP = -1
ROLLERSHUTTER_POSITION_OPENED = 100
ROLLERSHUTTER_POSITION_CLOSED = 0

MTS_TEMP_SCALE = 10  # native mts temperatures expressed in tenths of °C
MTS960_TEMP_SCALE = 100  # native mts960 temperatures expressed in hundredths of °C


# mts100 (and the likes..) valves mode
MTS100_MODE_CUSTOM = 0
MTS100_MODE_HEAT = 1
MTS100_MODE_COOL = 2
MTS100_MODE_ECO = 4
MTS100_MODE_AUTO = 3
MTS100_MODE_TO_CURRENTSET_MAP = {
    MTS100_MODE_CUSTOM: KEY_CUSTOM,
    MTS100_MODE_HEAT: KEY_COMFORT,
    MTS100_MODE_COOL: KEY_ECONOMY,
    MTS100_MODE_ECO: KEY_AWAY,
    None: KEY_CUSTOM,
}


# I don't have an MTS200 to test so these are inferred from a user trace
MTS200_MODE_HEAT = 0
MTS200_MODE_COOL = 1
MTS200_MODE_ECO = 2
MTS200_MODE_AUTO = 3
MTS200_MODE_MANUAL = 4
MTS200_MODE_TO_TARGETTEMP_MAP = {
    MTS200_MODE_MANUAL: KEY_MANUALTEMP,
    MTS200_MODE_HEAT: KEY_HEATTEMP,
    MTS200_MODE_COOL: KEY_COOLTEMP,
    MTS200_MODE_ECO: KEY_ECOTEMP,
    None: KEY_MANUALTEMP,
}

MTS200_SUMMERMODE_HEAT = 1
MTS200_SUMMERMODE_COOL = 2

# MTS200 external sensor status (overheat protection)
MTS200_OVERHEAT_WARNING_OK = 0
MTS200_OVERHEAT_WARNING_OVERHEATING = 1
MTS200_OVERHEAT_WARNING_NOTCONNECTED = 2
MTS200_OVERHEAT_WARNING_MAP = {
    MTS200_OVERHEAT_WARNING_OK: "ok",
    MTS200_OVERHEAT_WARNING_OVERHEATING: "overheating",
    MTS200_OVERHEAT_WARNING_NOTCONNECTED: "disconnected",
}

# inferring the mts960 modes from the manual
MTS960_MODE_HEAT_COOL = 1
MTS960_MODE_SCHEDULE = 2
MTS960_MODE_TIMER = 3
# mapping the "state" key value to the socket/plug action
MTS960_STATE_UNKNOWN = 0
MTS960_STATE_ON = 1
MTS960_STATE_OFF = 2  # this appears when the plug is off (why not 0?)
#
MTS960_WORKING_HEAT = 1
MTS960_WORKING_COOL = 2
#
# mapping the "ONOFF" key value to the socket/plug action
MTS960_ONOFF_ON = 1
MTS960_ONOFF_OFF = 2  # this appears when the plug is off (why not 0?)
#
# mapping the Timer Type
MTS960_TIMER_TYPE_COUNTDOWN = 1
MTS960_TIMER_TYPE_CYCLE = 2
#
# diffuser mode enums
DIFFUSER_SPRAY_MODE_OFF = 2  # or 255 ? or 'any' ?
DIFFUSER_SPRAY_MODE_ECO = 0
DIFFUSER_SPRAY_MODE_FULL = 1
DIFFUSER_LIGHT_MODE_RAINBOW = 0  # color modes taken from 'homebridge-meross' plugin
DIFFUSER_LIGHT_MODE_COLOR = 1
DIFFUSER_LIGHT_MODE_TEMPERATURE = 2
DIFFUSER_LIGHT_MODE_LIST = [
    "Rainbow",
    "Color",
    "Temperature",
]

# cherub machine
HP110A_LIGHT_EFFECT_LIST = [
    "Color",
    "Scene 1",
    "Scene 2",
    "Scene 3",
    "Scene 4",
]
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
HP110A_MP3_VOLUME_MAX = 16

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

TYPE_MAP100 = "map100"
TYPE_NAME_MAP["map"] = "Smart Air Purifier"

TYPE_MFC100 = "mfc100"
TYPE_NAME_MAP["mfc"] = "Smart Fan"

TYPE_MOD100 = "mod100"  # smart humidifier
TYPE_MOD150 = "mod150"  # smart humidifier
TYPE_NAME_MAP["mod"] = "Smart Humidifier"

TYPE_NAME_MAP["mrs"] = "Smart Roller Shutter"

TYPE_NAME_MAP["msg"] = "Smart Garage Door"

TYPE_MSH300 = "msh300"  # WiFi Hub
TYPE_NAME_MAP["msh"] = "Smart Hub"

TYPE_MSL100 = "msl100"  # smart bulb
TYPE_NAME_MAP[TYPE_MSL100] = "Smart Bulb"
TYPE_MSL120 = "msl120"  # smart bulb with color/temp
TYPE_NAME_MAP[TYPE_MSL120] = "Smart RGB Bulb"
TYPE_MSL320_PRO = "msl320cp"  # smart led strip pro
TYPE_NAME_MAP[TYPE_MSL320_PRO] = "Smart RGB Pro Led Strip"
TYPE_MSL320 = "msl320"  # smart led strip
TYPE_NAME_MAP[TYPE_MSL320] = "Smart RGB Led Strip"
TYPE_NAME_MAP["msl"] = "Smart Light"

TYPE_MSS310 = "mss310"  # smart plug with energy meter
TYPE_NAME_MAP[TYPE_MSS310] = "Smart Plug"
TYPE_NAME_MAP["mss560"] = "Smart Dimmer Switch"
TYPE_NAME_MAP["mss570"] = TYPE_NAME_MAP["mss560"]
TYPE_NAME_MAP["mss"] = "Smart Switch"
TYPE_NAME_MAP["mop320"] = "Smart Outdoor Plug"

TYPE_MTS100 = "mts100"  # Smart thermostat over hub
TYPE_MTS100V3 = "mts100v3"  # Smart thermostat over hub
TYPE_MTS150 = "mts150"  # Smart thermostat over hub
TYPE_MTS150P = "mts150p"  # Smart thermostat over hub
TYPE_NAME_MAP["mts1"] = "Smart Thermostat Valve"
TYPE_MTS200 = "mts200"  # Smart thermostat over wifi
TYPE_MTS960 = "mts960"  # Smart thermostat over wifi
TYPE_NAME_MAP[TYPE_MTS960] = "Smart Socket Thermostat"
TYPE_NAME_MAP["mts"] = "Smart Thermostat"
# do not register class 'ms' since it is rather
# unusual naming and could issue collissions with mss or msl
# just set the known type
TYPE_HP110A = "hp110"
TYPE_NAME_MAP[TYPE_HP110A] = "Smart Cherub Baby Machine"

TYPE_GS559 = "gs559"
TYPE_NAME_MAP[TYPE_GS559] = "Smart Smoke Alarm"

TYPE_MS100 = "ms100"  # Smart temp/humidity sensor over Hub
TYPE_NAME_MAP[TYPE_MS100] = "Smart Temp/Humidity Sensor"

TYPE_MS130 = "ms130"  # Smart temp/humidity sensor (with display) over Hub
TYPE_NAME_MAP[TYPE_MS130] = "Smart Temp/Humidity Sensor"

TYPE_MS200 = "ms200"
TYPE_NAME_MAP[TYPE_MS200] = "Smart Door/Window Sensor"

TYPE_MS400 = "ms400"
TYPE_NAME_MAP[TYPE_MS400] = "Smart Water Leak Sensor"

TYPE_MS600 = "ms600"
TYPE_NAME_MAP[TYPE_MS600] = "Smart Presence Sensor"

# REFOSS device types
TYPE_EM06 = "em06"
TYPE_NAME_MAP[TYPE_EM06] = "Smart Energy Monitor"

#
# HUB helpers symbols
#
MTS100_ALL_TYPESET = {TYPE_MTS150, TYPE_MTS150P, TYPE_MTS100V3, TYPE_MTS100}
"""subdevices types listed in NS_APPLIANCE_HUB_MTS100_ALL"""


"""
    GP constants
"""
MANUFACTURER = "Meross"
MEROSS_MACADDRESS = "48:e1:e9:xx:xx:xx"
MQTT_DEFAULT_PORT = 443
