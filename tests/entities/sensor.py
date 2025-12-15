from homeassistant.components import sensor as haec

from custom_components.meross_lan.devices.mss import (
    ConsumptionHSensor,
    ConsumptionXSensor,
    ElectricitySensor,
    ElectricityXSensor,
)
from custom_components.meross_lan.merossclient.protocol import (
    const as mc,
    namespaces as mn,
)
from custom_components.meross_lan.merossclient.protocol.namespaces import (
    thermostat as mn_t,
)
from custom_components.meross_lan.sensor import (
    MLEnumSensor,
    MLFilterMaintenanceSensor,
    MLHumiditySensor,
    MLLightSensor,
    MLNumericSensor,
    MLSignalStrengthSensor,
    MLTemperatureSensor,
    ProtocolSensor,
)

from tests.entities import EntityComponentTest


class EntityTest(EntityComponentTest):

    ENTITY_TYPE = haec.SensorEntity

    DEVICE_ENTITIES = [ProtocolSensor]

    DIGEST_ENTITIES = {
        mc.KEY_THERMOSTAT: {
            mc.KEY_MODE: [
                MLTemperatureSensor
            ],  # additional (disabled) current temperature sensor
            mc.KEY_MODEB: [
                MLTemperatureSensor
            ],  # additional (disabled) current temperature sensor
        },
    }

    NAMESPACES_ENTITIES = {
        mn.Appliance_Config_OverTemp.name: [MLEnumSensor],
        mn.Appliance_Control_ConsumptionH.name: [ConsumptionHSensor],
        mn.Appliance_Control_ConsumptionX.name: [ConsumptionXSensor],
        mn.Appliance_Control_Diffuser_Sensor.name: [
            MLHumiditySensor,
            MLTemperatureSensor,
        ],
        mn.Appliance_Control_Electricity.name: [
            ElectricitySensor,
            MLNumericSensor,
            MLNumericSensor,
            MLNumericSensor,
        ],
        mn.Appliance_Control_ElectricityX.name: [
            # There's an issue in removing 'ElectricityXSensor' when
            # the code in '_async_test_entities' should remove
            # this class from 'expected_entities'
            # ElectricityXSensor,
            *(
                [
                    _sensor_def[0]
                    for _sensor_def in ElectricityXSensor.SENSOR_DEFS.values()
                ]
            ),
        ]
        * 6,  # em06 has 6 channels but we might need a better approach for other supporting devices
        mn.Appliance_Control_FilterMaintenance.name: [MLFilterMaintenanceSensor],
        mn_t.Appliance_Control_Thermostat_ModeC.name: [  # mts300
            MLEnumSensor,  # output status sensors
            MLEnumSensor,
            MLEnumSensor,
            MLEnumSensor,
            MLEnumSensor,
            MLTemperatureSensor,  # additional (disabled) current temperature sensor
            MLHumiditySensor,  # additional (disabled) current humidity sensor
        ],
        mn_t.Appliance_Control_Thermostat_Overheat.name: [MLTemperatureSensor],
        mn.Appliance_Control_Sensor_Latest.name: [
            MLHumiditySensor
        ],  # mts200 (some models)
        mn.Appliance_System_Runtime.name: [MLSignalStrengthSensor],  # Signal strength
    }

    HUB_SUBDEVICES_ENTITIES = {
        None: [MLNumericSensor],  # battery sensor
        mc.TYPE_MS100: [MLHumiditySensor, MLTemperatureSensor],
        mc.KEY_TEMPHUMI: [MLHumiditySensor, MLTemperatureSensor, MLLightSensor],
        mc.TYPE_MTS100: [
            MLTemperatureSensor
        ],  # additional (disabled) current temperature sensor
        mc.TYPE_MTS100V3: [
            MLTemperatureSensor
        ],  # additional (disabled) current temperature sensor
        mc.TYPE_MTS150: [
            MLTemperatureSensor
        ],  # additional (disabled) current temperature sensor
        mc.KEY_SMOKEALARM: [MLEnumSensor, MLEnumSensor],  # status, interConn sensors
    }

    async def async_test_enabled_callback(self, entity: MLEnumSensor | MLNumericSensor):
        pass

    async def async_test_disabled_callback(
        self, entity: MLEnumSensor | MLNumericSensor
    ):
        pass
