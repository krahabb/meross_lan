from homeassistant.components import sensor as haec

from custom_components.meross_lan.devices.mss import (
    ConsumptionXSensor,
    ElectricitySensor,
)
from custom_components.meross_lan.merossclient import const as mc, namespaces as mn
from custom_components.meross_lan.sensor import (
    MLEnumSensor,
    MLFilterMaintenanceSensor,
    MLHumiditySensor,
    MLNumericSensor,
    MLSignalStrengthSensor,
    MLTemperatureSensor,
    ProtocolSensor,
)

from tests.entities import EntityComponentTest


class EntityTest(EntityComponentTest):

    ENTITY_TYPE = haec.SensorEntity

    DEVICE_ENTITIES = [ProtocolSensor]

    DIGEST_ENTITIES = {}

    NAMESPACES_ENTITIES = {
        mn.Appliance_Config_OverTemp.name: [MLEnumSensor],
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
        mn.Appliance_Control_FilterMaintenance.name: [MLFilterMaintenanceSensor],
        mn.Appliance_Control_Thermostat_Overheat.name: [MLTemperatureSensor],
        mn.Appliance_Control_Sensor_Latest.name: [MLHumiditySensor],
        mn.Appliance_System_Runtime.name: [MLSignalStrengthSensor],  # Signal strength
    }

    HUB_SUBDEVICES_ENTITIES = {
        mc.TYPE_MS100: [MLHumiditySensor, MLTemperatureSensor],
        mc.KEY_TEMPHUMI: [MLHumiditySensor, MLTemperatureSensor],
        mc.TYPE_MTS100: [MLTemperatureSensor],
        mc.TYPE_MTS100V3: [MLTemperatureSensor],
        mc.TYPE_MTS150: [MLTemperatureSensor],
        mc.KEY_SMOKEALARM: [MLEnumSensor, MLEnumSensor],  # status, interConn sensors
    }

    async def async_test_enabled_callback(self, entity: MLEnumSensor | MLNumericSensor):
        pass

    async def async_test_disabled_callback(
        self, entity: MLEnumSensor | MLNumericSensor
    ):
        pass
