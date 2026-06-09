"""Constants for the Meross IoT local LAN integration."""

from typing import TYPE_CHECKING, Final, NotRequired, TypedDict

from homeassistant import const as hac

from .merossclient import cloudapi, logging
from .merossclient.device.handler import NamespaceHandler
from .merossclient.protocol import const as mc, namespaces as mn

if TYPE_CHECKING:
    from typing import Any, Mapping


DOMAIN: Final = "meross_lan"

#########################
# common ConfigEntry keys
#########################
CONF_CREATE_DIAGNOSTIC_ENTITIES: Final = "create_diagnostic_entities"
CONF_KEY: Final = "key"
# sets the logging level x ConfigEntry
CONF_LOGGING_LEVEL: Final = "logging_level"
CONF_LOGGING_LEVEL_OPTIONS: Final = {
    logging.NOTSET: "default",
    logging.CRITICAL: "critical",
    logging.WARNING: "warning",
    logging.INFO: "info",
    logging.DEBUG: "debug",
    logging.VERBOSE: "verbose",
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
# versioning
CONF_TRACE_VERSION: Final = 3
CONF_TRACE_COLUMNS: Final = [
    "time",
    "direction",
    "transport",
    "method",
    "namespace",
    "data",
]
if TYPE_CHECKING:

    class TracingHeaderType(TypedDict):
        version: int
        config: Mapping[str, Any]
        state: Mapping[str, Any]
        trace: NotRequired[list[list]]


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
    # deprecated -> trace: NotRequired[bool]
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
# Device ConfigEntry keys
###############################
CONF_DEVICE_ID: Final = hac.CONF_DEVICE_ID
# device key eventually retrieved from Meross account
# This has been superseded by cloud_profile and will be
# removed from configentries as soon as the users
# update/repair/fix their configuration (no automatic migration)
CONF_CLOUD_KEY: Final = "cloud_key"  # deprecated
CONF_PAYLOAD: Final = hac.CONF_PAYLOAD
CONF_HOST: Final = hac.CONF_HOST
# protocol used to communicate with device
CONF_PROTOCOL: Final = hac.CONF_PROTOCOL
# general device state polling or whatever
CONF_POLLING_PERIOD: Final = "polling_period"
CONF_POLLING_PERIOD_MIN: Final = 5
CONF_POLLING_PERIOD_DEFAULT: Final = 30
# enable/disable Appliance.Control.Multiple
CONF_DISABLE_MULTIPLE: Final = "disable_multiple"
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

    # deprecated -> cloud_key: NotRequired[str | None]
    host: NotRequired[str | None]
    """device host (name or ip address): when empty the device can still use the host address recovered through MQTT payloads"""
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
CONF_PROFILE_ID_LOCAL: Final = "api"
"""label for ComponentApi as a 'fake' cloud profile"""

#
# issues general consts
#
ISSUE_CLOUD_TOKEN_EXPIRED = "cloud_token_expired"
"""raised when the token used to access the cloud api expires and need to be refreshed"""
ISSUE_DEVICE_ID_MISMATCH = "device_identity_mismatch"
"""raised when a device receives data from a different (uuid) appliance"""
ISSUE_DEVICE_TIMEZONE = "device_timezone"
"""raised when a device timezone is not set or is anyway different from HA default"""
ISSUE_HUB_SUBDEVICE_REMOVED = "hub_subdevice_removed"
"""raised when an Hub SubDevice is no more available (unbinded) and the device_egistry needs cleanup."""

# general working/configuration parameters
PARAM_DEFAULT_KEY = "meross"
"""Default key commonly used for local MQTT binded devices (MQTT Hub conf)"""
PARAM_INFINITE_TIMEOUT = 2147483647  # inifinite epoch (2038 bug?)
"""the (infinite) timeout in order to disable timed schedules"""
PARAM_COLDSTARTPOLL_DELAY = 2
"""(maximum) delay of initial poll after device setup"""
PARAM_CLOUD_UPDATE_PERIOD = 1195
"""General polling period for entities over cloud MQTT use 'at least' this"""
PARAM_CONFIG_UPDATE_PERIOD = 300
"""read device config polling period"""
PARAM_ENERGY_UPDATE_PERIOD = 55
"""read energy consumption only every ... second"""
PARAM_ENERGY_CLOUD_UPDATE_PERIOD = 600
"""read energy consumption over cloud mqtt only every ... second"""
PARAM_CLOUDPROFILE_DELAYED_SETUP_TIMEOUT = 5
"""(mimimum) timeout before querying cloud api after loading the profile"""
PARAM_CLOUDPROFILE_QUERY_DEVICELIST_TIMEOUT = 86400  # 1 day
"""timeout for querying cloud api deviceInfo endpoint"""
PARAM_CLOUDPROFILE_QUERY_DEVICELIST_RETRIGGER_TIMEOUT = 300  # 5 minutes
"""timeout for re-triggering cloud api deviceInfo query when internally solicited."""
PARAM_CLOUDPROFILE_DELAYED_SAVE_TIMEOUT = 30
"""used to delay updated profile data to storage"""

POLLING_CONFIG_FASTSENSOR = (0, 180, NamespaceHandler.async_poll_smart)
POLLING_CONFIG_SLOWSENSOR = (300, 600, NamespaceHandler.async_poll_smart)
POLLING_CONFIG_CONFIGURATION = (
    PARAM_CONFIG_UPDATE_PERIOD,
    PARAM_CLOUD_UPDATE_PERIOD,
    NamespaceHandler.async_poll_smart,
)
"""Common polling configuration for namespaces carrying configuration parameters.
These are polled on a longer period since we don't expect them to change very often."""

POLLING_CONFIG_DIAGNOSTIC = (300, PARAM_CLOUD_UPDATE_PERIOD, None)
NamespaceHandler.POLLING_CONFIG_MAP.update(
    {
        mn.Appliance_Config_Sensor_Association: POLLING_CONFIG_CONFIGURATION,
        mn.Appliance_Mcu_Firmware: NamespaceHandler.POLLING_CONFIG_ONCE,
        mn.Appliance_Mcu_Hp110_Firmware: NamespaceHandler.POLLING_CONFIG_ONCE,
    }
)
