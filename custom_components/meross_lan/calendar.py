import copy
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

from .climate import MtsClimate
from .helpers import clamp, entity as me
from .merossclient.protocol import const as mc

if typing.TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

    from .helpers.device import BaseDevice


async def async_setup_entry(
    hass: "HomeAssistant", config_entry: "ConfigEntry", async_add_devices
):
    me.platform_setup_entry(hass, config_entry, async_add_devices, calendar.DOMAIN)


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

    def get_event(self, climate: MtsClimate) -> calendar.CalendarEvent:
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
            summary=f"{self.data[1] / climate.device_scale} {climate.temperature_unit}",
            description="",
            uid=f"{MTS_SCHEDULE_WEEKDAY[self.weekday_index]}#{self.index}",
            rrule=MTS_SCHEDULE_RRULE,
        )


class MtsSchedule(me.MLEntity, calendar.CalendarEntity):
    PLATFORM = calendar.DOMAIN
    manager: "BaseDevice"

    # HA core entity attributes:
    entity_category = me.MLEntity.EntityCategory.CONFIG
    supported_features: calendar.CalendarEntityFeature = (
        calendar.CalendarEntityFeature.CREATE_EVENT
        | calendar.CalendarEntityFeature.DELETE_EVENT
        | calendar.CalendarEntityFeature.UPDATE_EVENT
    )

    climate: typing.Final[MtsClimate]
    _native_schedule: MtsScheduleNativeType | None
    _schedule: MtsScheduleNativeType | None

    __slots__ = (
        "climate",
        "_flatten",
        "_native_schedule",
        "_schedule",
        "_schedule_unit_time",
        "_schedule_entry_count_max",
        "_schedule_entry_count_min",
    )

    def __init__(self, climate: MtsClimate):
        self.climate = climate
        self._flatten = True
        # save a flattened version of the device schedule to ease/optimize CalendarEvent management
        # since the original schedule has a fixed number of contiguous events spanning the day(s) (6 on my MTS100)
        # we might 'compress' these when 2 or more consecutive entries don't change the temperature
        # _native_schedule carries the original unpacked schedule payload from the device representing
        # its effective state
        self._native_schedule = None
        self._schedule = None
        # set the 'granularity' of the schedule entries i.e. the schedule duration
        # must be a multiple of this time (in minutes). It is set lately by customized
        # implementations
        self._schedule_unit_time = 15
        # number of schedules per day supported by the device. Mines (mts100) default to 6
        # The exact value should be extracted from scheduleBMode for mts100.
        # mts200 instead are showing a "section" == 8 value in their .Schedule payload which
        # could represent this information. Being not sure we're skipping that.
        # The default here (=0) disables any schedule entry count check in building payloads
        # meaning we're sending (more or less) the effective number of entries (per day) as
        # shown/available in the calendar UI.
        self._schedule_entry_count_max = 0
        self._schedule_entry_count_min = 0
        super().__init__(climate.manager, climate.channel, self.ns.key, name="Schedule")

    # interface: MLEntity
    async def async_shutdown(self):
        self.climate = None  # type: ignore
        await super().async_shutdown()

    async def async_added_to_hass(self):
        self.manager.check_device_timezone()
        return await super().async_added_to_hass()

    def set_unavailable(self):
        self._native_schedule = None
        self._schedule = None
        super().set_unavailable()

    # interface: Calendar
    @property
    def event(self) -> calendar.CalendarEvent | None:
        """Return the next upcoming event."""
        if self.climate.is_mts_scheduled():
            if event_index := self._get_event_entry(datetime.now(tz=self.manager.tz)):
                return event_index.get_event(self.climate)
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
            event = event_entry.get_event(self.climate)
            if event.start >= end_date:
                break
            events.append(event)
            # we'll set a guard to prevent crazy loops
            if len(events) > 1000:
                self.log(
                    self.WARNING,
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
            self._build_internal_schedule()
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
            self._build_internal_schedule()
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
            self._build_internal_schedule()
            raise HomeAssistantError(str(error)) from error

    # interface: self
    async def _async_request_schedule(self):
        if schedule := self._schedule:
            payload = {}
            # unpack our schedule struct to be compliant with the device payload:
            # the weekday_schedule must contain between _schedule_entry_count_min and
            # _schedule_entry_count_max
            for weekday, weekday_schedule in schedule.items():
                schedule_entry_count = len(weekday_schedule)
                if schedule_entry_count > self._schedule_entry_count_max:
                    raise Exception("Too many elements in the schedule")
                schedule_items_missing = (
                    self._schedule_entry_count_min - schedule_entry_count
                )
                if schedule_items_missing > 0:
                    # our working schedule contains less entries than requested by MTS
                    weekday_schedule = list(weekday_schedule)
                    weekday_schedule.extend(
                        [[0, weekday_schedule[0][1]]] * schedule_items_missing
                    )
                payload[weekday] = weekday_schedule

            ns = self.ns
            payload[ns.key_channel] = self.channel
            if not await self.manager.async_request_ack(
                ns.name,
                mc.METHOD_SET,
                {ns.key: [payload]},
            ):
                # there was an error so we request the actual device state again
                if self.manager.online:
                    await self.manager.async_request(
                        ns.name,
                        mc.METHOD_GET,
                        {ns.key: [{ns.key_channel: self.channel}]},
                    )

    def _get_event_entry(self, event_time: datetime) -> MtsScheduleEntry | None:
        """Search for and return an entry description (MtsScheduleEntry) matching the internal
        schedule representation at the event_time point in time. This in turn helps in translating
        the internal representation to the HA CaleandarEvent used to pass the state to HA.
        event_time is expressed in local time of the device (if it has any configured)
        """
        schedule = self._schedule
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
        schedule = self._schedule
        if not schedule:
            return None
        with self.exception_warning("parsing internal schedule", timeout=14400):
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
            schedule_native_entry = weekday_schedule[schedule_index]
            return MtsScheduleEntry(
                weekday_index=weekday_index,
                index=schedule_index,
                minutes_begin=schedule_minutes_begin,
                minutes_end=schedule_minutes_begin + schedule_native_entry[0],
                day=event_day,
                data=schedule_native_entry,
            )

    def _extract_rfc5545_temp(self, event: dict[str, typing.Any]) -> int:
        match = re.search(r"[-+]?(?:\d*\.*\d+)", event[EVENT_SUMMARY])
        if match:
            return round(
                clamp(
                    float(match.group()),
                    self.climate.min_temp,
                    self.climate.max_temp,
                )
                * self.climate.device_scale
            )
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
        schedule = self._schedule
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
        schedule = self._schedule
        if not schedule:
            raise Exception("Internal state unavailable")
        schedule_unit_time = self._schedule_unit_time
        (
            event_start,
            event_end,
            event_temperature,
        ) = self._extract_rfc5545_info(kwargs)
        # allow only schedule up to midnight: i.e. not spanning multiple days
        event_day_start = event_start.replace(hour=0, minute=0, second=0, microsecond=0)
        event_minutes_start = event_start.hour * 60 + event_start.minute
        event_minutes_start -= event_minutes_start % schedule_unit_time
        event_day_end = event_day_start + timedelta(days=1)
        if event_end > event_day_end:
            raise Exception("Events spanning multiple days are not allowed")
        if event_end == event_day_end:
            event_minutes_end = 1440
        else:
            # round up to the next self._scheduleunittime interval
            event_minutes_end = event_end.hour * 60 + event_end.minute
            event_minutes_end_remainder = event_minutes_end % schedule_unit_time
            if event_minutes_end_remainder:
                event_minutes_end += schedule_unit_time - event_minutes_end_remainder
        event_minutes_duration = event_minutes_end - event_minutes_start
        if event_minutes_duration < schedule_unit_time:
            raise Exception(f"Minimum event duration is {schedule_unit_time} minutes")
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
                if len(rule_parts) == 2 and "BYDAY" in rule_parts:
                    lstdays = {}
                    for day in rule_parts["BYDAY"].split(","):
                        for MtsDays in MTS_SCHEDULE_WEEKDAY:
                            if MtsDays[:2] == day.lower():
                                lstdays[MtsDays] = None
                                break
                    recurrencedays = tuple(lstdays.keys())
                elif len(rule_parts) > 1:
                    raise Exception("Weekly recurrence too complex")
                else:
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
            schedule_entry_count = self._schedule_entry_count_max
            if not schedule_entry_count:
                # we don't have a set limit. Leave as is
                continue

            while len(weekday_schedule) > schedule_entry_count:
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
                # end while (len(weekday_schedule) > schedule_entry_count):

            # end for weekday

    def _build_internal_schedule(self):
        self._schedule = None
        if state := self._native_schedule:
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
            with self.exception_warning("_build_internal_schedule", timeout=14400):
                schedule: MtsScheduleNativeType = {w: [] for w in MTS_SCHEDULE_WEEKDAY}
                for weekday, weekday_schedule in schedule.items():
                    if weekday_state := state.get(weekday):
                        # weekday_state = [[390,150],[90,240],[300,190],[270,220],[300,150],[90,150]]
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

    # message handlers
    def _parse(self, payload: dict):
        # the payload we receive from the device might be partial
        # if we're getting the PUSH in realtime since it only carries
        # the updated entries for the updated day.
        native_schedule = self._native_schedule
        if native_schedule:
            payload = native_schedule | payload
            if payload == native_schedule:
                return
        self._native_schedule = payload
        if mc.KEY_SECTION in payload:
            # mts960 carries 'section' to accomodate the
            # maximum number of entries according to @bernardpe
            # mts200 too carries this field and is likely behaving
            # the same as mts960
            self._schedule_entry_count_min = self._schedule_entry_count_max = payload[
                mc.KEY_SECTION
            ]
            # TODO: check if we can leave _schedule_entry_count_min at 0 (default)
            # since it appears mts200 and mts960 could accept any number between 0
            # and max (key "section"). This needs to be confirmed though.
        self._build_internal_schedule()
        self.flush_state()
