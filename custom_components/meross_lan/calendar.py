import dataclasses
from datetime import datetime, timedelta
import re
from typing import TYPE_CHECKING, override

from homeassistant.components import calendar
from homeassistant.components.calendar.const import (
    EVENT_END,
    EVENT_RRULE,
    EVENT_START,
    EVENT_SUMMARY,
)
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.util import dt

from .helpers import clamp
from .helpers.entity import ParserEntity
from .merossclient.exceptions import MerossError, MerossProtocolError
from .merossclient.protocol import const as mc, namespaces as mn

if TYPE_CHECKING:
    from typing import Any, Callable, Final, Mapping, Sequence, Unpack

    from .climate import MtsClimate
    from .merossclient.protocol import types as mt

    # TODO: model the payload structure definition to merossclient.types
    MtsScheduleNativeEntry = list[int]
    MtsScheduleNativeDayEntry = list[MtsScheduleNativeEntry]
    MtsScheduleNativeType = dict[str, MtsScheduleNativeDayEntry]
    MtsScheduleNativeMappingType = Mapping[str, Sequence[Sequence[int]]]


MTS_SCHEDULE_WEEKDAY = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
MTS_SCHEDULE_RRULE = "FREQ=WEEKLY"


def insert_schedule_entry(
    day: "MtsScheduleNativeDayEntry",
    begin: int,
    end: int,
    temperature: int,
    *,
    flatten: bool,
    entry_count_max: int,
) -> "MtsScheduleNativeDayEntry":
    """Returns a new day schedule with minutes [begin, end) set to temperature.
    When entry_count_max is set, entries around the new one are merged to fit."""
    # [begin, end, temperature] segments
    segments: list[list[int]] = []
    segment_begin = 0
    for entry in day:
        segment_end = segment_begin + entry[0]
        segments.append([segment_begin, segment_end, entry[1]])
        segment_begin = segment_end
    new_segment = [begin, end, temperature]
    new_segments = [[b, min(e, begin), t] for b, e, t in segments if b < min(e, begin)]
    new_segments.append(new_segment)
    new_segments += [[max(b, end), e, t] for b, e, t in segments if max(b, end) < e]
    if flatten:
        segments, new_segments = new_segments, new_segments[:1]
        for segment in segments[1:]:
            last_segment = new_segments[-1]
            if segment[2] == last_segment[2]:
                last_segment[1] = segment[1]
                if segment is new_segment:
                    new_segment = last_segment
            else:
                new_segments.append(segment)
    index = new_segments.index(new_segment)
    if entry_count_max:
        while len(new_segments) > entry_count_max:
            if index > 1:
                # merge the entry before the new one into its previous
                index -= 1
                merged_segment = new_segments.pop(index)
                new_segments[index - 1][1] = merged_segment[1]
            elif len(new_segments) - index > 2:
                # merge the entry after the new one into its next
                merged_segment = new_segments.pop(index + 1)
                new_segments[index + 1][0] = merged_segment[0]
            else:
                # at most one neighbor per side: merge one into the new entry
                merged_segment = new_segments.pop(1 - index)
                new_segment[0] = min(new_segment[0], merged_segment[0])
                new_segment[1] = max(new_segment[1], merged_segment[1])
                index = 0
    return [[e - b, t] for b, e, t in new_segments]


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
    data: "MtsScheduleNativeEntry"  # actually points to the inner list in the native payload (not a copy)

    def get_event(self, climate: "MtsClimate") -> calendar.CalendarEvent:
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
            summary=f"{self.data[1] / climate.temperature_scale} {climate.native_temperature_unit}",
            description="",
            uid=f"{MTS_SCHEDULE_WEEKDAY[self.weekday_index]}#{self.index}",
            rrule=MTS_SCHEDULE_RRULE,
        )


