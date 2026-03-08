from homeassistant.components import button as habc
from homeassistant.util import dt as dt_util

from custom_components.meross_lan.button import Button, PersistentButton
from custom_components.meross_lan.merossclient.protocol import const as mc

from tests.entities import EntityComponentTest


class EntityTest(EntityComponentTest):

    ENTITY_TYPE = habc.ButtonEntity

    DEVICE_ENTITIES = [PersistentButton, PersistentButton]

    DIGEST_ENTITIES = {}

    NAMESPACES_ENTITIES = {}

    HUB_SUBDEVICES_ENTITIES = {
        mc.KEY_SMOKEALARM: [Button, Button],  # mute, test
    }

    async def async_test_enabled_callback(self, entity: Button):
        # TODO: test each expected outcome according to the button type

        if entity.entitykey in ("button_refresh", "button_reload"):
            return  # skip reload button testing

        await self.async_service_call_check(
            habc.SERVICE_PRESS, dt_util.utcnow().isoformat()
        )

    async def async_test_disabled_callback(self, entity: Button):

        if entity.entitykey in ("button_refresh", "button_reload"):
            return  # skip reload button testing

        await entity.async_press()
