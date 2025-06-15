from homeassistant.components import number as haec

from custom_components.meross_lan.cover import MLRollerShutterConfigNumber
from custom_components.meross_lan.devices.garageDoor import (
    MLGarageConfigNumber,
    MLGarageMultipleConfigNumber,
)
from custom_components.meross_lan.devices.hub import MLHubSensorAdjustNumber
from custom_components.meross_lan.devices.hub.mts100 import Mts100Climate
from custom_components.meross_lan.devices.thermostat.mts200 import Mts200Climate
from custom_components.meross_lan.devices.thermostat.mts300 import Mts300Climate
from custom_components.meross_lan.devices.thermostat.mts960 import Mts960Climate
from custom_components.meross_lan.devices.thermostat.mtsthermostat import (
    MLScreenBrightnessNumber,
    MtsCommonTemperatureExtNumber,
    MtsDeadZoneNumber,
    MtsFrostNumber,
    MtsOverheatNumber,
    MtsThermostatClimate,
    mn_t,
)
from custom_components.meross_lan.merossclient.protocol import (
    const as mc,
    namespaces as mn,
)
from custom_components.meross_lan.number import MLConfigNumber, MLNumber

from tests.entities import EntityComponentTest

_MTS100_ENTITES = [
    Mts100Climate.AdjustNumber,
    Mts100Climate.SetPointNumber,
    Mts100Climate.SetPointNumber,
    Mts100Climate.SetPointNumber,
]

class EntityTest(EntityComponentTest):

    ENTITY_TYPE = haec.NumberEntity

    DIGEST_ENTITIES = {
        mc.KEY_THERMOSTAT: {
            mc.KEY_MODE: [
                Mts200Climate.AdjustNumber,
                Mts200Climate.SetPointNumber,
                Mts200Climate.SetPointNumber,
                Mts200Climate.SetPointNumber,
            ],
            mc.KEY_MODEB: [
                Mts960Climate.AdjustNumber,
            ],
        },
    }
    NAMESPACES_ENTITIES = {
        mn.Appliance_GarageDoor_Config.name: [MLGarageConfigNumber],
        mn.Appliance_GarageDoor_MultipleConfig.name: [MLGarageMultipleConfigNumber],
        mn.Appliance_RollerShutter_Config.name: [
            MLRollerShutterConfigNumber,
            MLRollerShutterConfigNumber,
        ],
        mn.Appliance_Control_Screen_Brightness.name: [
            MLScreenBrightnessNumber,
            MLScreenBrightnessNumber,
        ],
        mn_t.Appliance_Control_Thermostat_Calibration.name: [
            MtsThermostatClimate.AdjustNumber
        ],
        mn_t.Appliance_Control_Thermostat_DeadZone.name: [MtsDeadZoneNumber],
        mn_t.Appliance_Control_Thermostat_Frost.name: [MtsFrostNumber],
        mn_t.Appliance_Control_Thermostat_ModeC.name: [Mts300Climate.AdjustNumber],
        mn_t.Appliance_Control_Thermostat_Overheat.name: [MtsOverheatNumber],
    }
    HUB_SUBDEVICES_ENTITIES = {
        mc.TYPE_MS100: [MLHubSensorAdjustNumber, MLHubSensorAdjustNumber],
        mc.TYPE_MTS100: _MTS100_ENTITES,
        mc.TYPE_MTS100V3: _MTS100_ENTITES,
        mc.TYPE_MTS150:_MTS100_ENTITES,
    }

    async def async_test_each_callback(self, entity: MLNumber):
        if isinstance(entity, MtsThermostatClimate.AdjustNumber):
            # This is intercept thermostat Calibration namespace requirement where
            # every MtsThermostatClimate descendant should instantiate
            # MtsThermostatClimate.AdjustNumber or a descendant
            EntityComponentTest.expected_entity_types.remove(
                MtsThermostatClimate.AdjustNumber
            )

        if isinstance(entity, MtsCommonTemperatureExtNumber):
            # rich temperatures are set to 'unavailable' when
            # the corresponding function is 'off'
            if switch := entity.switch:
                if not switch.is_on:
                    return
        await super().async_test_each_callback(entity)

    async def async_test_enabled_callback(self, entity: MLNumber):
        is_config_number = isinstance(entity, MLConfigNumber)
        states = self.hass_states
        time_mocker = self.device_context.time
        await self.async_service_call(
            haec.SERVICE_SET_VALUE, {haec.ATTR_VALUE: entity.max_value}
        )
        if is_config_number:
            await time_mocker.async_tick(entity.DEBOUNCE_DELAY)
        assert (state := states.get(self.entity_id))
        assert float(state.state) == entity.max_value, "max_value"
        await self.async_service_call(
            haec.SERVICE_SET_VALUE, {haec.ATTR_VALUE: entity.min_value}
        )
        if is_config_number:
            await time_mocker.async_tick(entity.DEBOUNCE_DELAY)
        assert (state := states.get(self.entity_id))
        assert float(state.state) == entity.min_value, "min_value"

    async def async_test_disabled_callback(self, entity: MLNumber):
        is_config_number = isinstance(entity, MLConfigNumber)
        time_mocker = self.device_context.time
        await entity.async_set_native_value(entity.native_max_value)
        if is_config_number:
            await time_mocker.async_tick(entity.DEBOUNCE_DELAY)
        assert entity.state == entity.max_value
        await entity.async_set_native_value(entity.native_min_value)
        if is_config_number:
            await time_mocker.async_tick(entity.DEBOUNCE_DELAY)
        assert entity.state == entity.min_value
