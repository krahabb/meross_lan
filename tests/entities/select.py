from homeassistant.components.select import (
    ATTR_OPTION,
    DOMAIN,
    SERVICE_SELECT_OPTION,
    SelectEntity,
)

from custom_components.meross_lan.merossclient import const as mc
from custom_components.meross_lan.select import MLSpray, MtsTrackedSensor

from tests.entities import EntityComponentTest


class EntityTest(EntityComponentTest):

    ENTITY_TYPE = SelectEntity

    DIGEST_ENTITIES = {
        mc.KEY_THERMOSTAT: {MtsTrackedSensor},
        mc.KEY_SPRAY: {MLSpray},
        mc.KEY_DIFFUSER: {MLSpray},
    }

    HUB_SUBDEVICES_ENTITIES = {
        mc.TYPE_MTS100: {MtsTrackedSensor},
        mc.TYPE_MTS150: {MtsTrackedSensor},
    }

    async def async_test_each_callback(self, entity: SelectEntity):
        pass

    async def async_test_enabled_callback(self, entity: SelectEntity):
        for option in entity.options:
            state = await self.async_service_call(
                SERVICE_SELECT_OPTION, {ATTR_OPTION: option}
            )
            assert state.state == option

    async def async_test_disabled_callback(self, entity: SelectEntity):
        for option in entity.options:
            await entity.async_select_option(option)
            assert entity.state == option
