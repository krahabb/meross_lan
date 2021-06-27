"""Constants for the Meross IoT local LAN integration."""

from homeassistant import const as hac
from .merossclient import const as mc

DOMAIN = "meross_lan"

PLATFORM_SWITCH = 'switch'
PLATFORM_SENSOR = 'sensor'
PLATFORM_BINARY_SENSOR = 'binary_sensor'
PLATFORM_LIGHT = 'light'
PLATFORM_COVER = 'cover'
PLATFORM_CLIMATE = 'climate'

SERVICE_REQUEST = "request"

# ConfigEntry keys
CONF_DEVICE_ID = hac.CONF_DEVICE_ID
CONF_KEY = 'key'
CONF_PAYLOAD = hac.CONF_PAYLOAD
CONF_DEVICE_TYPE = "device_type"
CONF_HOST = hac.CONF_HOST
CONF_PROTOCOL = hac.CONF_PROTOCOL # protocol used to communicate with device
CONF_OPTION_AUTO = 'auto'
CONF_OPTION_MQTT = 'mqtt'
CONF_OPTION_HTTP = 'http'
CONF_PROTOCOL_OPTIONS = (
    CONF_OPTION_AUTO, # best-effort: tries whatever to connect
    CONF_OPTION_MQTT,
    CONF_OPTION_HTTP
)
CONF_POLLING_PERIOD = 'polling_period' # general device state polling or whatever
CONF_POLLING_PERIOD_MIN = 5
CONF_POLLING_PERIOD_DEFAULT = 30
CONF_TIME_ZONE = hac.CONF_TIME_ZONE # if set in config we'll force time & zone for devices
CONF_TIMESTAMP = mc.KEY_TIMESTAMP # this is a 'fake' conf param we'll add to config_entry when we want to force flush to storage

"""
 general working/configuration parameters (waiting to be moved to CONF_ENTRY)
"""
PARAM_UNAVAILABILITY_TIMEOUT = 20  # number of seconds since last inquiry to consider the device unavailable
PARAM_HEARTBEAT_PERIOD = 295 # whatever the connection state periodically inquire the device is there
PARAM_ENERGY_UPDATE_PERIOD = 55 # read energy consumption only every ... second
PARAM_HUBBATTERY_UPDATE_PERIOD = 3595 # read battery levels only every ... second
PARAM_HUBSENSOR_UPDATE_PERIOD = 55
#PARAM_STALE_DEVICE_REMOVE_TIMEOUT = 60 # disable config_entry when device is offline for more than...

