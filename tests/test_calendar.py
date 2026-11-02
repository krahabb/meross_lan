"""Test the MtsSchedule calendar entity (thermostat weekly schedule) editing"""

import contextlib
from copy import deepcopy
from datetime import date, datetime, timedelta
import random
from typing import TYPE_CHECKING
from unittest.mock import patch
from zoneinfo import ZoneInfo

from homeassistant.components import calendar as haec
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.util import dt as dt_util
import pytest

from custom_components.meross_lan.calendar import (
    MTS_SCHEDULE_WEEKDAY,
    insert_schedule_entry,
)
from custom_components.meross_lan.climate import MtsClimate
from custom_components.meross_lan.merossclient.exceptions import MerossError
from custom_components.meross_lan.merossclient.protocol import const as mc
from custom_components.meross_lan.merossclient.protocol.namespaces import (
    hub as mn_h,
)
import emulator as emulator_module

from tests import const as tc, helpers

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

VALVE_ID = "01008C11"
DEVICE_TZ = "Europe/Rome"
# 3 distinct temperatures so that no entry is coalesced
A, B, C = 150, 210, 180
BASE_DAY = [[360, A], [480, B], [600, C]]  # 00:00-06:00, 06:00-14:00, 14:00-24:00
FULL_DAY = [[240, 150], [240, 160], [240, 170], [240, 180], [240, 190], [240, 200]]
T_SUMMARY = "22.5"
T = 225
# far from DST transitions
MON, TUE, WED, FRI = (date(2026, 6, day) for day in (15, 16, 17, 19))
MTS300_TRACE = "U01234567890123456789012345678925-Kpippo-mts300.json.txt"


def _insert(day, begin, end, temperature=T, **kwargs):
    day_copy = deepcopy(day)
    kwargs = {"flatten": True, "entry_count_max": 0} | kwargs
    result = insert_schedule_entry(day, begin, end, temperature, **kwargs)
    assert day == day_copy
    return result


@pytest.mark.parametrize(
    "begin, end, temperature, flatten, expected",
    [
        # replace an existing entry
        (360, 840, T, True, [[360, A], [480, T], [600, C]]),
        # inside a single entry: split into 3
        (480, 600, T, True, [[360, A], [120, B], [120, T], [240, B], [600, C]]),
        # across entries
        (300, 900, T, True, [[300, A], [600, T], [540, C]]),
        (300, 840, T, True, [[300, A], [540, T], [600, C]]),
        (360, 900, T, True, [[360, A], [540, T], [540, C]]),
        # day start/end
        (0, 120, T, True, [[120, T], [240, A], [480, B], [600, C]]),
        (0, 840, T, True, [[840, T], [600, C]]),
        (1320, 1440, T, True, [[360, A], [480, B], [480, C], [120, T]]),
        (360, 1440, T, True, [[360, A], [1080, T]]),
        (0, 1440, T, True, [[1440, T]]),
        # same temperature as a neighbor: coalesced only when flattening
        (300, 420, B, True, [[300, A], [540, B], [600, C]]),
        (300, 420, B, False, [[300, A], [120, B], [420, B], [600, C]]),
        (360, 840, A, True, [[840, A], [600, C]]),
    ],
)
def test_insert_schedule_entry(begin, end, temperature, flatten, expected):
    assert _insert(BASE_DAY, begin, end, temperature, flatten=flatten) == expected


@pytest.mark.parametrize(
    "day, begin, end, entry_count_max, expected",
    [
        # entries before the new one merge into their previous
        (FULL_DAY, 510, 570, 6, [[510, 150], [60, T], [150, 170]] + FULL_DAY[3:]),
        # entries after the new one merge into their next
        (FULL_DAY, 60, 120, 6, [[60, 150], [60, T], [600, 170]] + FULL_DAY[3:]),
        (BASE_DAY, 480, 600, 4, [[480, A], [120, T], [240, B], [600, C]]),
        # last resort: the remaining neighbor merges into the new entry
        (BASE_DAY, 480, 600, 2, [[600, T], [840, C]]),
        (BASE_DAY, 480, 600, 1, [[1440, T]]),
    ],
)
def test_insert_schedule_entry_count_max(day, begin, end, entry_count_max, expected):
    assert _insert(day, begin, end, entry_count_max=entry_count_max) == expected


