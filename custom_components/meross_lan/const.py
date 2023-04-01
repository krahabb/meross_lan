"""Constants for the Meross IoT local LAN integration."""
from typing import Final, TypedDict

from homeassistant import const as hac

from .merossclient import const as mc

DOMAIN: Final = "meross_lan"
# entity (sub)id for the switch representing DNDMode
DND_ID: Final = "dnd"
# ConfigEntry keys
CONF_DEVICE_ID: Final = hac.CONF_DEVICE_ID
# actual device key used to sign messages
CONF_KEY: Final = "key"
# device key eventually retrieved from Meross account
# This has been superseded by cloud_profile and will be
# removed from configentries as soon as the users
# update/repair/fix their configuration (no automatic migration)
CONF_CLOUD_KEY: Final = "cloud_key"
# email/id of cloud account to use with the device
CONF_PROFILE_ID: Final = "profile_id"
CONF_PROFILE_ID_LOCAL: Final = ""
CONF_PAYLOAD: Final = hac.CONF_PAYLOAD
CONF_HOST: Final = hac.CONF_HOST
# protocol used to communicate with device
CONF_PROTOCOL: Final = hac.CONF_PROTOCOL
CONF_PROTOCOL_AUTO: Final = "auto"
CONF_PROTOCOL_MQTT: Final = "mqtt"
CONF_PROTOCOL_HTTP: Final = "http"
CONF_PROTOCOL_OPTIONS: dict[str | None, str] = {
    CONF_PROTOCOL_AUTO: CONF_PROTOCOL_AUTO,
    CONF_PROTOCOL_MQTT: CONF_PROTOCOL_MQTT,
    CONF_PROTOCOL_HTTP: CONF_PROTOCOL_HTTP,
}

# general device state polling or whatever
CONF_POLLING_PERIOD: Final = "polling_period"
CONF_POLLING_PERIOD_MIN: Final = 5
CONF_POLLING_PERIOD_DEFAULT: Final = 30
# create a file with device info and communication tracing
CONF_TRACE: Final = "trace"
# when starting a trace stop it and close the file after .. secs
CONF_TRACE_TIMEOUT: Final = "trace_timeout"
CONF_TRACE_TIMEOUT_DEFAULT: Final = 600
CONF_TRACE_MAXSIZE: Final = 65536  # or when MAXSIZE exceeded
# folder where to store traces
CONF_TRACE_DIRECTORY: Final = "traces"
# filename format: device_type-device_id.csv
CONF_TRACE_FILENAME: Final = "{}-{}.csv"
# this is a 'fake' conf used to force-flush
CONF_TIMESTAMP: Final = mc.KEY_TIMESTAMP


class DeviceConfigTypeMinimal(TypedDict):
    """
    define required keys
    """

    device_id: str
    payload: dict


class DeviceConfigType(DeviceConfigTypeMinimal, total=False):
    """
    Our device config allows for optional keys so total=False
    allows thid in TypedDict: Nevertheless some keys are mandatory
    and defined though DeviceConfigTypeMinimal
    """

    key: str | None
    cloud_key: str | None
    profile_id: str | None
    host: str
    protocol: str
    polling_period: int | None
    trace: int | float | None
    trace_timeout: int | None
    timezone: str | None
    timestamp: float | None


SERVICE_REQUEST = "request"
# key used in service 'request' call
CONF_NOTIFYRESPONSE = "notifyresponse"

"""
 general working/configuration parameters (waiting to be moved to CONF_ENTRY)
"""
# (maximum) delay of initial poll after device setup
PARAM_COLDSTARTPOLL_DELAY = 2
# number of seconds since last inquiry to consider the device unavailable
PARAM_UNAVAILABILITY_TIMEOUT = 20
# whatever the connection state periodically inquire the device is there
PARAM_HEARTBEAT_PERIOD = 295
# used when restoring 'calculated' state after HA restart
PARAM_RESTORESTATE_TIMEOUT = 300
# read energy consumption only every ... second
PARAM_ENERGY_UPDATE_PERIOD = 55
# read energy consumption only every ... second
PARAM_SIGNAL_UPDATE_PERIOD = 295
# read battery levels only every ... second
PARAM_HUBBATTERY_UPDATE_PERIOD = 3595
PARAM_HUBSENSOR_UPDATE_PERIOD = 55
# 1 week before retrying timezone updates
PARAM_TIMEZONE_CHECK_PERIOD = 604800
# PARAM_STALE_DEVICE_REMOVE_TIMEOUT = 60 # disable config_entry when device is offline for more than...
PARAM_GARAGEDOOR_TRANSITION_MAXDURATION = 60
PARAM_GARAGEDOOR_TRANSITION_MINDURATION = 10
# max device timestamp diff against our and trigger warning and (eventually) fix it
PARAM_TIMESTAMP_TOLERANCE = 5
# used to delay the iteration of abilities while tracing
PARAM_TRACING_ABILITY_POLL_TIMEOUT = 2
PARAM_CLOUDPROFILE_QUERY_DEVICELIST_TIMEOUT = 86400
PARAM_CLOUDPROFILE_DELAYED_SETUP_TIMEOUT = 10
PARAM_CLOUDPROFILE_DELAYED_SAVE_TIMEOUT = 10
