from __future__ import annotations

import dataclasses
from datetime import datetime, timedelta
import re
import typing

from homeassistant.components import calendar
from homeassistant.components.calendar.const import (
    EVENT_END,
    EVENT_RRULE,
    EVENT_START,
    EVENT_SUMMARY,
)
from homeassistant.exceptions import HomeAssistantError
from homeassistant.util import dt

from . import meross_entity as me
from .climate import MtsClimate
from .helpers import clamp
from .merossclient import const as mc

if typing.TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

    from .meross_device import MerossDeviceBase


async def async_setup_entry(
    hass: HomeAssistant, config_entry: ConfigEntry, async_add_devices
):
    me.platform_setup_entry(hass, config_entry, async_add_devices, calendar.DOMAIN)


class MLCalendar(me.MerossEntity, calendar.CalendarEntity):  # type: ignore
    PLATFORM = calendar.DOMAIN


MTS_SCHEDULE_WEEKDAY = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
MTS_SCHEDULE_RRULE = "FREQ=WEEKLY"

MtsScheduleNativeEntry = list[int]
MtsScheduleNativeDayEntry = list[MtsScheduleNativeEntry]
MtsScheduleNativeType = dict[str, MtsScheduleNativeDayEntry]


@dataclasses.dataclass
class MtsScheduleEntry:
    """
    represent the index of an entry in the native schedule dictionary
    of the MTSXXX
    example:
         weekday_index = 0 # 'mon'
         index = 0
         data = [390, 200]
    """

    weekday_index: int
    index: int
    minutes_begin: int
    minutes_end: int
    day: datetime  # base date of day used when querying this: used to calculate CalendarEvent
    data: MtsScheduleNativeEntry  # actually points to the inner list in the native payload (not a copy)

    def get_event(self) -> calendar.CalendarEvent:
        """
        returns an HA CalendarEvent set up with this entry data (schedule)
        relevant for the calendar day provided in event_day
        """
        event_begin = self.day.replace(
            hour=self.minutes_begin // 60,
            minute=self.minutes_begin % 60,
            second=0,
            microsecond=0,
        )
        if self.minutes_end >= 1440:
            event_end = self.day.replace(
                hour=0, minute=0, second=0, microsecond=0
            ) + timedelta(days=1)
        else:
            event_end = self.day.replace(
                hour=self.minutes_end // 60,
                minute=self.minutes_end % 60,
                second=0,
                microsecond=0,
            )
        return calendar.CalendarEvent(
            start=dt.as_utc(event_begin),
            end=dt.as_utc(event_end),
            summary=f"{self.data[1] / 10} {MtsClimate.TEMP_CELSIUS}",
            description="",
            uid=f"{MTS_SCHEDULE_WEEKDAY[self.weekday_index]}#{self.index}",
            rrule=MTS_SCHEDULE_RRULE,
        )


