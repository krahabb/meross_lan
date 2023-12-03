"""Constants for the Meross IoT local LAN integration."""
from typing import Final, TypedDict

from homeassistant import const as hac

from .merossclient import cloudapi, const as mc

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


class HubConfigType(TypedDict):
    """MQTT Hub config_entry keys"""

    key: str


class DeviceConfigTypeMinimal(TypedDict):
    """Device config_entry required keys"""

    device_id: str
    payload: dict


class DeviceConfigType(DeviceConfigTypeMinimal, total=False):
    """
    Our device config allows for optional keys so total=False
    allows this in TypedDict: Nevertheless some keys are mandatory
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


CONF_EMAIL: Final = mc.KEY_EMAIL
CONF_PASSWORD: Final = hac.CONF_PASSWORD
CONF_SAVE_PASSWORD: Final = "save_password"
CONF_ALLOW_MQTT_PUBLISH: Final = "allow_mqtt_publish"
CONF_CHECK_FIRMWARE_UPDATES: Final = "check_firmware_updates"
CONF_CREATE_DIAGNOSTIC_ENTITIES: Final = "create_diagnostic_entities"


class ProfileConfigType(cloudapi.MerossCloudCredentials, total=False):
    """
    Meross cloud profile config_entry keys
    """

    password: str | None
    save_password: bool | None
    allow_mqtt_publish: bool | None
    check_firmware_updates: bool | None
    create_diagnostic_entities: bool | None


SERVICE_REQUEST = "request"
"""name of the general purpose device send request service exposed by meross_lan"""
CONF_NOTIFYRESPONSE = "notifyresponse"
"""key used in service 'request' call"""
CONF_PROFILE_ID_LOCAL: Final = ""
"""label for MerossApi as a 'fake' cloud profile"""

#
# issues general consts
#
ISSUE_CLOUD_TOKEN_EXPIRED = "cloud_token_expired"
"""raised when the token used to access the cloud api expires and need to be refreshed"""
ISSUE_DEVICE_ID_MISMATCH = "device_identity_mismatch"
"""raised when a device receives data from a different (uuid) appliance"""

# general working/configuration parameters
PARAM_INFINITE_EPOCH = 2147483647  # inifinite epoch (2038 bug?)
"""the (infinite) timeout in order to disable timed schedules"""
PARAM_COLDSTARTPOLL_DELAY = 2
"""(maximum) delay of initial poll after device setup"""
PARAM_UNAVAILABILITY_TIMEOUT = 20
"""number of seconds since last inquiry/response to consider the device unavailable"""
PARAM_HEARTBEAT_PERIOD = 295
"""whatever the connection state periodically inquire the device is available"""
PARAM_TIMEZONE_CHECK_OK_PERIOD = 604800
"""period between checks of timezone infos on locally mqtt binded devices"""
PARAM_TIMEZONE_CHECK_NOTOK_PERIOD = 86400
"""period between checks of failing timezone infos on locally mqtt binded devices"""
PARAM_TIMESTAMP_TOLERANCE = 5
"""max device timestamp diff against our and trigger warning and (eventually) fix it"""
PARAM_TRACING_ABILITY_POLL_TIMEOUT = 2
"""used to delay the iteration of abilities while tracing"""
PARAM_CLOUDMQTT_UPDATE_PERIOD = 1795
"""for polled entities over cloud MQTT use 'at least' this"""
PARAM_RESTORESTATE_TIMEOUT = 300
"""used when restoring 'calculated' state after HA restart"""
PARAM_ENERGY_UPDATE_PERIOD = 55
"""read energy consumption only every ... second"""
PARAM_SIGNAL_UPDATE_PERIOD = 295
"""read energy consumption only every ... second"""
PARAM_HUBBATTERY_UPDATE_PERIOD = 3595
"""read battery levels only every ... second"""
PARAM_HUBSENSOR_UPDATE_PERIOD = 55
PARAM_GARAGEDOOR_TRANSITION_MAXDURATION = 60
PARAM_GARAGEDOOR_TRANSITION_MINDURATION = 10
PARAM_CLOUDPROFILE_QUERY_DEVICELIST_TIMEOUT = 86400  # 1 day
"""timeout for querying cloud api deviceInfo endpoint"""
PARAM_CLOUDPROFILE_QUERY_LATESTVERSION_TIMEOUT = 604800  # 1 week
"""timeout for querying cloud api latestVersion endpoint"""
PARAM_CLOUDPROFILE_DELAYED_SAVE_TIMEOUT = 30
"""used to delay updated profile data to storage"""
