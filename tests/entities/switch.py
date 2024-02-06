from homeassistant.components.switch import (
    DOMAIN,
    SERVICE_TURN_OFF,
    SERVICE_TURN_ON,
    SwitchEntity,
)
from homeassistant.helpers.entity import STATE_OFF, STATE_ON

from tests.entities import EntityComponentTest, EntityTestContext


class EntityTest(EntityComponentTest):

    ENTITY_TYPE = SwitchEntity

    async def async_test_each_callback(
        self, context: EntityTestContext, entity: SwitchEntity
    ):
        pass

    async def async_test_enabled_callback(
        self, context: EntityTestContext, entity: SwitchEntity, entity_id: str
    ):
        hass = context.hass
        call_service = hass.services.async_call
        states = hass.states
        await call_service(
            DOMAIN,
            SERVICE_TURN_ON,
            service_data={
                "entity_id": entity_id,
            },
            blocking=True,
        )
        state = states.get(entity_id)
        assert state and state.state == STATE_ON
        await call_service(
            DOMAIN,
            SERVICE_TURN_OFF,
            service_data={
                "entity_id": entity_id,
            },
            blocking=True,
        )
        state = states.get(entity_id)
        assert state and state.state == STATE_OFF

    async def async_test_disabled_callback(
        self, context: EntityTestContext, entity: SwitchEntity
    ):
        await entity.async_turn_on()
        assert entity.is_on
        await entity.async_turn_off()
        assert not entity.is_on
