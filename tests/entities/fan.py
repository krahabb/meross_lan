from homeassistant import const as hac
from homeassistant.components import fan as haec

from custom_components.meross_lan.fan import Fan
from custom_components.meross_lan.merossclient.protocol import (
    const as mc,
    namespaces as mn,
)

from tests.entities import ToggleEntityComponentTest


class EntityTest(ToggleEntityComponentTest):

    ENTITY_TYPE = haec.FanEntity

    DIGEST_ENTITIES = {}

    NAMESPACES_ENTITIES = {
        mn.Appliance_Control_Fan: [Fan],
    }

    async def async_test_each_callback(self, entity: Fan):
        await super().async_test_each_callback(entity)
        assert entity.speed_count, "speed_count"
        self._check_remove_togglex(entity)

    async def async_test_enabled_callback(self, entity: Fan):
        await super().async_test_enabled_callback(entity)

        speed_count = entity.speed_count
        for speed in range(0, speed_count):
            percentage = round(speed * 100 / speed_count)
            state = await self.async_service_call(
                haec.SERVICE_SET_PERCENTAGE, {haec.ATTR_PERCENTAGE: percentage}
            )
            assert state.attributes[haec.ATTR_PERCENTAGE] == percentage, "percentage"

        await self.async_service_call_check(haec.SERVICE_TURN_OFF, hac.STATE_OFF)
