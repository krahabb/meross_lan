"""Constants for the Meross IoT local LAN integration."""

DOMAIN = "meross_lan"


PLATFORMS = ["switch"]

CONF_DEVICE_ID = "device_id"
CONF_DISCOVERY_PAYLOAD = "payload"
#DEFAULT_DEVICE_ID = "1909182170548290802048e1e9522946"

DISCOVERY_TOPIC = "/appliance/+/publish"
COMMAND_TOPIC = "/appliance/{}/subscribe"

METHOD_PUSH = "PUSH"
METHOD_GET = "GET"
METHOD_GETACK = "GETACK"
METHOD_SET = "SET"
METHOD_SETACK = "SETACK"

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
#Humidifier abilities
NS_APPLIANCE_SYSTEM_DND = "Appliance.System.DNDMode"
NS_APPLIANCE_CONTROL_SPRAY = "Appliance.Control.Spray"
