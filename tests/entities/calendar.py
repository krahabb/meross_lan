from homeassistant.components.calendar import CalendarEntity

from custom_components.meross_lan.devices.mts100 import Mts100Schedule
from custom_components.meross_lan.devices.mts200 import Mts200Schedule
from custom_components.meross_lan.devices.mts960 import Mts960Schedule
from custom_components.meross_lan.merossclient import const as mc
from custom_components.meross_lan.calendar import MtsSchedule

from tests.entities import EntityComponentTest


class EntityTest(EntityComponentTest):

    ENTITY_TYPE = CalendarEntity

    NAMESPACES_ENTITIES = {
        mc.NS_APPLIANCE_CONTROL_THERMOSTAT_SCHEDULE: [Mts200Schedule],
        mc.NS_APPLIANCE_CONTROL_THERMOSTAT_SCHEDULEB: [Mts960Schedule],
    }

    HUB_SUBDEVICES_ENTITIES = {
        mc.TYPE_MTS100: [Mts100Schedule],
        mc.TYPE_MTS100V3: [Mts100Schedule],
        mc.TYPE_MTS150: [Mts100Schedule],
    }

    async def async_test_each_callback(self, entity: MtsSchedule):
        await super().async_test_each_callback(entity)
        if isinstance(entity, Mts100Schedule):
            # these are not set in stone though, but always appears like these in our traces
            assert entity._schedule_entry_count == 6, "schedule_entry_count"
            assert entity._schedule_unit_time == 15, "schedule_unit_time"

    async def async_test_enabled_callback(self, entity: MtsSchedule):
        pass

    async def async_test_disabled_callback(self, entity: MtsSchedule):
        pass
