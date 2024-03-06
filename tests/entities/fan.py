from homeassistant.components import fan as haec

from custom_components.meross_lan.fan import MLFan
from custom_components.meross_lan.merossclient import const as mc
from custom_components.meross_lan.switch import MLSwitch

from tests.entities import EntityComponentTest


class EntityTest(EntityComponentTest):

    ENTITY_TYPE = haec.FanEntity

    DIGEST_ENTITIES = {}

    NAMESPACES_ENTITIES = {
        mc.NS_APPLIANCE_CONTROL_FAN: [MLFan],
    }

    async def async_test_each_callback(self, entity: MLFan):
        assert entity.speed_count, "speed_count"
        EntityComponentTest.expected_entity_types.remove(MLSwitch)

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
