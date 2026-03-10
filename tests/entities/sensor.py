from homeassistant.components import sensor as haec

from custom_components.meross_lan.devices import hub, ms600, mss
from custom_components.meross_lan.merossclient.protocol import (
    const as mc,
    namespaces as mn,
)
from custom_components.meross_lan.merossclient.protocol.namespaces import (
    thermostat as mn_t,
)
from custom_components.meross_lan.sensor import (
    EnumParser,
    FilterMaintenanceSensor,
    HumiditySensor,
    LightSensor,
    SensorParser,
    ProtocolSensor,
    SignalStrengthSensor,
    TemperatureSensor,
)

from tests.entities import EntityComponentTest


class EntityTest(EntityComponentTest):

    ENTITY_TYPE = haec.SensorEntity

    DEVICE_ENTITIES = [ProtocolSensor]

    DIGEST_ENTITIES = {
        mc.KEY_THERMOSTAT: {
            mc.KEY_MODE: [
                TemperatureSensor
            ],  # additional (disabled) current temperature sensor
            mc.KEY_MODEB: [
                TemperatureSensor
            ],  # additional (disabled) current temperature sensor
        },
    }

    NAMESPACES_ENTITIES = {
        mn.Appliance_Config_OverTemp: [EnumParser],
        mn.Appliance_Control_ConsumptionH: [mss.ConsumptionHSensor],
        mn.Appliance_Control_ConsumptionX: [mss.ConsumptionXSensor],
        mn.Appliance_Control_Diffuser_Sensor: [
            HumiditySensor,
            TemperatureSensor,
        ],
        mn.Appliance_Control_Electricity: [
            mss.ElectricitySensor,
            SensorParser,
            SensorParser,
            SensorParser,
        ],
        mn.Appliance_Control_ElectricityX: [
            # There's an issue in removing 'ElectricityXSensor' when
            # the code in '_async_test_entities' should remove
            # this class from 'expected_entities'
            mss.ElectricityXSensor,
            *(
                _entity_def.type
                for _entity_def in mss.ElectricityXSensor.ENTITY_DEFS.values()
            ),
        ],
        mn.Appliance_Control_FilterMaintenance: [FilterMaintenanceSensor],
        mn.Appliance_Control_Presence_Config: [
            # These are entities installed by mn.Appliance_Control_Sensor_LatestX
            # but we use this namespace to detect presence capability (ms600)
            ms600.PresenceSensor,
            ms600.SensorParser,
            ms600.SensorParser,
            LightSensor,
        ],
        mn_t.Appliance_Control_Thermostat_ModeC: [  # mts300
            EnumParser,  # output status sensors
            EnumParser,
            EnumParser,
            EnumParser,
            EnumParser,
            TemperatureSensor,  # additional (disabled) current temperature sensor
            HumiditySensor,  # additional (disabled) current humidity sensor
        ],
        mn_t.Appliance_Control_Thermostat_Overheat: [TemperatureSensor],
        mn.Appliance_Control_Sensor_Latest: [HumiditySensor],  # mts200 (some models)
        mn.Appliance_System_Runtime: [SignalStrengthSensor],
    }

    HUB_SUBDEVICES_ENTITIES = {
        None: [hub.SubDevice],  # actual implementation of battery sensor
        mc.TYPE_MS100: [hub.MS100Sensor, HumiditySensor],
        mc.KEY_TEMPHUMI: [hub.MS130Sensor, HumiditySensor, LightSensor],
        mc.TYPE_MTS100: [
            TemperatureSensor
        ],  # additional (disabled) current temperature sensor
        mc.TYPE_MTS100V3: [
            TemperatureSensor
        ],  # additional (disabled) current temperature sensor
        mc.TYPE_MTS150: [
            TemperatureSensor
        ],  # additional (disabled) current temperature sensor
        mc.KEY_SMOKEALARM: [
            hub.SmokeAlarmSensor,
            EnumParser,
        ],  # status, interConn sensors
    }

    async def async_test_enabled_callback(self, entity: EnumParser | SensorParser):
        pass

    async def async_test_disabled_callback(self, entity: EnumParser | SensorParser):
        pass
