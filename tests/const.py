"""Constants for integration_blueprint tests."""
from custom_components.meross_lan import const as mlc
from custom_components.meross_lan.merossclient import cloudapi, const as mc

# Mock config data to be used across multiple tests
MOCK_DEVICE_UUID = "01234567890123456789001122334455"
MOCK_DEVICE_IP = "10.0.0.1"
MOCK_MACADDRESS = "48:e1:e9:aa:bb:cc"
MOCK_KEY = "test_key"
MOCK_HUB_CONFIG: mlc.HubConfigType = {
    mlc.CONF_KEY: MOCK_KEY,
}
MOCK_DEVICE_CONFIG: mlc.DeviceConfigType = {
    mlc.CONF_DEVICE_ID: "9109182170548290880048b1a9522933",
    mlc.CONF_KEY: MOCK_KEY,
    mlc.CONF_PAYLOAD: {
        mc.KEY_ALL: {
            "system": {
                "hardware": {
                    "type": "msh300",
                    "subType": "un",
                    "version": "4.0.0",
                    "chipType": "mt7686",
                    "uuid": "9109182170548290880048b1a9522933",
                    "macAddress": "aa:bb:cc:dd:ee:ff",
                },
                "firmware": {
                    "version": "4.1.26",
                    "compileTime": "2020/11/07 16:29:31 GMT +08:00",
                    "wifiMac": "aa:bb:cc:dd:ee:ff",
                    "innerIp": "10.0.0.1",
                    "server": "10.0.0.7",
                    "port": "8883",
                    "userId": "0",
                },
                "time": {
                    "timestamp": 1638365548,
                    "timezone": "Europe/Rome",
                    "timeRule": [[1635634800, 3600, 0], [1648339200, 7200, 1]],
                },
                "online": {"status": 1},
            },
            "digest": {
                "hub": {
                    "hubId": -381895630,
                    "mode": 0,
                    "subdevice": [
                        {"id": "120027D21C19", "status": 2},
                        {
                            "id": "01008C11",
                            "status": 1,
                            "scheduleBMode": 6,
                            "onoff": 1,
                            "lastActiveTime": 1638365524,
                            "mts100v3": {"mode": 0},
                        },
                        {
                            "id": "0100783A",
                            "status": 1,
                            "scheduleBMode": 6,
                            "onoff": 1,
                            "lastActiveTime": 1638365410,
                            "mts100v3": {"mode": 0},
                        },
                    ],
                }
            },
        },
        mc.KEY_ABILITY: {
            "Appliance.Config.Key": {},
            "Appliance.Config.WifiList": {},
            "Appliance.Config.Wifi": {},
            "Appliance.Config.Trace": {},
            "Appliance.System.All": {},
            "Appliance.System.Hardware": {},
            "Appliance.System.Firmware": {},
            "Appliance.System.Debug": {},
            "Appliance.System.Online": {},
            "Appliance.System.Time": {},
            "Appliance.System.Ability": {},
            "Appliance.System.Runtime": {},
            "Appliance.System.Report": {},
            "Appliance.System.Position": {},
            "Appliance.System.DNDMode": {},
            "Appliance.Control.Multiple": {"maxCmdNum": 5},
            "Appliance.Control.Bind": {},
            "Appliance.Control.Unbind": {},
            "Appliance.Control.Upgrade": {},
            "Appliance.Digest.Hub": {},
            "Appliance.Hub.Online": {},
            "Appliance.Hub.ToggleX": {},
            "Appliance.Hub.Exception": {},
            "Appliance.Hub.SubdeviceList": {},
            "Appliance.Hub.Report": {},
            "Appliance.Hub.Battery": {},
            "Appliance.Hub.Mts100.All": {},
            "Appliance.Hub.Mts100.TimeSync": {},
            "Appliance.Hub.Mts100.Mode": {},
            "Appliance.Hub.Mts100.Temperature": {},
            "Appliance.Hub.Mts100.Adjust": {},
            "Appliance.Hub.Mts100.Schedule": {"scheduleUnitTime": 30},
            "Appliance.Hub.Mts100.ScheduleB": {"scheduleUnitTime": 15},
            "Appliance.Hub.Sensor.All": {},
            "Appliance.Hub.Sensor.Latest": {},
            "Appliance.Hub.Sensor.TempHum": {},
            "Appliance.Hub.Sensor.Adjust": {},
            "Appliance.Hub.Sensor.Alert": {},
        },
    },
}

MOCK_POLLING_PERIOD = 15.0
MOCK_TRACE_TIMEOUT = 120
MOCK_HTTP_RESPONSE_DELAY = 0.1

# cloud profiles
# setting mocks for a 'default' nicely working cloud profile
# to test expected normal behavior state
MOCK_PROFILE_ID = "111111"
MOCK_PROFILE_EMAIL = "mockprofile@meross_lan.local"
MOCK_PROFILE_PASSWORD = "Avery.-Strangest?:001$%ò*"
MOCK_PROFILE_KEY = "abcdefghijklmnopq"
MOCK_PROFILE_TOKEN = "1234567890ABCDEF"
MOCK_PROFILE_CONFIG: mlc.ProfileConfigType = {
    mc.KEY_USERID_: MOCK_PROFILE_ID,
    mc.KEY_EMAIL: MOCK_PROFILE_EMAIL,
    mc.KEY_KEY: MOCK_PROFILE_KEY,
    mc.KEY_TOKEN: MOCK_PROFILE_TOKEN,
    mlc.CONF_ALLOW_MQTT_PUBLISH: True,
}

