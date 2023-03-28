"""Constants for integration_blueprint tests."""
from custom_components.meross_lan.const import CONF_DEVICE_ID, CONF_KEY, CONF_PAYLOAD
from custom_components.meross_lan.merossclient import const as mc

# Mock config data to be used across multiple tests
MOCK_DEVICE_UUID = "01234567890123456789001122334455"
MOCK_DEVICE_IP = '10.0.0.1'
MOCK_MACADDRESS = '48:e1:e9:aa:bb:cc'
MOCK_KEY = 'test_key'
MOCK_HUB_CONFIG = {
    CONF_KEY: MOCK_KEY
}
MOCK_DEVICE_CONFIG = {
    CONF_DEVICE_ID: "9109182170548290880048b1a9522933",
    CONF_KEY: MOCK_KEY,
    CONF_PAYLOAD: {
        mc.KEY_ALL : {"system": {"hardware": {"type": "msh300", "subType": "un", "version": "4.0.0", "chipType": "mt7686", "uuid": "9109182170548290880048b1a9522933", "macAddress": "aa:bb:cc:dd:ee:ff"}, "firmware": {"version": "4.1.26", "compileTime": "2020/11/07 16:29:31 GMT +08:00", "wifiMac": "aa:bb:cc:dd:ee:ff", "innerIp": "10.0.0.1", "server": "10.0.0.7", "port": "8883", "userId": "0"}, "time": {"timestamp": 1638365548, "timezone": "Europe/Rome", "timeRule": [[1635634800, 3600, 0], [1648339200, 7200, 1]]}, "online": {"status": 1}}, "digest": {"hub": {"hubId": -381895630, "mode": 0, "subdevice": [{"id": "120027D21C19", "status": 2}, {"id": "01008C11", "status": 1, "scheduleBMode": 6, "onoff": 1, "lastActiveTime": 1638365524, "mts100v3": {"mode": 0}}, {"id": "0100783A", "status": 1, "scheduleBMode": 6, "onoff": 1, "lastActiveTime": 1638365410, "mts100v3": {"mode": 0}}]}}},
        mc.KEY_ABILITY : {"Appliance.Config.Key": {}, "Appliance.Config.WifiList": {}, "Appliance.Config.Wifi": {}, "Appliance.Config.Trace": {}, "Appliance.System.All": {}, "Appliance.System.Hardware": {}, "Appliance.System.Firmware": {}, "Appliance.System.Debug": {}, "Appliance.System.Online": {}, "Appliance.System.Time": {}, "Appliance.System.Ability": {}, "Appliance.System.Runtime": {}, "Appliance.System.Report": {}, "Appliance.System.Position": {}, "Appliance.System.DNDMode": {}, "Appliance.Control.Multiple": {"maxCmdNum": 5}, "Appliance.Control.Bind": {}, "Appliance.Control.Unbind": {}, "Appliance.Control.Upgrade": {}, "Appliance.Digest.Hub": {}, "Appliance.Hub.Online": {}, "Appliance.Hub.ToggleX": {}, "Appliance.Hub.Exception": {}, "Appliance.Hub.SubdeviceList": {}, "Appliance.Hub.Report": {}, "Appliance.Hub.Battery": {}, "Appliance.Hub.Mts100.All": {}, "Appliance.Hub.Mts100.TimeSync": {}, "Appliance.Hub.Mts100.Mode": {}, "Appliance.Hub.Mts100.Temperature": {}, "Appliance.Hub.Mts100.Adjust": {}, "Appliance.Hub.Mts100.Schedule": {"scheduleUnitTime": 30}, "Appliance.Hub.Mts100.ScheduleB": {"scheduleUnitTime": 15}, "Appliance.Hub.Sensor.All": {}, "Appliance.Hub.Sensor.Latest": {}, "Appliance.Hub.Sensor.TempHum": {}, "Appliance.Hub.Sensor.Adjust": {}, "Appliance.Hub.Sensor.Alert": {}}
    }
}
MOCK_POLLING_PERIOD = 15.0
MOCK_HTTP_RESPONSE_DELAY = 0.1

EMULATOR_TRACES_PATH = "./emulator_traces/"
EMULATOR_TRACES_MAP = {
    mc.TYPE_MTS200: "mts200b-1674112759-U0123456789012345678901234567890C-Kpippo.csv",
    mc.TYPE_MSS310: "mss310r-1676020598-U0123456789012345678901234567890E-Kpippo.csv",
}
