from homeassistant.components import button as habc
from homeassistant.util import dt as dt_util

from custom_components.meross_lan.button import Button, PersistentButton
from custom_components.meross_lan.merossclient.protocol import const as mc
from custom_components.meross_lan.merossclient.protocol.namespaces import (
    hub as mn_h,
)

from tests.entities import EntityComponentTest


class EntityTest(EntityComponentTest):

    ENTITY_TYPE = habc.ButtonEntity

    DEVICE_ENTITIES = [PersistentButton, PersistentButton]

    DIGEST_ENTITIES = {}

    NAMESPACES_ENTITIES = {
        mn_h.Appliance_Hub_PairSubDev: [Button],  # pair subdevice button
    }

    HUB_SUBDEVICES_ENTITIES = {
        mc.KEY_SMOKEALARM: [Button, Button],  # mute, test
    }

    async def async_test_enabled_callback(self, entity: Button):
        # TODO: test each expected outcome according to the button type

        if entity.entity_key in ("button_refresh", "button_reload"):
            return  # skip reload button testing

        await self.async_service_call_check(
            habc.SERVICE_PRESS, dt_util.utcnow().isoformat()
        )

    async def async_test_disabled_callback(self, entity: Button):

        if entity.entity_key in ("button_refresh", "button_reload"):
            return  # skip reload button testing

        await entity.async_press()