MOCK_PROFILE_MSS310_UUID = "00000000000000000000000000000001"
MOCK_PROFILE_MSS310_DEVNAME_STORED = "Cloud plug"
MOCK_PROFILE_MSS310_DEVNAME = "Smart plug"
MOCK_PROFILE_MSS310_DOMAIN = "mqtt-1.meross_lan.local"
MOCK_PROFILE_MSS310_RESERVEDDOMAIN = "mqtt-1.meross_lan.local"
MOCK_PROFILE_MSH300_UUID = "00000000000000000000000000000002"
MOCK_PROFILE_MSH300_DEVNAME = "Cloud smart hub"
MOCK_PROFILE_MSH300_DOMAIN = "mqtt-2.meross_lan.local"
MOCK_PROFILE_MSH300_RESERVEDDOMAIN = "mqtt-1.meross_lan.local"
MOCK_PROFILE_CLOUDAPI_DEVLIST: list[cloudapi.DeviceInfoType] = [
    {
        "uuid": MOCK_PROFILE_MSS310_UUID,
        "onlineStatus": 1,
        "devName": MOCK_PROFILE_MSS310_DEVNAME,
        "devIconId": "device045_it",
        "bindTime": 1677165116,
        "deviceType": mc.TYPE_MSS310,
        "subType": "it",
        "channels": [{}],
        "region": "eu",
        "fmwareVersion": "2.1.4",
        "hdwareVersion": "2.0.0",
        "userDevIcon": "",
        "iconType": 1,
        "cluster": 1,
        "domain": MOCK_PROFILE_MSS310_DOMAIN,
        "reservedDomain": MOCK_PROFILE_MSS310_RESERVEDDOMAIN,
    },
    {
        "uuid": MOCK_PROFILE_MSH300_UUID,
        "onlineStatus": 1,
        "devName": MOCK_PROFILE_MSH300_DEVNAME,
        "devIconId": "device045_it",
        "bindTime": 1677165116,
        "deviceType": mc.TYPE_MSH300,
        "subType": "it",
        "channels": [{}],
        "region": "eu",
        "fmwareVersion": "4.1.26",
        "hdwareVersion": "4.0.0",
        "userDevIcon": "",
        "iconType": 1,
        "cluster": 2,
        "domain": MOCK_PROFILE_MSH300_DOMAIN,
        "reservedDomain": MOCK_PROFILE_MSH300_RESERVEDDOMAIN,
    },
]
MOCK_PROFILE_CLOUDAPI_SUBDEVICE_DICT: dict[str, list[cloudapi.SubDeviceInfoType]] = {
    MOCK_PROFILE_MSH300_UUID: [
        {
            "subDeviceId": "00001234",
            "subDeviceType": mc.TYPE_MTS100V3,
            "subDeviceVendor": "Meross",
            "subDeviceName": "Awesome thermostat",
            "subDeviceIconId": "device045_it",
        },
        {
            "subDeviceId": "00001235",
            "subDeviceType": mc.TYPE_MS100,
            "subDeviceVendor": "Meross",
            "subDeviceName": "Nice temp/humidity outside",
            "subDeviceIconId": "device045_it",
        },
    ]
}
MOCK_PROFILE_STORE_KEY = f"{mlc.DOMAIN}.profile.{MOCK_PROFILE_ID}"
MOCK_PROFILE_STORE_DEVICEINFO_DICT: dict[str, cloudapi.DeviceInfoType] = {
    MOCK_PROFILE_MSS310_UUID: {
        "uuid": MOCK_PROFILE_MSS310_UUID,
        "onlineStatus": 1,
        "devName": MOCK_PROFILE_MSS310_DEVNAME_STORED,
        "devIconId": "device045_it",
        "bindTime": 1677165116,
        "deviceType": mc.TYPE_MSS310,
        "subType": "it",
        "channels": [{}],
        "region": "eu",
        "fmwareVersion": "2.1.4",
        "hdwareVersion": "2.0.0",
        "userDevIcon": "",
        "iconType": 1,
        "cluster": 1,
        "domain": MOCK_PROFILE_MSS310_DOMAIN,
        "reservedDomain": MOCK_PROFILE_MSS310_RESERVEDDOMAIN,
    }
}
MOCK_PROFILE_STORE = {
    "version": 1,
    "data": {
        mc.KEY_USERID_: MOCK_PROFILE_ID,
        mc.KEY_EMAIL: MOCK_PROFILE_EMAIL,
        mc.KEY_KEY: MOCK_PROFILE_KEY,
        mc.KEY_TOKEN: MOCK_PROFILE_TOKEN,
        "deviceInfo": MOCK_PROFILE_STORE_DEVICEINFO_DICT,
        "deviceInfoTime": 0,
    },
}
# storage could contain more than one cloud profiles.
# right now we just set our 'default' nice one
MOCK_PROFILE_STORAGE = {MOCK_PROFILE_STORE_KEY: MOCK_PROFILE_STORE}

EMULATOR_TRACES_PATH = "./emulator_traces/"
EMULATOR_TRACES_MAP = {
    mc.TYPE_MTS200: "U0123456789012345678901234567890C-Kpippo-mts200b-1674112759.csv",
    mc.TYPE_MSS310: "U0123456789012345678901234567890E-Kpippo-mss310r-1676020598.csv",
    mc.TYPE_MSH300: "U0123456789012345678901234567890F-Kpippo-msh300-1646299947.csv",
}
