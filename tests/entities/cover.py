from homeassistant.components.cover import (
    ATTR_POSITION,
    DOMAIN,
    SERVICE_CLOSE_COVER,
    SERVICE_OPEN_COVER,
    SERVICE_SET_COVER_POSITION,
    SERVICE_STOP_COVER,
    CoverEntity,
)

from custom_components.meross_lan.cover import MLGarage, MLRollerShutter
from custom_components.meross_lan.merossclient import const as mc

from tests.entities import EntityComponentTest


class EntityTest(EntityComponentTest):

    ENTITY_TYPE = CoverEntity

    DIGEST_ENTITIES = {
        mc.KEY_GARAGEDOOR: {MLGarage},
    }

    NAMESPACES_ENTITIES = {
        mc.NS_APPLIANCE_ROLLERSHUTTER_STATE: {MLRollerShutter},
    }

    async def async_test_each_callback(self, entity: CoverEntity):
        pass

    async def async_test_enabled_callback(self, entity: CoverEntity):
        pass

    async def async_test_disabled_callback(self, entity: CoverEntity):
        pass