class MtsSchedule(MLCalendar):
    manager: MerossDeviceBase
    climate: typing.Final[MtsClimate]

    namespace: str  # set in descendant class def
    key_channel: str  # set in descendant class def

    _attr_entity_category = me.EntityCategory.CONFIG
    _attr_state: MtsScheduleNativeType | None
    _attr_supported_features = (
        calendar.CalendarEntityFeature.CREATE_EVENT
        | calendar.CalendarEntityFeature.DELETE_EVENT
        | calendar.CalendarEntityFeature.UPDATE_EVENT
    )

    _schedule: MtsScheduleNativeType | None

    __slots__ = (
        "climate",
        "_flatten",
        "_schedule",
        "_schedule_unit_time",
        "_schedule_entry_count",
    )

    def __init__(
        self,
        climate: MtsClimate,
    ):
        self.climate = climate
        self._flatten = True
        # save a flattened version of the device schedule to ease/optimize CalendarEvent management
        # since the original schedule has a fixed number of contiguous events spanning the day(s) (6 on my MTS100)
        # we might 'compress' these when 2 or more consecutive entries don't change the temperature
        # self._attr_state carries the original unpacked schedule payload from the device representing
        # its effective state
        self._schedule = None
        # set the 'granularity' of the schedule entries i.e. the schedule duration
        # must be a multiple of this time (in minutes). It is set lately by customized
        # implementations
        self._schedule_unit_time = 15
        # number of schedules per day supported by the device. Mines (mts100) default to 6
        # but we're recovering the value by inspecting the device scheduleB payload.
        # Also, this should be the same as scheduleBMode in Mts100Climate
        self._schedule_entry_count = 0
        self._attr_extra_state_attributes = {}
        super().__init__(climate.manager, climate.channel, mc.KEY_SCHEDULE, None)

    # interface: MerossEntity
    async def async_shutdown(self):
        self.climate = None  # type: ignore
        await super().async_shutdown()

    # interface: Calendar
    @property
    def event(self) -> calendar.CalendarEvent | None:
        """Return the next upcoming event."""
        if self.climate.is_mts_scheduled():
            if event_index := self._get_event_entry(datetime.now(tz=self.manager.tz)):
                return event_index.get_event()
        return None

    async def async_get_events(
        self,
        hass,
        start_date: datetime,
        end_date: datetime,
    ) -> list[calendar.CalendarEvent]:
        """Return calendar events within a datetime range."""
        events = []
        event_entry = self._get_event_entry(start_date.astimezone(self.manager.tz))
        while event_entry:
            event = event_entry.get_event()
            if event.start >= end_date:
                break
            events.append(event)
            # we'll set a guard to prevent crazy loops
            if len(events) > 1000:
                self.warning(
                    "returning too many calendar events: breaking the loop now",
                    timeout=14400,
                )
                break
            event_entry = self._get_next_event_entry(event_entry)
        return events

    async def async_create_event(self, **kwargs):
        try:
            await self._internal_create_event(**kwargs)
            await self._async_request_schedule()
        except Exception as exception:
            # invalidate working data (might be dirty)
            self._schedule = None
            raise HomeAssistantError(
                f"{type(exception).__name__} {str(exception)}"
            ) from exception

    async def async_delete_event(
        self,
        uid: str,
        recurrence_id: str | None = None,
        recurrence_range: str | None = None,
    ):
        try:
            if self._internal_delete_event(uid):
                await self._async_request_schedule()
            else:
                raise Exception("The daily schedule must contain at least one event")
        except Exception as error:
            # invalidate working data (might be dirty)
            self._schedule = None
            raise HomeAssistantError(str(error)) from error

    async def async_update_event(
        self,
        uid: str,
        event: dict,
        recurrence_id: str | None = None,
        recurrence_range: str | None = None,
    ) -> None:
        try:
            self._internal_delete_event(uid)
            await self._internal_create_event(**event)
            await self._async_request_schedule()
        except Exception as error:
            # invalidate working data (might be dirty)
            self._schedule = None
            raise HomeAssistantError(str(error)) from error

    # interface: self
    @property
    def schedule(self):
        if self._schedule is None:
            if state := self._attr_state:
                # state = {
                #   ...
                #   "mon": [[390,150],[90,240],[300,190],[270,220],[300,150],[90,150]],
                #   "tue": [[390,150],[90,240],[300,190],[270,220],[300,150],[90,150]],
                #   "wed": [[390,150],[90,240],[300,190],[270,220],[300,150],[90,150]],
                #   "thu": [[390,150],[90,240],[300,190],[270,220],[300,150],[90,150]],
                #   "fri": [[390,150],[90,240],[300,190],[270,220],[300,150],[90,150]],
                #   "sat": [[390,150],[90,240],[300,190],[270,220],[300,150],[90,150]],
                #   "sun": [[390,150],[90,240],[300,190],[270,220],[300,150],[90,150]]
                #   }
                schedule: MtsScheduleNativeType = {w: [] for w in MTS_SCHEDULE_WEEKDAY}
                for weekday, weekday_schedule in schedule.items():
                    if weekday_state := state.get(weekday):
                        # weekday_state = [[390,150],[90,240],[300,190],[270,220],[300,150],[90,150]]
                        # recover the length and do a sanity check: we expect
                        # the device schedules to be the same fixed length
                        schedule_entry_count = len(weekday_state)
                        if self._schedule_entry_count != schedule_entry_count:
                            # this should fire only on first weekday scan
                            if self._schedule_entry_count:
                                self.warning(
                                    "unexpected device schedule entries count",
                                    timeout=14400,
                                )
                            else:
                                self._schedule_entry_count = schedule_entry_count
                        if self._flatten:
                            current_entry = None
                            for entry in weekday_state:
                                if current_entry and (entry[1] == current_entry[1]):
                                    # same T: flatten out
                                    current_entry[0] = current_entry[0] + entry[0]
                                else:
                                    current_entry = list(entry)
                                    weekday_schedule.append(current_entry)
                        else:
                            # don't flatten..but (deep)copy over
                            for entry in weekday_state:
                                weekday_schedule.append(list(entry))

                self._schedule = schedule

        return self._schedule

    def update_mts_state(self):
        # since our state/active event is dependent on climate mode
        # we'll force a state update when the climate entity
        if self._hass_connected:
            self._async_write_ha_state()

    async def _async_request_schedule(self):
        if schedule := self.schedule:
            payload = {}
            # the time duration step (minimum interval) of the schedule intervals
            schedule_entry_unittime = self._schedule_unit_time
            # unpack our schedule struct to be compliant with the device payload i.e.
            # the weekday_schedule must contain
            for weekday, weekday_schedule in schedule.items():
                # our working schedule might contain less entries than requested by MTS
                schedule_items_missing = self._schedule_entry_count - len(
                    weekday_schedule
                )
                if schedule_items_missing < 0:
                    raise Exception("Inconsistent number of elements in the schedule")
                if schedule_items_missing == 0:
                    payload[weekday] = weekday_schedule
                    continue
                # we have to generate some 'filler' since the mts expects
                # schedule_items_count in the weekday schedule
                payload_weekday_schedule = []
                for schedule_entry in weekday_schedule:
                    # schedule_entry[0] = duration
                    # schedule_entry[1] = setpoint
                    if schedule_items_missing == 0:
                        # at this point just pass over
                        payload_weekday_schedule.append(schedule_entry)
                        continue
                    schedule_entry_duration = schedule_entry[0]
                    while (schedule_entry_duration > schedule_entry_unittime) and (
                        schedule_items_missing > 0
                    ):
                        payload_weekday_schedule.append(
                            [schedule_entry_unittime, schedule_entry[1]]
                        )
                        schedule_entry_duration -= schedule_entry_unittime
                        schedule_items_missing -= 1
                    payload_weekday_schedule.append(
                        [schedule_entry_duration, schedule_entry[1]]
                    )
                payload[weekday] = payload_weekday_schedule

            payload[self.key_channel] = self.channel

            if not await self.manager.async_request_ack(
                self.namespace,
                mc.METHOD_SET,
                {mc.KEY_SCHEDULE: [payload]},
            ):
                # there was an error so we request the actual device state again
                if self.manager.online:
                    await self.manager.async_request(
                        self.namespace,
                        mc.METHOD_GET,
                        {mc.KEY_SCHEDULE: [{self.key_channel: self.channel}]},
                    )

    def _get_event_entry(self, event_time: datetime) -> MtsScheduleEntry | None:
        """Search for and return an entry description (MtsScheduleEntry) matching the internal
        schedule representation at the event_time point in time. This in turn helps in translating
        the internal representation to the HA CaleandarEvent used to pass the state to HA.
        event_time is expressed in local time of the device (if it has any configured)
        """
        schedule = self.schedule
        if not schedule:
            return None
        weekday_index = event_time.weekday()
        weekday_schedule = schedule.get(MTS_SCHEDULE_WEEKDAY[weekday_index])
        if not weekday_schedule:
            return None
        event_day = event_time.replace(hour=0, minute=0, second=0, microsecond=0)
        event_minutes = (event_time - event_day).total_seconds() / 60
        schedule_minutes_begin = 0
        schedule_index = 0
        for schedule in weekday_schedule:
            # here schedule is a list like [390, 75]
            schedule_minutes_end = schedule_minutes_begin + schedule[0]
            if schedule_minutes_begin <= event_minutes < schedule_minutes_end:
                return MtsScheduleEntry(
                    weekday_index=weekday_index,
                    index=schedule_index,
                    minutes_begin=schedule_minutes_begin,
                    minutes_end=schedule_minutes_end,
                    day=event_day,
                    data=schedule,
                )
            schedule_minutes_begin = schedule_minutes_end
            schedule_index += 1
        return None

    def _get_next_event_entry(
        self, event_entry: MtsScheduleEntry
    ) -> MtsScheduleEntry | None:
        """Extracts the next event entry description from the internal schedule representation
        Useful to iterate over when HA asks for data
        """
        with self.exception_warning("parsing internal schedule", timeout=14400):
            schedule = self.schedule
            if not schedule:
                return None
            weekday_index = event_entry.weekday_index
            weekday_schedule: list = schedule[MTS_SCHEDULE_WEEKDAY[weekday_index]]
            schedule_index = event_entry.index + 1
            if schedule_index < len(weekday_schedule):
                event_day = event_entry.day
                schedule_minutes_begin = event_entry.minutes_end
            else:
                event_day = event_entry.day + timedelta(days=1)
                weekday_index = event_day.weekday()
                weekday_schedule = schedule[MTS_SCHEDULE_WEEKDAY[weekday_index]]
                schedule_index = 0
                schedule_minutes_begin = 0
            schedule = weekday_schedule[schedule_index]
            return MtsScheduleEntry(
                weekday_index=weekday_index,
                index=schedule_index,
                minutes_begin=schedule_minutes_begin,
                minutes_end=schedule_minutes_begin + schedule[0],
                day=event_day,
                data=schedule,
            )

    def _extract_rfc5545_temp(self, event: dict[str, typing.Any]) -> int:
        match = re.search(r"[-+]?(?:\d*\.*\d+)", event[EVENT_SUMMARY])
        if match:
            return int(
                clamp(
                    float(match.group()),
                    self.climate.min_temp,
                    self.climate.max_temp,
                )
                * 10
            )
        else:
            raise Exception("Provide a valid temperature in the summary field")

        for s in event[EVENT_SUMMARY].split():
            try:
                return int(
                    clamp(
                        float(s),
                        self.climate.min_temp,
                        self.climate.max_temp,
                    )
                    * 10
                )
            except Exception:
                pass
        else:
            raise Exception("Provide a valid temperature in the summary field")

    def _extract_rfc5545_info(
        self, event: dict[str, typing.Any]
    ) -> tuple[datetime, datetime, int]:
        """Returns event start,end,temperature from an RFC5545 dict. Throws exception if
        the temperature cannot be parsed (expecting the SUMMARY field to carry the T value)
        """
        return (
            event[EVENT_START],
            event[EVENT_END],
            self._extract_rfc5545_temp(event),
        )

    def _internal_delete_event(self, uid: str):
        schedule = self.schedule
        if not schedule:
            raise Exception("Internal state unavailable")
        uid_split = uid.split("#")
        weekday_schedule: list = schedule[uid_split[0]]
        # our schedule cannot be empty: it must fill the 24 hours
        if len(weekday_schedule) <= 1:
            return False
        schedule_index = int(uid_split[1])
        schedule_entry = weekday_schedule.pop(schedule_index)
        # we have to fill up the schedule by extending the preceding
        # or the following schedule_entry in order to keep the overall
        # weekday_schedule duration equal to 24 hours
        if schedule_index > 0:
            # add the duration of the removed entry to the preceeding
            weekday_schedule[schedule_index - 1][0] += schedule_entry[0]
        else:
            # we removed the first so we'll add to the next (now first)
            weekday_schedule[0][0] += schedule_entry[0]
        return True

    async def _internal_create_event(self, **kwargs):
        schedule = self.schedule
        if not schedule:
            raise Exception("Internal state unavailable")
        # get the number of maximum entries for the day from device state
        if self._schedule_entry_count < 1:
            raise Exception("Not enough schedule space available")
        (
            event_start,
            event_end,
            event_temperature,
        ) = self._extract_rfc5545_info(kwargs)
        # allow only schedule up to midnight: i.e. not spanning multiple days
        event_day_start = event_start.replace(hour=0, minute=0, second=0, microsecond=0)
        event_minutes_start = event_start.hour * 60 + event_start.minute
        event_minutes_start -= event_minutes_start % self._schedule_unit_time
        event_day_end = event_day_start + timedelta(days=1)
        if event_end > event_day_end:
            raise Exception("Events spanning multiple days are not allowed")
        if event_end == event_day_end:
            event_minutes_end = 1440
        else:
            # round up to the next self._scheduleunittime interval
            event_minutes_end = event_end.hour * 60 + event_end.minute
            event_minutes_end_remainder = event_minutes_end % self._schedule_unit_time
            if event_minutes_end_remainder:
                event_minutes_end += (
                    self._schedule_unit_time - event_minutes_end_remainder
                )
        event_minutes_duration = event_minutes_end - event_minutes_start
        if event_minutes_duration < self._schedule_unit_time:
            raise Exception(
                f"Minimum event duration is {self._schedule_unit_time} minutes"
            )

        # recognize some basic recurrence scheme: typically the MTS100 has a weekly schedule
        # and that's by default but we let the user select a daily schedule to setup
        # the same entry among all of the week days
        recurrencedays: tuple
        if event_rrule := kwargs.get(EVENT_RRULE):
            rule_parts = dict(s.split("=", 1) for s in event_rrule.split(";"))
            if not (freq := rule_parts.get("FREQ")):
                raise Exception("Recurrence rule did not contain FREQ")
            if freq == "DAILY":
                if len(rule_parts) > 1:
                    raise Exception("Daily recurrence too complex")
                recurrencedays = MTS_SCHEDULE_WEEKDAY
            elif freq == "WEEKLY":
                if len(rule_parts) > 1:
                    raise Exception("Weekly recurrence too complex")
                recurrencedays = (MTS_SCHEDULE_WEEKDAY[event_start.weekday()],)
            else:
                raise Exception(f"Invalid frequency for rule: {event_rrule}")
        else:
            recurrencedays = (MTS_SCHEDULE_WEEKDAY[event_start.weekday()],)

        for weekday in recurrencedays:
            weekday_schedule = schedule[weekday]
            schedule_minutes_begin = 0
            schedule_index = 0
            schedule_index_insert = None
            for schedule_entry in weekday_schedule:
                schedule_minutes_end = schedule_minutes_begin + schedule_entry[0]
                if event_minutes_start < schedule_minutes_begin:
                    # if our code is good this shouldnt happen!
                    raise Exception("Inconsistent schedule state")
                elif event_minutes_start == schedule_minutes_begin:
                    # insert before
                    schedule_index_insert = schedule_index
                    weekday_schedule.insert(
                        schedule_index_insert,
                        [event_minutes_duration, event_temperature],
                    )
                    # now remove event_minutes_duration of schedule events
                    schedule_index += 1
                    _event_minutes_duration = event_minutes_duration
                    while _event_minutes_duration:
                        schedule_entry_minutes_duration = schedule_entry[0]
                        if schedule_entry_minutes_duration > _event_minutes_duration:
                            # schedule entry ends after our new event: just resize
                            schedule_entry[0] = (
                                schedule_entry_minutes_duration
                                - _event_minutes_duration
                            )
                            break
                        # schedule_entry totally overlapped from newer so we'll discard this
                        # and check the next
                        weekday_schedule.pop(schedule_index)
                        if _event_minutes_duration == schedule_entry_minutes_duration:
                            break  # exit before accessing maybe non-existing schedule_index
                        assert (
                            _event_minutes_duration >= schedule_entry_minutes_duration
                        ), "Something wrong in our schedule"
                        _event_minutes_duration -= schedule_entry_minutes_duration
                        schedule_entry = weekday_schedule[schedule_index]
                        # end while _event_minutes_duration:
                    break
                elif event_minutes_start < schedule_minutes_end:
                    # shorten the previous since the new one is tarting before end
                    schedule_entry[0] = event_minutes_start - schedule_minutes_begin
                    schedule_index_insert = schedule_index + 1
                    weekday_schedule.insert(
                        schedule_index_insert,
                        [event_minutes_duration, event_temperature],
                    )
                    if event_minutes_end < schedule_minutes_end:
                        # new event spans a range contained in the existing schedule_entry
                        # so we add an entry filling the gap between the new end and the old one
                        weekday_schedule.insert(
                            schedule_index + 2,
                            [
                                schedule_minutes_end - event_minutes_end,
                                schedule_entry[1],
                            ],
                        )
                    elif event_minutes_end == schedule_minutes_end:
                        # end of new event aligned to the overwritten existing event: no work to do
                        pass
                    else:
                        # new event overlaps more entries: sayaku!
                        _event_minutes_duration = (
                            event_minutes_duration
                            - schedule_minutes_end
                            - event_minutes_start
                        )
                        schedule_index += 2
                        while _event_minutes_duration > 0:
                            schedule_entry = weekday_schedule[schedule_index]
                            schedule_entry_minutes_duration = schedule_entry[0]
                            if (
                                schedule_entry_minutes_duration
                                > _event_minutes_duration
                            ):
                                # schedule entry ends after our new event: just resize
                                schedule_entry[0] = (
                                    schedule_entry_minutes_duration
                                    - _event_minutes_duration
                                )
                                break
                            weekday_schedule.pop(schedule_index)
                            _event_minutes_duration -= schedule_entry_minutes_duration
                        assert (
                            _event_minutes_duration == 0
                        ), "Something wrong in our schedule"
                    break
                schedule_minutes_begin = schedule_minutes_end
                schedule_index += 1
                # end for schedule_entry in weekday_schedule:

            # at this point our schedule is set but might have too many events in it
            # (total schedules entry need to be less than so we'll reparse and kindly coalesce some events
            assert schedule_index_insert is not None, "New event was not added"
            while len(weekday_schedule) > self._schedule_entry_count:
                # note that len(weekday_schedule) will always be >= 2 since we floored schedule_count
                if schedule_index_insert > 1:
                    # remove event before if not inserted as first or second
                    schedule_index_insert -= 1
                    schedule_entry = weekday_schedule.pop(schedule_index_insert)
                    # add its duration to its previous
                    weekday_schedule[schedule_index_insert - 1][0] += schedule_entry[0]
                    continue
                schedule_entries_after = (
                    len(weekday_schedule) - schedule_index_insert - 1
                )
                if schedule_entries_after > 1:
                    # remove event after
                    schedule_entry = weekday_schedule.pop(schedule_index_insert + 1)
                    # add its duration to its next
                    weekday_schedule[schedule_index_insert + 1][0] += schedule_entry[0]
                    continue
                # we're left with an array of 2 entry where 1 is the newly added so
                # we discard the other and enlarge the last addition to cover the full day
                schedule_entry = weekday_schedule.pop(1 - schedule_index_insert)
                weekday_schedule[0][0] += schedule_entry[0]
                # end while (len(weekday_schedule) > schedule_count):

            # end for weekday

    # message handlers
    def _parse_schedule(self, payload: dict):
        # the payload we receive from the device might be partial
        # if we're getting the PUSH in realtime since it only carries
        # the updated entries for the updated day.
        if isinstance(self._attr_state, dict):
            self._attr_state.update(payload)
        else:
            self._attr_state = payload
        self._attr_extra_state_attributes[mc.KEY_SCHEDULE] = str(self._attr_state)
        # invalidate our internal representation and flush
        self._schedule = None
        self._schedule_entry_count = 0
        if self._hass_connected:
            self._async_write_ha_state()
