from homeassistant.components import select as haec

from custom_components.meross_lan.devices.diffuser import MLDiffuserSpray
from custom_components.meross_lan.devices.spray import MLSpray
from custom_components.meross_lan.merossclient.protocol import const as mc
from custom_components.meross_lan.select import MLSelect, MtsTrackedSensor

from tests.entities import EntityComponentTest


class EntityTest(EntityComponentTest):

    ENTITY_TYPE = haec.SelectEntity

    DIGEST_ENTITIES = {
        mc.KEY_THERMOSTAT: {
            mc.KEY_MODE: [MtsTrackedSensor],
            mc.KEY_MODEB: [MtsTrackedSensor],
        },
        mc.KEY_SPRAY: [MLSpray],
        mc.KEY_DIFFUSER: {mc.KEY_SPRAY: [MLDiffuserSpray]},
    }

    HUB_SUBDEVICES_ENTITIES = {
        mc.TYPE_MTS100: [MtsTrackedSensor],
        mc.TYPE_MTS100V3: [MtsTrackedSensor],
        mc.TYPE_MTS150: [MtsTrackedSensor],
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
