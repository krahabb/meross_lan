from homeassistant import const as hac
from homeassistant.components import siren as haec

from custom_components.meross_lan import siren
from custom_components.meross_lan.merossclient.protocol import (
    namespaces as mn,
)

from tests.entities import ToggleEntityComponentTest


class EntityTest(ToggleEntityComponentTest):

    ENTITY_TYPE = haec.SirenEntity

    DIGEST_ENTITIES = {}

    NAMESPACES_ENTITIES = {
        mn.Appliance_Control_Alarm: [siren.Siren],
    }

    async def async_test_enabled_callback(self, entity: siren.Siren):
        await super().async_test_enabled_callback(entity)

    async def async_test_disabled_callback(self, entity: siren.Siren):
        await super().async_test_disabled_callback(entity)
