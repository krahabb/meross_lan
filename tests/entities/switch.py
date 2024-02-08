from homeassistant.components.switch import (
    DOMAIN,
    SERVICE_TURN_OFF,
    SERVICE_TURN_ON,
    SwitchEntity,
)
from homeassistant.helpers.entity import STATE_OFF, STATE_ON

from tests.entities import EntityComponentTest


class EntityTest(EntityComponentTest):

    ENTITY_TYPE = SwitchEntity

    async def async_test_each_callback(self, entity: SwitchEntity):
        pass

    async def async_test_enabled_callback(self, entity: SwitchEntity):
        state = await self.async_service_call(SERVICE_TURN_ON)
        assert state.state == STATE_ON
        state = await self.async_service_call(SERVICE_TURN_OFF)
        assert state.state == STATE_OFF

    async def async_test_disabled_callback(self, entity: SwitchEntity):
        await entity.async_turn_on()
        assert entity.is_on
        await entity.async_turn_off()
        assert not entity.is_on