class MtsSchedule(ParserEntity, calendar.CalendarEntity):

    if TYPE_CHECKING:
        climate: Final[MtsClimate]  # type: ignore[assignment]

        flatten: Final[bool]  # type: ignore[assignment]
        # save a flattened version of the device schedule to ease/optimize CalendarEvent management
        # since the original schedule has a fixed number of contiguous events spanning the day(s) (6 on my MTS100)
        # we might 'compress' these when 2 or more consecutive entries don't change the temperature
        # ns_value carries the original unpacked schedule payload from the device representing
        # its effective state
        ns_value: MtsScheduleNativeMappingType
        _schedule: MtsScheduleNativeType
        # set the 'granularity' of the schedule entries i.e. the schedule duration
        # must be a multiple of this time (in minutes). It is set lately by customized
        # implementations
        _schedule_unit_time: int
        # number of schedules per day supported by the device. Mines (mts100) default to 6
        # The exact value should be extracted from scheduleBMode for mts100.
        # mts200 instead are showing a "section" == 8 value in their .Schedule payload which
        # could represent this information. Being not sure we're skipping that.
        # The default here (=0) disables any schedule entry count check in building payloads
        # meaning we're sending (more or less) the effective number of entries (per day) as
        # shown/available in the calendar UI.
        _schedule_entry_count_max: int
        _schedule_entry_count_min: int

        class Args(ParserEntity.Args):
            climate: MtsClimate
            ns: mn.Namespace

        def __init__(
            self, *args: Unpack[ParserEntity.InitArgs], **kwargs: Unpack[Args]
        ): ...

    PLATFORM = calendar.DOMAIN

    # HA core entity attributes:
    _attr_entity_category = ParserEntity.EntityCategory.CONFIG
    _attr_name = "Schedule"
    _attr_supported_features = (
        calendar.CalendarEntityFeature.CREATE_EVENT
        | calendar.CalendarEntityFeature.DELETE_EVENT
        | calendar.CalendarEntityFeature.UPDATE_EVENT
    )

    _INIT_flatten = True
    _INIT__schedule = mn.EMPTY_DICT
    _INIT__schedule_unit_time = 15
    _INIT__schedule_entry_count_max = 0
    _INIT__schedule_entry_count_min = 0
    _AUTO_INIT = (
        "climate",
        "flatten",
        "_schedule",
        "_schedule_unit_time",
        "_schedule_entry_count_max",
        "_schedule_entry_count_min",
    )
    _AUTO_DESTROY = ("climate",)
    # Never mixed-in with NamespaceParser so we fix the slots
    __slots__ = ParserEntity._calc_slots()

    def set_unavailable(self):
        self._schedule = mn.EMPTY_DICT
        super().set_unavailable()

    # interface: Calendar
    @property
    def event(self) -> calendar.CalendarEvent | None:
        """Return the next upcoming event."""
        if self.climate.is_mts_scheduled():
            if event_index := self._get_event_entry(datetime.now(tz=self.parent.tz)):
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
        event_entry = self._get_event_entry(start_date.astimezone(self.parent.tz))
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
        await self._async_edit_schedule(
            lambda schedule: self._internal_create_event(schedule, **kwargs)
        )

    async def async_delete_event(
        self,
        uid: str,
        recurrence_id: str | None = None,
        recurrence_range: str | None = None,
    ):
        def _edit(schedule: "MtsScheduleNativeType"):
            if not self._internal_delete_event(schedule, uid):
                raise ServiceValidationError(
                    "The daily schedule must contain at least one event"
                )

        await self._async_edit_schedule(_edit)

    async def async_update_event(
        self,
        uid: str,
        event: dict,
        recurrence_id: str | None = None,
        recurrence_range: str | None = None,
    ) -> None:
        def _edit(schedule: "MtsScheduleNativeType"):
            self._internal_delete_event(schedule, uid)
            self._internal_create_event(schedule, **event)

        await self._async_edit_schedule(_edit)

    # interface: self
    async def _async_edit_schedule(
        self, edit: "Callable[[MtsScheduleNativeType], None]", /
    ):
        """Applies edit to a copy of the schedule and sends it to the device."""
        if not self._schedule:
            raise HomeAssistantError("Schedule not available (device offline?)")
        schedule = {k: [list(e) for e in v] for k, v in self._schedule.items()}
        try:
            edit(schedule)
            await self._async_request_schedule(schedule)
        except HomeAssistantError:
            raise
        except MerossProtocolError as error:
            reason = error.reason
            if isinstance(reason, dict):
                reason = reason.get("message", reason)
            raise HomeAssistantError(
                f"The device refused the schedule: {reason}"
            ) from error
        except MerossError as error:
            self.log_exception(self.WARNING, error, "sending the schedule")
            raise HomeAssistantError(
                "Unable to send the schedule to the device"
            ) from error
        except Exception as exception:
            self.log_exception(self.WARNING, exception, "editing the schedule")
            raise HomeAssistantError("Schedule update failed (see log)") from exception

    async def _async_request_schedule(self, schedule: "MtsScheduleNativeType"):
        payload = {}
        # unpack our schedule struct to be compliant with the device payload:
        # the weekday_schedule must contain between _schedule_entry_count_min and
        # _schedule_entry_count_max
        _schedule_entry_count_max = self._schedule_entry_count_max
        _schedule_entry_count_min = self._schedule_entry_count_min
        for weekday, weekday_schedule in schedule.items():
            if not weekday_schedule:
                # missing in the device state: leave it as is
                continue
            schedule_entry_count = len(weekday_schedule)
            if _schedule_entry_count_max and (
                schedule_entry_count > _schedule_entry_count_max
            ):
                raise HomeAssistantError("Too many elements in the schedule")
            schedule_items_missing = _schedule_entry_count_min - schedule_entry_count
            if schedule_items_missing > 0:
                # our working schedule contains less entries than requested by MTS
                weekday_schedule = list(weekday_schedule)
                weekday_schedule.extend(
                    [[0, weekday_schedule[0][1]]] * schedule_items_missing
                )
            payload[weekday] = weekday_schedule

        # on success the sent payload becomes our state
        await self.async_request_parse(payload)

    def _get_event_entry(self, event_time: datetime) -> MtsScheduleEntry | None:
        """Search for and return an entry description (MtsScheduleEntry) matching the internal
        schedule representation at the event_time point in time. This in turn helps in translating
        the internal representation to the HA CaleandarEvent used to pass the state to HA.
        event_time is expressed in local time of the device (if it has any configured)
        """
        try:
            weekday_index = event_time.weekday()
            weekday_schedule = self._schedule[MTS_SCHEDULE_WEEKDAY[weekday_index]]
        except KeyError:
            return None
        else:
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
        try:
            weekday_index = event_entry.weekday_index
            weekday_schedule = self._schedule[MTS_SCHEDULE_WEEKDAY[weekday_index]]
            schedule_index = event_entry.index + 1
            if schedule_index < len(weekday_schedule):
                event_day = event_entry.day
                schedule_minutes_begin = event_entry.minutes_end
            else:
                event_day = event_entry.day + timedelta(days=1)
                weekday_index = event_day.weekday()
                weekday_schedule = self._schedule[MTS_SCHEDULE_WEEKDAY[weekday_index]]
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
        except KeyError:
            return None
        except Exception as e:
            self.log_exception(self.WARNING, e, "_get_next_event_entry", timeout=14400)
            return None

    def _extract_rfc5545_info(
        self, event: "dict[str, Any]"
    ) -> tuple[datetime, datetime, int]:
        """Returns event start,end,temperature from an RFC5545 dict. Throws exception if
        the temperature cannot be parsed (expecting the SUMMARY field to carry the T value)
        """
        event_start = event[EVENT_START]
        event_end = event[EVENT_END]
        if not (isinstance(event_start, datetime) and isinstance(event_end, datetime)):
            raise ServiceValidationError(
                "All-day events are not supported: provide start and end times"
            )
        match = re.search(r"[-+]?\d+(?:\.\d+)?", event[EVENT_SUMMARY])
        if match:
            climate = self.climate
            return (
                # naive datetimes are in HA local time
                dt.as_local(event_start),
                dt.as_local(event_end),
                round(
                    clamp(
                        float(match.group()),
                        climate.min_temp,
                        climate.max_temp,
                    )
                    * climate.temperature_scale
                ),
            )
        else:
            raise ServiceValidationError(
                "Provide a valid temperature in the summary field"
            )

    def _internal_delete_event(self, schedule: "MtsScheduleNativeType", uid: str):
        weekday, _, index = uid.partition("#")
        weekday_schedule = schedule.get(weekday)
        if not (
            weekday_schedule
            and index.isdecimal()
            and int(index) < len(weekday_schedule)
        ):
            raise ServiceValidationError(f"Invalid event uid: {uid}")
        schedule_index = int(index)
        # our schedule cannot be empty: it must fill the 24 hours
        if len(weekday_schedule) <= 1:
            return False
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

    def _internal_create_event(self, schedule: "MtsScheduleNativeType", **kwargs):
        schedule_unit_time = self._schedule_unit_time
        (
            local_start,
            local_end,
            event_temperature,
        ) = self._extract_rfc5545_info(kwargs)
        device_tz = self.parent.tz
        event_start = local_start.astimezone(device_tz)
        event_end = local_end.astimezone(device_tz)
        # allow only schedule up to midnight: i.e. not spanning multiple days
        event_day_start = event_start.replace(hour=0, minute=0, second=0, microsecond=0)
        event_minutes_start = event_start.hour * 60 + event_start.minute
        event_minutes_start -= event_minutes_start % schedule_unit_time
        event_day_end = event_day_start + timedelta(days=1)
        if event_end > event_day_end:
            raise ServiceValidationError("Events must not cross midnight (device time)")
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
            raise ServiceValidationError(
                f"Minimum event duration is {schedule_unit_time} minutes"
            )
        # recognize some basic recurrence scheme: typically the MTS100 has a weekly schedule
        # and that's by default but we let the user select a daily schedule to setup
        # the same entry among all of the week days
        recurrencedays: tuple
        if event_rrule := kwargs.get(EVENT_RRULE):
            rule_parts = dict(s.split("=", 1) for s in event_rrule.split(";"))
            if not (freq := rule_parts.get("FREQ")):
                raise ServiceValidationError("Recurrence rule did not contain FREQ")
            if freq == "DAILY":
                if len(rule_parts) > 1:
                    raise ServiceValidationError("Daily recurrence too complex")
                recurrencedays = MTS_SCHEDULE_WEEKDAY
            elif freq == "WEEKLY":
                if len(rule_parts) == 2 and "BYDAY" in rule_parts:
                    # BYDAY refers to HA local days: shift them to device days
                    shift = (event_start.date() - local_start.date()).days
                    lstdays = {}
                    for day in rule_parts["BYDAY"].split(","):
                        for index, MtsDays in enumerate(MTS_SCHEDULE_WEEKDAY):
                            if MtsDays[:2] == day.lower():
                                index = (index + shift) % 7
                                lstdays[MTS_SCHEDULE_WEEKDAY[index]] = None
                                break
                        else:
                            raise ServiceValidationError(
                                f"Unsupported recurrence day: {day}"
                            )
                    recurrencedays = tuple(lstdays.keys())
                elif len(rule_parts) > 1:
                    raise ServiceValidationError("Weekly recurrence too complex")
                else:
                    recurrencedays = (MTS_SCHEDULE_WEEKDAY[event_start.weekday()],)
            else:
                raise ServiceValidationError(
                    f"Invalid frequency for rule: {event_rrule}"
                )
        else:
            recurrencedays = (MTS_SCHEDULE_WEEKDAY[event_start.weekday()],)

        for weekday in recurrencedays:
            if not schedule[weekday]:
                raise HomeAssistantError(f"Schedule for {weekday} is not available")
            schedule[weekday] = insert_schedule_entry(
                schedule[weekday],
                event_minutes_start,
                event_minutes_end,
                event_temperature,
                flatten=self.flatten,
                entry_count_max=self._schedule_entry_count_max,
            )

    def _build_internal_schedule(self):
        self._schedule = mn.EMPTY_DICT
        # payload = {
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
            schedule: "MtsScheduleNativeType" = {w: [] for w in MTS_SCHEDULE_WEEKDAY}
            for weekday, weekday_schedule in schedule.items():
                try:
                    weekday_state = self.ns_value[weekday]
                    # weekday_state = [[390,150],[90,240],[300,190],[270,220],[300,150],[90,150]]
                    if self.flatten:
                        current_entry = None
                        for entry in weekday_state:
                            if not entry[0]:
                                # skip zero-duration (padding) entries
                                continue
                            if current_entry and (entry[1] == current_entry[1]):
                                # same T: flatten out
                                current_entry[0] = current_entry[0] + entry[0]
                            else:
                                current_entry = list(entry)
                                weekday_schedule.append(current_entry)
                    else:
                        # don't flatten..but (deep)copy over
                        for entry in weekday_state:
                            if entry[0]:
                                weekday_schedule.append(list(entry))
                except KeyError:
                    # missing day: leave empty
                    continue
            self._schedule = schedule

    # message handlers
    @override
    def __call__(self, payload: "mt.JsonMapping", /):
        # the payload we receive from the device might be partial
        # if we're getting the PUSH in realtime since it only carries
        # the updated entries for the updated day.
        if len(payload) < len(self.ns_value):
            # This is a partial update. It happens when the device
            # pushes a single day array instead of the full week payload.
            payload = self.ns_value | payload  # type: ignore
            if payload == self.ns_value:
                return

        self.ns_value = payload
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


async_setup_entry = MtsSchedule.platform_setup_entry
