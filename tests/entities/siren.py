from homeassistant import const as hac
from homeassistant.components import siren as haec

from custom_components.meross_lan import siren
from custom_components.meross_lan.merossclient.protocol import (
    namespaces as mn,
)

from tests.entities import EntityComponentTest


class EntityTest(EntityComponentTest):

    ENTITY_TYPE = haec.SirenEntity

    DIGEST_ENTITIES = {}

    NAMESPACES_ENTITIES = {
        mn.Appliance_Control_Alarm: [siren.Siren],
    }

    async def async_test_enabled_callback(self, entity: siren.Siren):
        await self.async_service_call_check(haec.SERVICE_TURN_ON, hac.STATE_ON)
        await self.async_service_call_check(haec.SERVICE_TURN_OFF, hac.STATE_OFF)

    async def async_test_disabled_callback(self, entity: siren.Siren):
        await entity.async_turn_on()
        assert entity.is_on
        await entity.async_turn_off()
        assert not entity.is_on
