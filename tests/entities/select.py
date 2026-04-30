from homeassistant.components import select as haec

from custom_components.meross_lan import climate, select
from custom_components.meross_lan.devices import ms600
from custom_components.meross_lan.devices.diffuser import DiffuserSpray
from custom_components.meross_lan.devices.spray import Spray
from custom_components.meross_lan.devices.thermostat import (
    MtsHoldAction,
    MtsTempUnit,
    mn,
    mn_t,
)
from custom_components.meross_lan.devices.thermostat.mts300 import Mts300Climate
from custom_components.meross_lan.merossclient.protocol import const as mc

from tests.entities import EntityComponentTest


class EntityTest(EntityComponentTest):

    ENTITY_TYPE = haec.SelectEntity

    DIGEST_ENTITIES = {
        mc.KEY_THERMOSTAT: {
            mc.KEY_MODE: [climate.MtsClimate.TrackSensorSelect],
            mc.KEY_MODEB: [climate.MtsClimate.TrackSensorSelect],
        },
        mc.KEY_SPRAY: [Spray],
        mc.KEY_DIFFUSER: {mc.KEY_SPRAY: [DiffuserSpray]},
    }
    NAMESPACES_ENTITIES = {
        mn.Appliance_Config_Alarm: [select.SelectParser],
        mn.Appliance_Config_Sensor_Association: [Mts300Climate.SensorAssociationSelect],
        mn.Appliance_Control_TempUnit: [MtsTempUnit],
        mn.Appliance_Control_Presence_Config: [
            ms600.PresenceConfigMode,  # workmode
            ms600.PresenceConfigMode,  # testmode
            ms600.SelectParser,  # sensitivity
        ],
        mn_t.Appliance_Control_Thermostat_HoldAction: [MtsHoldAction],
        mn_t.Appliance_Control_Thermostat_ModeC: [climate.MtsClimate.TrackSensorSelect],
    }
    HUB_SUBDEVICES_ENTITIES = {
        mc.TYPE_MTS100: [climate.MtsClimate.TrackSensorSelect],
        mc.TYPE_MTS100V3: [climate.MtsClimate.TrackSensorSelect],
        mc.TYPE_MTS150: [climate.MtsClimate.TrackSensorSelect],
        mc.KEY_TEMPHUMI: [
            select.SelectParser,  # timeCfg_am
            select.SelectParser,  # ms130Cfg_bl_bri
            select.SelectParser,  # unitCfg_tempUnit
        ],
    }

    async def async_test_enabled_callback(self, entity: haec.SelectEntity):
        for option in entity.options:
            state = await self.async_service_call(
                haec.SERVICE_SELECT_OPTION, {haec.ATTR_OPTION: option}
            )
            assert state.state == option

    async def async_test_disabled_callback(self, entity: haec.SelectEntity):
        for option in entity.options:
            await entity.async_select_option(option)
            assert entity.state == option