def test_insert_schedule_entry_random():
    rnd = random.Random(1234)
    day = FULL_DAY
    for _ in range(200):
        begin = rnd.randrange(0, 1440 - 15, 15)
        end = rnd.randrange(begin + 15, 1440 + 1, 15)
        temperature = rnd.randrange(50, 350, 5)
        day = _insert(day, begin, end, temperature, entry_count_max=6)
        assert len(day) <= 6
        assert all(entry[0] > 0 for entry in day)
        assert sum(entry[0] for entry in day) == 1440
        minutes = 0
        for duration, t in day:
            if minutes <= begin < minutes + duration:
                assert t == temperature
                break
            minutes += duration


def _midnight(day: date):
    return datetime(day.year, day.month, day.day, tzinfo=ZoneInfo(DEVICE_TZ))


def _event(day: date, start: float, end: float, summary=T_SUMMARY, rrule=None):
    """An event dict in device time (start/end in hours from day midnight)."""
    midnight = _midnight(day)
    event = {
        haec.EVENT_START: midnight + timedelta(hours=start),
        haec.EVENT_END: midnight + timedelta(hours=end),
        haec.EVENT_SUMMARY: summary,
    }
    return event | {haec.EVENT_RRULE: rrule} if rrule else event


class ValveContext:

    def __init__(self, context: helpers.DeviceContext):
        self.context = context
        climate = context.device.subdevices[VALVE_ID]
        assert isinstance(climate, MtsClimate)
        self.schedule = climate.schedule
        self.p_schedule: dict = context.emulator._get_subdevice_namespace(  # type: ignore
            VALVE_ID, mn_h.Appliance_Hub_Mts100_ScheduleB
        )
        """The emulator state for the valve ScheduleB namespace."""

    def check_day(self, weekday: str, expected: list):
        """Checks the emulator (device) and the entity state for weekday."""
        p_day = self.p_schedule[weekday]
        # mts100 always needs 6 entries (scheduleBMode) when written
        assert len(p_day) == 6 or p_day == expected
        assert [entry for entry in p_day if entry[0]] == expected
        assert self.schedule._schedule[weekday] == expected
        assert self.schedule.ns_value[weekday] == p_day

    def check_week(self, expected: dict | None = None):
        expected = expected or {}
        for weekday in MTS_SCHEDULE_WEEKDAY:
            self.check_day(weekday, expected.get(weekday, BASE_DAY))

    async def async_create(self, *args, **kwargs):
        await self.schedule.async_create_event(**_event(*args, **kwargs))

    async def async_service(self, service: str, data: dict, **kwargs):
        return await self.context.hass.services.async_call(
            haec.DOMAIN,
            service,
            {"entity_id": self.schedule.entity_id} | data,
            blocking=True,
            **kwargs,
        )

    async def async_get_events(self, start: datetime, end: datetime):
        response = await self.async_service(
            haec.SERVICE_GET_EVENTS,
            {haec.EVENT_START_DATETIME: start, haec.EVENT_END_DATETIME: end},
            return_response=True,
        )
        return response[self.schedule.entity_id]["events"]  # type: ignore


@contextlib.asynccontextmanager
async def _valve_context(request, hass: "HomeAssistant", week: dict | None = None):
    """An msh300 hub with an mts100 valve whose schedule is BASE_DAY for every
    day unless overridden in week."""
    emulator = helpers.build_emulator(mc.TYPE_MSH300)
    emulator.set_timezone(DEVICE_TZ)
    async with helpers.DeviceContext(
        request, hass, emulator, auto_poll=True
    ) as context:
        valve = ValveContext(context)
        week = week or {}
        for weekday in MTS_SCHEDULE_WEEKDAY:
            valve.p_schedule[weekday] = deepcopy(week.get(weekday, BASE_DAY))
        schedule = valve.schedule
        assert str(schedule.parent.tz) == DEVICE_TZ
        await schedule.handler_ns.async_get(schedule.index)
        valve.check_week(week)
        yield valve


async def test_calendar_create(request, hass: "HomeAssistant"):
    async with _valve_context(request, hass) as valve:
        # unaligned start/end are floored/ceiled to the unit time (15 min)
        await valve.async_create(MON, 5 + 10 / 60, 15 + 20 / 60)
        valve.check_week({"mon": [[300, A], [630, T], [510, C]]})


