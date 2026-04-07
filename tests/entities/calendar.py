from homeassistant.components import calendar as haec
from homeassistant.components.calendar import CalendarEntity
from homeassistant.util import dt as dt_util

from custom_components.meross_lan.calendar import MtsSchedule
from custom_components.meross_lan.merossclient.protocol import const as mc
from custom_components.meross_lan.merossclient.protocol.namespaces import (
    hub as mn_h,
    thermostat as mn_t,
)

from tests.entities import EntityComponentTest


class EntityTest(EntityComponentTest):

    ENTITY_TYPE = CalendarEntity

    NAMESPACES_ENTITIES = {
        mn_t.Appliance_Control_Thermostat_Schedule: [MtsSchedule],
        mn_t.Appliance_Control_Thermostat_ScheduleB: [MtsSchedule],
    }

    HUB_SUBDEVICES_ENTITIES = {
        mc.TYPE_MTS100: [MtsSchedule],
        mc.TYPE_MTS100V3: [MtsSchedule],
        mc.TYPE_MTS150: [MtsSchedule],
    }

    async def async_test_each_callback(self, entity: MtsSchedule):
        # TODO: add more test for calendar platform(s)
        await super().async_test_each_callback(entity)
        if entity.ns is mn_h.Appliance_Hub_Mts100_ScheduleB:
            # these are not set in stone though, but always appear like these in our traces
            assert entity._schedule_entry_count_max == 6, "schedule_entry_count_max"
            assert entity._schedule_entry_count_min == 6, "schedule_entry_count_min"
            assert entity._schedule_unit_time == 15, "schedule_unit_time"

    async def async_test_enabled_callback(self, entity: MtsSchedule):
        # TODO: refine test for calendar platform(s)
        # Right now this is useful to just add code coverage for the calendar entities
        service_response = await self.async_service_response(
            haec.SERVICE_GET_EVENTS,
            {
                haec.EVENT_START_DATETIME: dt_util.start_of_local_day(),
                haec.EVENT_END_DATETIME: dt_util.now(),
            },
        )
        assert service_response is not None, "no service response"

        if service_response[self.entity_id]["events"]:  # type: ignore
            # since the emulator state is not yet sanitized we cannot
            # ensure the state is available and or consistent.
            # We'll then just create events for entities where a state is avaialble
            try:
                await self.async_service_call(
                    haec.CREATE_EVENT_SERVICE,
                    {
                        haec.EVENT_SUMMARY: "21",
                        haec.EVENT_START_DATETIME: dt_util.start_of_local_day(),
                        haec.EVENT_END_DATETIME: dt_util.start_of_local_day()
                        + dt_util.dt.timedelta(hours=1),
                    },
                )
            except Exception as ex:
                if str(ex) != "Exception Too many elements in the schedule":
                    # this is acceptable since the schedule may be full
                    raise ex

    async def async_test_disabled_callback(self, entity: MtsSchedule):
        pass
