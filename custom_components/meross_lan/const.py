"""Constants for the Meross IoT local LAN integration."""

from homeassistant import const as hac
from .merossclient import const as mc

DOMAIN = "meross_lan"

DND_ID = 'dnd' # entity (sub)id for the switch representing DNDMode

# ConfigEntry keys
CONF_DEVICE_ID = hac.CONF_DEVICE_ID
CONF_KEY = 'key' # actual device key used to sign messages
CONF_CLOUD_KEY = 'cloud_key' # device key eventually retrieved from Meross account
CONF_CLOUD_PROFILE = 'cloud_profile' # email/id of cloud account to use with the device
CONF_PAYLOAD = hac.CONF_PAYLOAD
CONF_HOST = hac.CONF_HOST

CONF_PROTOCOL = hac.CONF_PROTOCOL # protocol used to communicate with device
CONF_PROTOCOL_AUTO = 'auto' # 'best effort' behaviour
CONF_PROTOCOL_MQTT = 'mqtt'
CONF_PROTOCOL_HTTP = 'http'
CONF_PROTOCOL_OPTIONS = {
    CONF_PROTOCOL_AUTO: CONF_PROTOCOL_AUTO,
    CONF_PROTOCOL_MQTT: CONF_PROTOCOL_MQTT,
    CONF_PROTOCOL_HTTP: CONF_PROTOCOL_HTTP
}

CONF_POLLING_PERIOD = 'polling_period' # general device state polling or whatever
CONF_POLLING_PERIOD_MIN = 5
CONF_POLLING_PERIOD_DEFAULT = 30

CONF_TRACE = 'trace' # create a file with device info and communication tracing
CONF_TRACE_TIMEOUT = 'trace_timeout'
CONF_TRACE_TIMEOUT_DEFAULT = 600 # when starting a trace stop it and close the file after .. secs
CONF_TRACE_MAXSIZE = 65536 # or when MAXSIZE exceeded
CONF_TRACE_DIRECTORY = 'traces' # folder where to store traces
CONF_TRACE_FILENAME = '{}-{}.csv' # filename format: device_type-device_id.csv

CONF_TIMESTAMP = mc.KEY_TIMESTAMP # this is a 'fake' conf used to force-flush

SERVICE_REQUEST = "request"
CONF_NOTIFYRESPONSE = 'notifyresponse' # key used in service 'request' call

"""
 general working/configuration parameters (waiting to be moved to CONF_ENTRY)
"""
PARAM_COLDSTARTPOLL_DELAY = 2 # (maximum) delay of initial poll after device setup
PARAM_UNAVAILABILITY_TIMEOUT = 20  # number of seconds since last inquiry to consider the device unavailable
PARAM_HEARTBEAT_PERIOD = 295 # whatever the connection state periodically inquire the device is there
PARAM_RESTORESTATE_TIMEOUT = 300 # used when restoring 'calculated' state after HA restart
PARAM_ENERGY_UPDATE_PERIOD = 55 # read energy consumption only every ... second
PARAM_SIGNAL_UPDATE_PERIOD = 295 # read energy consumption only every ... second
PARAM_HUBBATTERY_UPDATE_PERIOD = 3595 # read battery levels only every ... second
PARAM_HUBSENSOR_UPDATE_PERIOD = 55
PARAM_TIMEZONE_CHECK_PERIOD = 604800 # 1 week before retrying timezone updates
#PARAM_STALE_DEVICE_REMOVE_TIMEOUT = 60 # disable config_entry when device is offline for more than...
PARAM_GARAGEDOOR_TRANSITION_MAXDURATION = 60
PARAM_GARAGEDOOR_TRANSITION_MINDURATION = 10
PARAM_TIMESTAMP_TOLERANCE = 5 # max device timestamp diff against our and trigger warning and (eventually) fix it
PARAM_TRACING_ABILITY_POLL_TIMEOUT = 2 # used to delay the iteration of abilities while tracing
PARAM_CLOUDAPI_QUERY_DEVICELIST_TIMEOUT = 86400
