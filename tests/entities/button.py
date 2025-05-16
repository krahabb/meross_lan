from homeassistant.components import button as habc
from homeassistant.util import dt as dt_util

from custom_components.meross_lan.merossclient import const as mc, namespaces as mn
from custom_components.meross_lan.button import MLButton, MLPersistentButton

from tests.entities import EntityComponentTest


class EntityTest(EntityComponentTest):

    ENTITY_TYPE = habc.ButtonEntity

    DEVICE_ENTITIES = [MLPersistentButton, MLPersistentButton]

    DIGEST_ENTITIES = {}

    NAMESPACES_ENTITIES = {}

    HUB_SUBDEVICES_ENTITIES = {}

    async def async_test_enabled_callback(self, entity: MLButton):
        # We'll patch the hard way so that pressing thse buttons has no consequences.
        # Specific buttons behaviors are to be tested in dedicated code.
        old_handler = entity.async_press
        pressed = False

        async def _async_press():
            nonlocal pressed
            pressed = True

        entity.async_press = _async_press
        try:
            await self.async_service_call_check(
                habc.SERVICE_PRESS, dt_util.utcnow().isoformat()
            )
            assert pressed, ("button was not pressed", self.entity_id)
        finally:
            entity.async_press = old_handler

    async def async_test_disabled_callback(self, entity: MLButton):
        pass