async def test_calendar_create_invalid(request, hass: "HomeAssistant"):
    async with _valve_context(request, hass) as valve:
        for start, end, summary, rrule, message in (
            (5, 5, T_SUMMARY, None, "Minimum event duration is 15 minutes"),
            (22, 25, T_SUMMARY, None, r"Events must not cross midnight \(device"),
            (5, 6, "warm", None, "Provide a valid temperature in the summary"),
            (5, 6, T_SUMMARY, "FREQ=DAILY;COUNT=2", "Daily recurrence too complex"),
            (5, 6, T_SUMMARY, "FREQ=MONTHLY", "Invalid frequency for rule"),
            (5, 6, T_SUMMARY, "FREQ=WEEKLY;BYDAY=MO,XX", "recurrence day: XX"),
        ):
            with pytest.raises(ServiceValidationError, match=message):
                await valve.async_create(MON, start, end, summary, rrule)
        with pytest.raises(ServiceValidationError, match="All-day events"):
            await valve.async_service(
                haec.CREATE_EVENT_SERVICE,
                {
                    haec.EVENT_SUMMARY: T_SUMMARY,
                    haec.EVENT_START_DATE: MON,
                    haec.EVENT_END_DATE: MON + timedelta(days=1),
                },
            )
        valve.check_week()


async def test_calendar_create_recurrence(request, hass: "HomeAssistant"):
    expected = [[300, A], [600, T], [540, C]]
    async with _valve_context(request, hass) as valve:
        await valve.async_create(MON, 5, 15, rrule="FREQ=DAILY")
        valve.check_week({weekday: expected for weekday in MTS_SCHEDULE_WEEKDAY})

    async with _valve_context(request, hass) as valve:
        # 10:00-15:00 device time is the same day in HA (US/Pacific)
        await valve.async_create(MON, 10, 15, rrule="FREQ=WEEKLY;BYDAY=MO,WE")
        expected = [[360, A], [240, B], [300, T], [540, C]]
        valve.check_week({"mon": expected, "wed": expected})
        # a plain weekly recurrence applies to the event weekday (device time)
        await valve.async_create(WED, 0, 1, rrule="FREQ=WEEKLY")
        valve.check_week({"mon": expected, "wed": [[60, T], [300, A]] + expected[1:]})

    async with _valve_context(request, hass) as valve:
        # Tue 05:00 in the device is Mon 20:00 in HA: BYDAY is in HA local time
        assert str(dt_util.get_default_time_zone()) == "US/Pacific"
        await valve.async_create(TUE, 5, 6, rrule="FREQ=WEEKLY;BYDAY=MO")
        valve.check_week({"tue": [[300, A], [60, T], [480, B], [600, C]]})


async def test_calendar_missing_day(request, hass: "HomeAssistant"):
    async with _valve_context(request, hass) as valve:
        schedule = valve.schedule
        schedule.ns_value = {k: v for k, v in schedule.ns_value.items() if k != "sun"}
        schedule._build_internal_schedule()
        with pytest.raises(HomeAssistantError, match="Schedule for sun"):
            await valve.async_create(MON + timedelta(days=6), 5, 15)
        # the missing day is not sent: the device keeps its own
        await valve.async_create(MON, 5, 15)
        valve.check_day("mon", [[300, A], [600, T], [540, C]])
        assert valve.p_schedule["sun"] == BASE_DAY


async def test_calendar_delete_update(request, hass: "HomeAssistant"):
    async with _valve_context(request, hass, {"tue": [[1440, A]]}) as valve:
        schedule = valve.schedule
        # delete middle: the previous entry fills the gap
        await schedule.async_delete_event("mon#1")
        valve.check_day("mon", [[840, A], [600, C]])
        # delete first: the next entry fills the gap
        await schedule.async_delete_event("wed#0")
        valve.check_day("wed", [[840, B], [600, C]])
        with pytest.raises(ServiceValidationError, match="at least one event"):
            await schedule.async_delete_event("tue#0")
        for uid in ("thu#7", "thu#-1", "thu", "xyz#0"):
            with pytest.raises(ServiceValidationError, match="Invalid event uid"):
                await schedule.async_delete_event(uid)
        valve.check_day("tue", [[1440, A]])
        valve.check_day("thu", BASE_DAY)
        # update: delete + create
        await schedule.async_update_event(
            "fri#1", _event(FRI, 8, 10, rrule="FREQ=WEEKLY")
        )
        valve.check_day("fri", [[480, A], [120, T], [240, A], [600, C]])
        # update of the only entry in the day
        await schedule.async_update_event("tue#0", _event(TUE, 0, 24))
        valve.check_day("tue", [[1440, T]])


