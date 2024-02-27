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
PARAM_ROLLERSHUTTER_TRANSITION_POLL_TIMEOUT = 2
"""used when polling the cover state to monitor an ongoing transition"""
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

"""
Default timeouts and config parameters for polled namespaces managed
through PollingStrategy helper classes. For every namespace we
set the defaults used to initialize these helpers.
The configuration is set in the tuple as:
(polling_timeout, polling_timeout_cloud, response_size, additional_size)
see the PollingStrategy for the meaning of these values
The 'response_size' is a conservative (in excess) estimate of the
expected response size for the whole message (header itself weights around 300 bytes).
Some payloads would depend on the number of channels/subdevices available
and the configured number would just be a base size (minimum) while
the 'additional_size' value must be multiplied for the number of channels/subdevices
and will be used to adjust the actual 'response_size' at runtime in the relative PollingStrategy.
This parameter in turn will be used to split expected huge payload requests/responses
in Appliance.Control.Multiple since it appears the HTTP interface has an outbound
message size limit around 3000 chars/bytes (on a legacy mss310) and this would lead to a malformed (truncated)
response. This issue also appeared on hubs when querying for a big number of subdevices
as reported in #244 (here the buffer limit was around 4000 chars). From limited testing this 'kind of overflow' is not happening on MQTT
responses though
"""
POLLING_STRATEGY_CONF: dict[str, tuple[int, int, int, int]] = {
    mc.NS_APPLIANCE_SYSTEM_ALL: (0, 0, 1000, 0),
    mc.NS_APPLIANCE_SYSTEM_DEBUG: (0, 0, 1900, 0),
    mc.NS_APPLIANCE_SYSTEM_DNDMODE: (0, PARAM_CLOUDMQTT_UPDATE_PERIOD, 320, 0),
    mc.NS_APPLIANCE_SYSTEM_RUNTIME: (
        PARAM_SIGNAL_UPDATE_PERIOD,
        PARAM_CLOUDMQTT_UPDATE_PERIOD,
        330,
        0,
    ),
    mc.NS_APPLIANCE_CONFIG_OVERTEMP: (0, PARAM_CLOUDMQTT_UPDATE_PERIOD, 340, 0),
    mc.NS_APPLIANCE_CONTROL_CONSUMPTIONX: (
        PARAM_ENERGY_UPDATE_PERIOD,
        PARAM_CLOUDMQTT_UPDATE_PERIOD,
        320,
        53,
    ),
    mc.NS_APPLIANCE_CONTROL_DIFFUSER_SENSOR: (0, 0, PARAM_HEADER_SIZE, 100),
    mc.NS_APPLIANCE_CONTROL_ELECTRICITY: (0, PARAM_CLOUDMQTT_UPDATE_PERIOD, 430, 0),
    mc.NS_APPLIANCE_CONTROL_FAN: (
        0,
        PARAM_CLOUDMQTT_UPDATE_PERIOD,
        PARAM_HEADER_SIZE,
        20,
    ),
    mc.NS_APPLIANCE_CONTROL_FILTERMAINTENANCE: (
        PARAM_CLOUDMQTT_UPDATE_PERIOD,
        PARAM_CLOUDMQTT_UPDATE_PERIOD,
        PARAM_HEADER_SIZE,
        35,
    ),
    mc.NS_APPLIANCE_CONTROL_LIGHT_EFFECT: (0, PARAM_CLOUDMQTT_UPDATE_PERIOD, 1850, 0),
    mc.NS_APPLIANCE_CONTROL_MP3: (0, 0, 380, 0),
    mc.NS_APPLIANCE_CONTROL_PHYSICALLOCK: (
        0,
        PARAM_CLOUDMQTT_UPDATE_PERIOD,
        PARAM_HEADER_SIZE,
        35,
    ),
    mc.NS_APPLIANCE_CONTROL_THERMOSTAT_CALIBRATION: (
        0,
        PARAM_CLOUDMQTT_UPDATE_PERIOD,
        PARAM_HEADER_SIZE,
        80,
    ),
    mc.NS_APPLIANCE_CONTROL_THERMOSTAT_DEADZONE: (
        0,
        PARAM_CLOUDMQTT_UPDATE_PERIOD,
        PARAM_HEADER_SIZE,
        80,
    ),
    mc.NS_APPLIANCE_CONTROL_THERMOSTAT_FROST: (
        0,
        PARAM_CLOUDMQTT_UPDATE_PERIOD,
        PARAM_HEADER_SIZE,
        80,
    ),
    mc.NS_APPLIANCE_CONTROL_THERMOSTAT_OVERHEAT: (0, 0, PARAM_HEADER_SIZE, 140),
    mc.NS_APPLIANCE_CONTROL_THERMOSTAT_SCHEDULE: (0, 0, PARAM_HEADER_SIZE, 550),
    mc.NS_APPLIANCE_CONTROL_THERMOSTAT_SCHEDULEB: (0, 0, PARAM_HEADER_SIZE, 550),
    mc.NS_APPLIANCE_CONTROL_THERMOSTAT_SENSOR: (0, 0, PARAM_HEADER_SIZE, 40),
    mc.NS_APPLIANCE_CONTROL_SCREEN_BRIGHTNESS: (
        0,
        PARAM_CLOUDMQTT_UPDATE_PERIOD,
        PARAM_HEADER_SIZE,
        70,
    ),
    mc.NS_APPLIANCE_GARAGEDOOR_CONFIG: (0, PARAM_CLOUDMQTT_UPDATE_PERIOD, 410, 0),
    mc.NS_APPLIANCE_GARAGEDOOR_MULTIPLECONFIG: (
        0,
        PARAM_CLOUDMQTT_UPDATE_PERIOD,
        PARAM_HEADER_SIZE,
        140,
    ),
    mc.NS_APPLIANCE_HUB_BATTERY: (
        PARAM_HUBBATTERY_UPDATE_PERIOD,
        PARAM_CLOUDMQTT_UPDATE_PERIOD,
        PARAM_HEADER_SIZE,
        40,
    ),
    mc.NS_APPLIANCE_HUB_MTS100_ADJUST: (
        PARAM_CLOUDMQTT_UPDATE_PERIOD,
        PARAM_CLOUDMQTT_UPDATE_PERIOD,
        PARAM_HEADER_SIZE,
        40,
    ),
    mc.NS_APPLIANCE_HUB_MTS100_ALL: (
        0,
        PARAM_CLOUDMQTT_UPDATE_PERIOD,
        PARAM_HEADER_SIZE,
        350,
    ),
    mc.NS_APPLIANCE_HUB_MTS100_SCHEDULEB: (
        0,
        PARAM_CLOUDMQTT_UPDATE_PERIOD,
        PARAM_HEADER_SIZE,
        500,
    ),
    mc.NS_APPLIANCE_HUB_SENSOR_ADJUST: (
        PARAM_CLOUDMQTT_UPDATE_PERIOD,
        PARAM_CLOUDMQTT_UPDATE_PERIOD,
        PARAM_HEADER_SIZE,
        60,
    ),
    mc.NS_APPLIANCE_HUB_SENSOR_ALL: (
        0,
        PARAM_CLOUDMQTT_UPDATE_PERIOD,
        PARAM_HEADER_SIZE,
        250,
    ),
    mc.NS_APPLIANCE_HUB_SUBDEVICE_VERSION: (
        0,
        PARAM_CLOUDMQTT_UPDATE_PERIOD,
        PARAM_HEADER_SIZE,
        55,
    ),
    mc.NS_APPLIANCE_HUB_TOGGLEX: (0, 0, PARAM_HEADER_SIZE, 35),
    mc.NS_APPLIANCE_ROLLERSHUTTER_ADJUST: (
        PARAM_CLOUDMQTT_UPDATE_PERIOD,
        PARAM_CLOUDMQTT_UPDATE_PERIOD,
        PARAM_HEADER_SIZE,
        35,
    ),
    mc.NS_APPLIANCE_ROLLERSHUTTER_CONFIG: (
        0,
        PARAM_CLOUDMQTT_UPDATE_PERIOD,
        PARAM_HEADER_SIZE,
        70,
    ),
    mc.NS_APPLIANCE_ROLLERSHUTTER_POSITION: (0, 0, PARAM_HEADER_SIZE, 50),
    mc.NS_APPLIANCE_ROLLERSHUTTER_STATE: (0, 0, PARAM_HEADER_SIZE, 40),
}
