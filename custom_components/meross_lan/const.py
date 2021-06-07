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
CONF_TIMESTAMP = mc.KEY_TIMESTAMP
CONF_PROTOCOL = hac.CONF_PROTOCOL
CONF_OPTION_AUTO = 'auto'
CONF_OPTION_MQTT = 'mqtt'
CONF_OPTION_HTTP = 'http'
CONF_PROTOCOL_OPTIONS = (
    CONF_OPTION_AUTO, # best-effort: tries whatever to connect
    CONF_OPTION_MQTT,
    CONF_OPTION_HTTP
)

DISCOVERY_TOPIC = "/appliance/+/publish"
REQUEST_TOPIC = "/appliance/{}/subscribe"
RESPONSE_TOPIC = "/appliance/{}/publish"

"""
 general working/configuration parameters (waiting to be moved to CONF_ENTRY)
"""
PARAM_UNAVAILABILITY_TIMEOUT = 20  # number of seconds since last inquiry to consider the device unavailable
PARAM_POLLING_PERIOD = 30  # general device state polling or whatever
PARAM_HEARTBEAT_PERIOD = 300 # whatever the connection state periodically inquire the device is there
PARAM_ENERGY_UPDATE_PERIOD = 55 # read energy consumption only every ... second
PARAM_HUBBATTERY_UPDATE_PERIOD = 3595 # read battery levels only every ... second
PARAM_HUBSENSOR_UPDATE_PERIOD = 55
#PARAM_STALE_DEVICE_REMOVE_TIMEOUT = 60 # disable config_entry when device is offline for more than...

