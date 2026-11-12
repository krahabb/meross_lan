from homeassistant.components import button as habc
from homeassistant.util import dt as dt_util

from custom_components.meross_lan.button import Button, PersistentButton
from custom_components.meross_lan.merossclient.protocol import const as mc
from custom_components.meross_lan.merossclient.protocol.namespaces import (
    hub as mn_h,
    thermostat as mn_t,
)
from custom_components.meross_lan.schedule_sync import (
    SchedulePullButton,
    SchedulePushButton,
)

from tests.entities import EntityComponentTest


class EntityTest(EntityComponentTest):

    ENTITY_TYPE = habc.ButtonEntity

    DEVICE_ENTITIES = [PersistentButton, PersistentButton]

    # schedule helper sync (tested in test_schedule_sync/test_schedule_services)
    SCHEDULE_BUTTONS = (SchedulePullButton, SchedulePushButton)

    DIGEST_ENTITIES = {
        mc.KEY_THERMOSTAT: {
            mc.KEY_MODE: [*SCHEDULE_BUTTONS],
            mc.KEY_MODEB: [*SCHEDULE_BUTTONS],
        },
    }

    NAMESPACES_ENTITIES = {
        mn_h.Appliance_Hub_PairSubDev: [Button],  # pair subdevice button
        mn_t.Appliance_Control_Thermostat_ModeC: [*SCHEDULE_BUTTONS],  # mts300
    }

    HUB_SUBDEVICES_ENTITIES = {
        mc.KEY_SMOKEALARM: [Button, Button],  # mute, test
        mc.TYPE_MTS100: [*SCHEDULE_BUTTONS],
        mc.TYPE_MTS100V3: [*SCHEDULE_BUTTONS],
        mc.TYPE_MTS150: [*SCHEDULE_BUTTONS],
    }

    async def async_test_enabled_callback(self, entity: Button):
        # TODO: test each expected outcome according to the button type

        if entity.entity_key in ("button_refresh", "button_reload"):
            return  # skip reload button testing
        if isinstance(entity, self.SCHEDULE_BUTTONS):
            return

        await self.async_service_call_check(
            habc.SERVICE_PRESS, dt_util.utcnow().isoformat()
        )

    async def async_test_disabled_callback(self, entity: Button):

        if entity.entity_key in ("button_refresh", "button_reload"):
            return  # skip reload button testing
        if isinstance(entity, self.SCHEDULE_BUTTONS):
            return

        await entity.async_press()
