from homeassistant.components import fan as haec

from custom_components.meross_lan.fan import MLFan
from custom_components.meross_lan.merossclient.protocol import (
    const as mc,
    namespaces as mn,
)

from tests.entities import EntityComponentTest


class EntityTest(EntityComponentTest):

    ENTITY_TYPE = haec.FanEntity

    DIGEST_ENTITIES = {}

    NAMESPACES_ENTITIES = {
        mn.Appliance_Control_Fan.name: [MLFan],
    }

    async def async_test_each_callback(self, entity: MLFan):
        await super().async_test_each_callback(entity)
        assert entity.speed_count, "speed_count"
        self._check_remove_togglex(entity)

    async def async_test_enabled_callback(self, entity: MLFan):
        speed_count = entity.speed_count
        for speed in range(0, speed_count):
            percentage = round(speed * 100 / speed_count)
            state = await self.async_service_call(
                haec.SERVICE_SET_PERCENTAGE, {haec.ATTR_PERCENTAGE: percentage}
            )
            assert state.attributes[haec.ATTR_PERCENTAGE] == percentage, "percentage"

    async def async_test_disabled_callback(self, entity: MLFan):
        pass
