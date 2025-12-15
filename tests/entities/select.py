from homeassistant.components import select as haec

from custom_components.meross_lan.climate import MtsClimate
from custom_components.meross_lan.devices.diffuser import MLDiffuserSpray
from custom_components.meross_lan.devices.spray import MLSpray
from custom_components.meross_lan.devices.thermostat.mts300 import Mts300Climate
from custom_components.meross_lan.devices.thermostat.mtsthermostat import (
    MtsHoldAction,
    MtsTempUnit,
    mn,
    mn_t,
)
from custom_components.meross_lan.merossclient.protocol import const as mc
from custom_components.meross_lan.select import MLSelect

from tests.entities import EntityComponentTest


class EntityTest(EntityComponentTest):

    ENTITY_TYPE = haec.SelectEntity

    DIGEST_ENTITIES = {
        mc.KEY_THERMOSTAT: {
            mc.KEY_MODE: [MtsClimate.TrackSensorSelect],
            mc.KEY_MODEB: [MtsClimate.TrackSensorSelect],
        },
        mc.KEY_SPRAY: [MLSpray],
        mc.KEY_DIFFUSER: {mc.KEY_SPRAY: [MLDiffuserSpray]},
    }
    NAMESPACES_ENTITIES = {
        mn.Appliance_Config_Sensor_Association.name: [
            Mts300Climate.SensorAssociationSelect
        ],
        mn.Appliance_Control_TempUnit.name: [MtsTempUnit],
        mn_t.Appliance_Control_Thermostat_HoldAction.name: [MtsHoldAction],
        mn_t.Appliance_Control_Thermostat_ModeC.name: [MtsClimate.TrackSensorSelect],
    }
    HUB_SUBDEVICES_ENTITIES = {
        mc.TYPE_MTS100: [MtsClimate.TrackSensorSelect],
        mc.TYPE_MTS100V3: [MtsClimate.TrackSensorSelect],
        mc.TYPE_MTS150: [MtsClimate.TrackSensorSelect],
    }

    async def async_test_enabled_callback(self, entity: MLSelect):
        for option in entity.options:
            state = await self.async_service_call(
                haec.SERVICE_SELECT_OPTION, {haec.ATTR_OPTION: option}
            )
            assert state.state == option

    async def async_test_disabled_callback(self, entity: MLSelect):
        for option in entity.options:
            await entity.async_select_option(option)
            assert entity.state == option
