"""Constants for the Meross IoT local LAN integration."""

import logging
from typing import Final, NotRequired, TypedDict

from homeassistant import const as hac

from .merossclient import cloudapi, const as mc

DOMAIN: Final = "meross_lan"
#########################
# common ConfigEntry keys
#########################
CONF_CREATE_DIAGNOSTIC_ENTITIES: Final = "create_diagnostic_entities"
CONF_KEY: Final = "key"
# sets the logging level x ConfigEntry
CONF_LOGGING_LEVEL: Final = "logging_level"
CONF_LOGGING_VERBOSE: Final = 5
CONF_LOGGING_DEBUG: Final = logging.DEBUG
CONF_LOGGING_INFO: Final = logging.INFO
CONF_LOGGING_WARNING: Final = logging.WARNING
CONF_LOGGING_CRITICAL: Final = logging.CRITICAL
CONF_LOGGING_LEVEL_OPTIONS: Final = {
    logging.NOTSET: "default",
    CONF_LOGGING_CRITICAL: "critical",
    CONF_LOGGING_WARNING: "warning",
    CONF_LOGGING_INFO: "info",
    CONF_LOGGING_DEBUG: "debug",
    CONF_LOGGING_VERBOSE: "verbose",
}
CONF_OBFUSCATE: Final = "obfuscate"
# create a file with device info and communication tracing
CONF_TRACE: Final = "trace"
# when starting a trace stop it and close the file after .. secs
CONF_TRACE_TIMEOUT: Final = "trace_timeout"
CONF_TRACE_TIMEOUT_DEFAULT: Final = 600
CONF_TRACE_MAXSIZE: Final = 262144  # or when MAXSIZE exceeded
# folder where to store traces
CONF_TRACE_DIRECTORY: Final = "traces"
CONF_TRACE_FILENAME: Final = "{}_{}.csv"


class ManagerConfigType(TypedDict):
    """Common config_entry keys for any ConfigEntryManager type"""

    key: str
    """device key unique to this ConfigEntryManager type"""
    create_diagnostic_entities: NotRequired[bool]
    """create various diagnostic entities for debugging/diagnostics purposes"""
    logging_level: NotRequired[int]
    """override the default log level set in HA configuration"""
    obfuscate: NotRequired[bool]
    """obfuscate sensitive data when logging/tracing"""
    trace_timeout: NotRequired[int | None]
    """duration of the tracing feature when activated"""


#####################################################
# ApiProfile (Hub and MerossProfile) ConfigEntry keys
#####################################################
CONF_ALLOW_MQTT_PUBLISH: Final = "allow_mqtt_publish"


class ApiProfileConfigType(ManagerConfigType):
    """Common config_entry keys for ApiProfile type"""

    allow_mqtt_publish: NotRequired[bool]
    """allow meross_lan to publish over local MQTT: actually ignored since it is True in code"""


class HubConfigType(ApiProfileConfigType):
    """MQTT Hub config_entry keys"""


###############################
# MerossDevice ConfigEntry keys
###############################
CONF_DEVICE_ID: Final = hac.CONF_DEVICE_ID
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
# this is a 'fake' conf used to force-flush
CONF_TIMESTAMP: Final = mc.KEY_TIMESTAMP


class DeviceConfigTypeMinimal(ManagerConfigType):
    """Device config_entry required keys"""

    device_id: str
    payload: dict


class DeviceConfigType(DeviceConfigTypeMinimal, total=False):
    """
    Our device config allows for optional keys so total=False
    allows this in TypedDict: Nevertheless some keys are mandatory
    and defined though DeviceConfigTypeMinimal
    """

    cloud_key: NotRequired[str | None]
    """deprecated field: used to store the device key as recovered from the cloud account"""
    host: NotRequired[str]
    """device address: when empty the device can still use the host address recovered through MQTT payloads"""
    protocol: NotRequired[str]
    """configures the protocol: auto will automatically switch between the available transports"""
    polling_period: NotRequired[int | None]
    """base polling period to query device state"""
    timezone: NotRequired[str]
    """IANA timezone set in the device"""
    timestamp: NotRequired[float]
    """special (hidden from UI) field used to force entry save"""


