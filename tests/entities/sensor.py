from homeassistant.components import sensor as haec

from custom_components.meross_lan.devices.mss import (
    ConsumptionXSensor,
    EnergyEstimateSensor,
)
from custom_components.meross_lan.merossclient import const as mc
from custom_components.meross_lan.sensor import (
    MLDiagnosticSensor,
    MLEnumSensor,
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
        mc.NS_APPLIANCE_CONFIG_OVERTEMP: [MLEnumSensor],
        mc.NS_APPLIANCE_CONTROL_CONSUMPTIONX: [ConsumptionXSensor],
        mc.NS_APPLIANCE_CONTROL_DIFFUSER_SENSOR: [
            MLHumiditySensor,
            MLTemperatureSensor,
        ],
        mc.NS_APPLIANCE_CONTROL_ELECTRICITY: [EnergyEstimateSensor, MLNumericSensor, MLNumericSensor, MLNumericSensor],
        mc.NS_APPLIANCE_CONTROL_THERMOSTAT_OVERHEAT: [MLTemperatureSensor],
        mc.NS_APPLIANCE_SYSTEM_RUNTIME: [MLSignalStrengthSensor],  # Signal strength
    }

    HUB_SUBDEVICES_ENTITIES = {
        mc.TYPE_MS100: [MLHumiditySensor, MLTemperatureSensor],
        mc.TYPE_MTS100: [MLTemperatureSensor],
        mc.TYPE_MTS100V3: [MLTemperatureSensor],
        mc.TYPE_MTS150: [MLTemperatureSensor],
        mc.KEY_SMOKEALARM: [MLEnumSensor, MLEnumSensor],  # status, interConn sensors
    }

    async def async_test_each_callback(self, entity: MLEnumSensor | MLNumericSensor):
        pass

    async def async_test_enabled_callback(self, entity: MLEnumSensor | MLNumericSensor):
        pass

    async def async_test_disabled_callback(
        self, entity: MLEnumSensor | MLNumericSensor
    ):
        pass
