from homeassistant.components.calendar import CalendarEntity

from custom_components.meross_lan.calendar import MtsSchedule
from custom_components.meross_lan.devices.hub.mts100 import Mts100Climate
from custom_components.meross_lan.devices.thermostat.mts200 import Mts200Climate
from custom_components.meross_lan.devices.thermostat.mts300 import Mts300Climate
from custom_components.meross_lan.devices.thermostat.mts960 import Mts960Climate
from custom_components.meross_lan.merossclient.protocol import const as mc
from custom_components.meross_lan.merossclient.protocol.namespaces import (
    thermostat as mn_t,
)

from tests.entities import EntityComponentTest


class EntityTest(EntityComponentTest):

    ENTITY_TYPE = CalendarEntity

    NAMESPACES_ENTITIES = {
        mn_t.Appliance_Control_Thermostat_Schedule.name: [Mts200Climate.Schedule],
        mn_t.Appliance_Control_Thermostat_ScheduleB.name: [
            Mts960Climate.Schedule,
            Mts300Climate.Schedule,
        ],
    }

    HUB_SUBDEVICES_ENTITIES = {
        mc.TYPE_MTS100: [Mts100Climate.Schedule],
        mc.TYPE_MTS100V3: [Mts100Climate.Schedule],
        mc.TYPE_MTS150: [Mts100Climate.Schedule],
    }

    async def async_test_each_callback(self, entity: MtsSchedule):
        # TODO: add more test for calendar platform(s)
        await super().async_test_each_callback(entity)
        if type(entity) is Mts100Climate.Schedule:
            # these are not set in stone though, but always appear like these in our traces
            assert entity._schedule_entry_count_max == 6, "schedule_entry_count_max"
            assert entity._schedule_entry_count_min == 6, "schedule_entry_count_min"
            assert entity._schedule_unit_time == 15, "schedule_unit_time"
        elif type(entity) is Mts300Climate.Schedule:
            EntityComponentTest.expected_entity_types.remove(Mts960Climate.Schedule)
        elif type(entity) is Mts960Climate.Schedule:
            EntityComponentTest.expected_entity_types.remove(Mts300Climate.Schedule)

    async def async_test_enabled_callback(self, entity: MtsSchedule):
        pass

    async def async_test_disabled_callback(self, entity: MtsSchedule):
        pass