CONF_CLOUD_REGION: Final = "cloud_region"
CONF_EMAIL: Final = mc.KEY_EMAIL
CONF_PASSWORD: Final = hac.CONF_PASSWORD
CONF_MFA_CODE: Final = "mfa_code"
CONF_SAVE_PASSWORD: Final = "save_password"
CONF_CHECK_FIRMWARE_UPDATES: Final = "check_firmware_updates"


class ProfileConfigType(
    ApiProfileConfigType, cloudapi.MerossCloudCredentials, total=False
):
    """
    Meross cloud profile config_entry keys
    """

    cloud_region: NotRequired[str]
    mfa_code: NotRequired[bool]
    """logged in with MFA"""
    password: NotRequired[str]
    """password of the Meross user account"""
    save_password: NotRequired[bool]
    """saves the account password in HA storage"""
    check_firmware_updates: NotRequired[bool]
    """activate a periodical query to the cloud api to look for fw updates """


SERVICE_REQUEST = "request"
"""name of the general purpose device send request service exposed by meross_lan"""
CONF_NOTIFYRESPONSE = "notifyresponse"
"""key used in service 'request' call"""
CONF_PROFILE_ID_LOCAL: Final = ""
"""label for MerossApi as a 'fake' cloud profile"""

#
# some common entitykeys
#
DND_ID: Final = "dnd"
SIGNALSTRENGTH_ID: Final = "signal_strength"
ENERGY_ESTIMATE_ID: Final = "energy_estimate"
#
# issues general consts
#
ISSUE_CLOUD_TOKEN_EXPIRED = "cloud_token_expired"
"""raised when the token used to access the cloud api expires and need to be refreshed"""
ISSUE_DEVICE_ID_MISMATCH = "device_identity_mismatch"
"""raised when a device receives data from a different (uuid) appliance"""
ISSUE_DEVICE_TIMEZONE = "device_timezone"
"""raised when a device timezone is not set or is anyway different from HA default"""

# general working/configuration parameters
PARAM_INFINITE_TIMEOUT = 2147483647  # inifinite epoch (2038 bug?)
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
PARAM_ROLLERSHUTTER_TRANSITION_POLL_TIMEOUT = 2
"""used when polling the cover state to monitor an ongoing transition"""
PARAM_CLOUDMQTT_UPDATE_PERIOD = 1795
"""for polled entities over cloud MQTT use 'at least' this"""
PARAM_DIAGNOSTIC_UPDATE_PERIOD = 300
"""read diagnostic sensors only every ... second"""
PARAM_ENERGY_UPDATE_PERIOD = 55
"""read energy consumption only every ... second"""
PARAM_GARAGEDOOR_TRANSITION_MAXDURATION = 60
PARAM_GARAGEDOOR_TRANSITION_MINDURATION = 10
PARAM_CLOUDPROFILE_DELAYED_SETUP_TIMEOUT = 5
"""(mimimum) timeout before querying cloud api after loading the profile"""
PARAM_CLOUDPROFILE_QUERY_DEVICELIST_TIMEOUT = 86400  # 1 day
"""timeout for querying cloud api deviceInfo endpoint"""
PARAM_CLOUDPROFILE_QUERY_LATESTVERSION_TIMEOUT = 604800  # 1 week
"""timeout for querying cloud api latestVersion endpoint"""
PARAM_CLOUDPROFILE_DELAYED_SAVE_TIMEOUT = 30
"""used to delay updated profile data to storage"""
PARAM_HEADER_SIZE = 300
"""(rough) estimate of the header part of any response"""
PARAM_RESPONSE_SIZE_MAX = 3000
"""(rough) estimate of the allowed response size limit before overflow occurs (see #244)"""