async def test_calendar_websocket(request, hass: "HomeAssistant", hass_ws_client):
    async with _valve_context(request, hass) as valve:
        client = await hass_ws_client(hass)

        async def _send(type: str, **kwargs):
            entity_id = valve.schedule.entity_id
            await client.send_json_auto_id(
                {"type": f"calendar/event/{type}", "entity_id": entity_id} | kwargs
            )
            return await client.receive_json()

        def _ws_event(day: date, start: int, end: int):
            return {k: str(v) for k, v in _event(day, start, end).items()}

        result = await _send("create", event=_ws_event(MON, 5, 15))
        assert result["success"], result
        valve.check_day("mon", [[300, A], [600, T], [540, C]])
        result = await _send("update", uid="tue#1", event=_ws_event(TUE, 6, 15))
        assert result["success"], result
        valve.check_day("tue", [[360, A], [540, T], [540, C]])
        # errors are reported with their message
        result = await _send("delete", uid="wed#9")
        assert not result["success"]
        assert result["error"]["message"] == "Invalid event uid: wed#9"


async def test_calendar_timezone(request, hass: "HomeAssistant"):
    ha_tz = dt_util.get_default_time_zone()
    assert str(ha_tz) != DEVICE_TZ
    async with _valve_context(request, hass) as valve:
        # 06:00-07:00 HA local (naive: as the service schema passes it)
        ha_start = datetime(WED.year, WED.month, WED.day, 6, 0)
        ha_end = ha_start + timedelta(hours=1)
        await valve.async_service(
            haec.CREATE_EVENT_SERVICE,
            {
                haec.EVENT_SUMMARY: T_SUMMARY,
                haec.EVENT_START_DATETIME: ha_start,
                haec.EVENT_END_DATETIME: ha_end,
            },
        )
        # US/Pacific (-7) -> Europe/Rome (+2): 15:00-16:00
        wed = [[360, A], [480, B], [60, C], [60, T], [480, C]]
        valve.check_week({"wed": wed})
        # reading back gives the same instant
        ha_start, ha_end = ha_start.replace(tzinfo=ha_tz), ha_end.replace(tzinfo=ha_tz)
        events = await valve.async_get_events(ha_start, ha_end)
        assert len(events) == 1
        assert dt_util.parse_datetime(events[0]["start"]) == ha_start
        assert dt_util.parse_datetime(events[0]["end"]) == ha_end
        assert events[0]["summary"].startswith("22.5")


async def test_calendar_read_after_write(request, hass: "HomeAssistant"):
    async with _valve_context(request, hass) as valve:
        await valve.async_create(WED, 15, 16)
        week_start = _midnight(MON)
        events = await valve.async_get_events(
            week_start, week_start + timedelta(days=7)
        )
        assert len(events) == 6 * 3 + 5


async def test_calendar_device_error(request, hass: "HomeAssistant"):
    async with _valve_context(request, hass) as valve:
        with patch.object(
            valve.context.emulator,
            "_SET_Appliance_Hub_Mts100_ScheduleB",
            create=True,
            side_effect=Exception("schedule rejected"),
        ):
            with pytest.raises(HomeAssistantError) as exc_info:
                await valve.async_create(MON, 5, 15)
        assert (
            str(exc_info.value) == "The device refused the schedule: schedule rejected"
        )
        valve.check_week()

        for exception, message, log in (
            (MerossError("timeout"), "Unable to send the schedule", "timeout"),
            (ValueError("boom"), r"Schedule update failed \(see log\)", "boom"),
        ):
            with patch.object(
                type(valve.schedule.parent), "async_request", side_effect=exception
            ):
                with pytest.raises(HomeAssistantError, match=message):
                    await valve.async_create(MON, 5, 15)
            valve.context.assert_logs(1, level=30, message=rf".*{log}.*")
            valve.check_week()


async def test_calendar_mts300_readonly(request, hass: "HomeAssistant"):
    emulator = emulator_module.build_emulator(
        tc.EMULATOR_TRACES_PATH + MTS300_TRACE,
        key=tc.MOCK_KEY,
        uuid=tc.MOCK_DEVICE_UUID,
    )
    async with helpers.DeviceContext(
        request, hass, emulator, auto_poll=True
    ) as context:
        climate = next(
            entity
            for entity in context.device.entities.values()
            if isinstance(entity, MtsClimate)
        )
        schedule = climate.schedule
        assert schedule.supported_features == 0
        start = dt_util.start_of_local_day()
        assert await schedule.async_get_events(hass, start, start + timedelta(days=1))
