"""Constants for the Meross IoT local LAN integration."""

DOMAIN = "meross_lan"
#PLATFORMS = ["switch", "sensor", "light", "cover"]


CONF_DEVICE_ID = "device_id"
CONF_KEY = "key"
CONF_DISCOVERY_PAYLOAD = "payload"
CONF_DEVICE_TYPE = "device_type"

DISCOVERY_TOPIC = "/appliance/+/publish"
COMMAND_TOPIC = "/appliance/{}/subscribe"

METHOD_PUSH = "PUSH"
METHOD_GET = "GET"
METHOD_GETACK = "GETACK"
METHOD_SET = "SET"
METHOD_SETACK = "SETACK"
METHOD_ERROR = "ERROR"

NS_APPLIANCE_SYSTEM_ALL = "Appliance.System.All"
NS_APPLIANCE_SYSTEM_ABILITY = "Appliance.System.Ability"
NS_APPLIANCE_SYSTEM_REPORT = "Appliance.System.Report"
NS_APPLIANCE_SYSTEM_ONLINE = "Appliance.System.Online"
NS_APPLIANCE_SYSTEM_DEBUG = "Appliance.System.Debug"
NS_APPLIANCE_CONFIG_TRACE = "Appliance.Config.Trace"
NS_APPLIANCE_CONFIG_WIFILIST = "Appliance.Config.WifiList"
NS_APPLIANCE_CONTROL_TOGGLEX = "Appliance.Control.ToggleX"
NS_APPLIANCE_CONTROL_TOGGLE = "Appliance.Control.Toggle"
NS_APPLIANCE_CONTROL_TRIGGER = "Appliance.Control.Trigger"
NS_APPLIANCE_CONTROL_TRIGGERX = "Appliance.Control.TriggerX"
NS_APPLIANCE_CONTROL_CONSUMPTIONX = "Appliance.Control.ConsumptionX"
NS_APPLIANCE_CONTROL_ELECTRICITY = "Appliance.Control.Electricity"
# Light Abilities
NS_APPLIANCE_CONTROL_LIGHT = "Appliance.Control.Light"
# Humidifier abilities
NS_APPLIANCE_SYSTEM_DND = "Appliance.System.DNDMode"
NS_APPLIANCE_CONTROL_SPRAY = "Appliance.Control.Spray"
# Garage door opener
NS_APPLIANCE_GARAGEDOOR_STATE = "Appliance.GarageDoor.State"

"""
 general working/configuration parameters (waiting to be moved to CONF_ENTRY)
"""
PARAM_UNAVAILABILITY_TIMEOUT = 10  # number of seconds since last inquiry to consider the device unavailable
PARAM_ENERGY_UPDATE_PERIOD = 60 # read energy consumption only every ... second
PARAM_UPDATE_POLLING_PERIOD = 30  # periodic state polling or whatever
PARAM_STALE_DEVICE_REMOVE_TIMEOUT = 60 # disable config_entry when device is offline for more than...
"""
    GP constant strings
"""
MANUFACTURER = "Meross"