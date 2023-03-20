from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
import typing

from homeassistant.exceptions import HomeAssistantError
import homeassistant.util.dt as dt

from .. import meross_entity as me
from ..calendar import (
    EVENT_END,
    EVENT_RECURRENCE_ID,
    EVENT_RRULE,
    EVENT_START,
    EVENT_SUMMARY,
    EVENT_UID,
    CalendarEntityFeature,
    CalendarEvent,
    MLCalendar,
)
from ..climate import (
    ATTR_TEMPERATURE,
    PRESET_AUTO,
    PRESET_AWAY,
    PRESET_COMFORT,
    PRESET_CUSTOM,
    PRESET_OFF,
    PRESET_SLEEP,
    MtsClimate,
    MtsSetPointNumber,
)
from ..helpers import clamp
from ..merossclient import const as mc  # mEROSS cONST

if typing.TYPE_CHECKING:
    from ..meross_device_hub import MerossSubDevice


class Mts100Climate(MtsClimate):
    """Climate entity for hub paired devices MTS100, MTS100V3, MTS150"""

    MTS_MODE_AUTO = mc.MTS100_MODE_AUTO
    MTS_MODE_TO_PRESET_MAP = {
        mc.MTS100_MODE_CUSTOM: PRESET_CUSTOM,
        mc.MTS100_MODE_HEAT: PRESET_COMFORT,
        mc.MTS100_MODE_COOL: PRESET_SLEEP,
        mc.MTS100_MODE_ECO: PRESET_AWAY,
        mc.MTS100_MODE_AUTO: PRESET_AUTO,
    }
    PRESET_TO_MTS_MODE_MAP = {
        PRESET_CUSTOM: mc.MTS100_MODE_CUSTOM,
        PRESET_COMFORT: mc.MTS100_MODE_HEAT,
        PRESET_SLEEP: mc.MTS100_MODE_COOL,
        PRESET_AWAY: mc.MTS100_MODE_ECO,
        PRESET_AUTO: mc.MTS100_MODE_AUTO,
    }
    # when setting target temp we'll set an appropriate payload key
    # for the mts100 depending on current 'preset' mode.
    # if mts100 is in any of 'off', 'auto' we just set the 'custom'
    # target temp but of course the valve will not follow
    # this temp since it's mode is not set to follow a manual set
    PRESET_TO_TEMPERATUREKEY_MAP = {
        PRESET_OFF: mc.KEY_CUSTOM,
        PRESET_CUSTOM: mc.KEY_CUSTOM,
        PRESET_COMFORT: mc.KEY_COMFORT,
        PRESET_SLEEP: mc.KEY_ECONOMY,
        PRESET_AWAY: mc.KEY_AWAY,
        PRESET_AUTO: mc.KEY_CUSTOM,
    }

    def __init__(self, subdevice: "MerossSubDevice"):
        super().__init__(subdevice.hub, subdevice.id, None, None, subdevice)
        self._attr_extra_state_attributes = {}

    @property
    def scheduleBMode(self):
        return self._attr_extra_state_attributes.get(mc.KEY_SCHEDULEBMODE)

    @scheduleBMode.setter
    def scheduleBMode(self, value):
        if value:
            self._attr_extra_state_attributes[mc.KEY_SCHEDULEBMODE] = value
        else:
            self._attr_extra_state_attributes.pop(mc.KEY_SCHEDULEBMODE)

    async def async_set_preset_mode(self, preset_mode: str):
        if preset_mode == PRESET_OFF:
            await self.async_request_onoff(0)
        else:
            mode = self.PRESET_TO_MTS_MODE_MAP.get(preset_mode)
            if mode is not None:

                def _ack_callback(acknowledge: bool, header: dict, payload: dict):
                    if acknowledge:
                        self._mts_mode = mode
                        self.update_modes()

                await self.device.async_request(
                    mc.NS_APPLIANCE_HUB_MTS100_MODE,
                    mc.METHOD_SET,
                    {mc.KEY_MODE: [{mc.KEY_ID: self.id, mc.KEY_STATE: mode}]},
                    _ack_callback,
                )

                if not self._mts_onoff:
                    await self.async_request_onoff(1)

    async def async_set_temperature(self, **kwargs):
        t = kwargs[ATTR_TEMPERATURE]
        key = self.PRESET_TO_TEMPERATUREKEY_MAP[self._attr_preset_mode or PRESET_CUSTOM]

        def _ack_callback(acknowledge: bool, header: dict, payload: dict):
            if acknowledge:
                self._attr_target_temperature = t
                self.update_modes()

        # when sending a temp this way the device will automatically
        # exit auto mode if needed
        await self.device.async_request(
            mc.NS_APPLIANCE_HUB_MTS100_TEMPERATURE,
            mc.METHOD_SET,
            {
                mc.KEY_TEMPERATURE: [{mc.KEY_ID: self.id, key: int(t * 10)}]
            },  # the device rounds down ?!
            _ack_callback,
        )

    async def async_request_onoff(self, onoff: int):
        def _ack_callback(acknowledge: bool, header: dict, payload: dict):
            if acknowledge:
                self._mts_onoff = onoff
                self.update_modes()

        await self.device.async_request(
            mc.NS_APPLIANCE_HUB_TOGGLEX,
            mc.METHOD_SET,
            {mc.KEY_TOGGLEX: [{mc.KEY_ID: self.id, mc.KEY_ONOFF: onoff}]},
            _ack_callback,
        )


class Mts100SetPointNumber(MtsSetPointNumber):
    """
    customize MtsSetPointNumber to interact with Mts100 family valves
    """

    namespace = mc.NS_APPLIANCE_HUB_MTS100_TEMPERATURE
    key_namespace = mc.KEY_TEMPERATURE
    key_channel = mc.KEY_ID


MTS100_SCHEDULE_WEEKDAY = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
MTS100_SCHEDULE_RRULE = "FREQ=WEEKLY"


@dataclass
class Mts100ScheduleEntry:
    """
    represent the index of an entry in the native schedule dictionary
    of the MTS100
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
    data: list  # actually points to the inner list in the native payload (not a copy)

    def get_event(self) -> CalendarEvent:
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
        return CalendarEvent(
            start=dt.as_utc(event_begin),
            end=dt.as_utc(event_end),
            summary="Target temperature = " + str(self.data[1] / 10),
            uid=f"{MTS100_SCHEDULE_WEEKDAY[self.weekday_index]}#{self.index}",
        )


class Mts100Schedule(MLCalendar):

    _attr_entity_category = me.EntityCategory.CONFIG

    subdevice: MerossSubDevice

    # ScheduleDict = Dict[str, list] # internal schedule dict type alias
    _schedule: dict[str, list] | None
    _attr_state: dict[
        str, list
    ] | None  # device internal scheduleB representation type hint

    def __init__(self, climate: Mts100Climate):
        super().__init__(
            climate.device, climate.id, mc.KEY_SCHEDULE, None, climate.subdevice
        )
        self.climate = climate
        # save a flattened version of the device schedule to ease/optimize CalendarEvent management
        # since the original schedule has a fixed number of contiguous events spanning the day(s) (6 on my MTS100)
        # we might 'compress' these when 2 or more consecutive entries don't change the temperature
        # self._attr_state carries the original unpacked schedule payload from the device representing
        # its effective state
        self._schedule = None
        self._attr_extra_state_attributes = {}
        # get the 'granularity' of the schedule entries i.e. the schedule duration
        # must be a multiple of this time (in minutes)
        self._scheduleunittime = self.device.descriptor.ability.get(
            mc.NS_APPLIANCE_HUB_MTS100_SCHEDULEB, {}
        ).get(mc.KEY_SCHEDULEUNITTIME, 0)
        if self._scheduleunittime:
            self._attr_extra_state_attributes[
                mc.KEY_SCHEDULEUNITTIME
            ] = self._scheduleunittime
        else:
            self._scheduleunittime = (
                15  # fallback to a reasonable defult for internal calculations
            )
        # number of schedules per day supported by the device. Mines default to 6
        # but we're recovering the value by inspecting the device scheduleB payload.
        # Also, this should be the same as scheduleBMode in Mts100Climate
        self._schedule_entry_count = 0

    @property
    def schedule(self):
        if self._schedule is None:
            if state := self._attr_state:
                # state = {
                #   "id": "00000000",
                #   "mon": [[390,150],[90,240],[300,190],[270,220],[300,150],[90,150]],
                #   "tue": [[390,150],[90,240],[300,190],[270,220],[300,150],[90,150]],
                #   "wed": [[390,150],[90,240],[300,190],[270,220],[300,150],[90,150]],
                #   "thu": [[390,150],[90,240],[300,190],[270,220],[300,150],[90,150]],
                #   "fri": [[390,150],[90,240],[300,190],[270,220],[300,150],[90,150]],
                #   "sat": [[390,150],[90,240],[300,190],[270,220],[300,150],[90,150]],
                #   "sun": [[390,150],[90,240],[300,190],[270,220],[300,150],[90,150]]
                #   }
                schedule = {}
                for weekday in MTS100_SCHEDULE_WEEKDAY:
                    weekday_schedule = []
                    if weekday in state:
                        weekday_state = state[weekday]
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
                        current_entry = None
                        for entry in weekday_state:
                            if current_entry is None:
                                # first step
                                current_entry = entry
                                continue
                            if entry[1] == current_entry[1]:  # same T: flatten out
                                # create a copy anyway since we have to be sure not modifing original state
                                current_entry = [
                                    current_entry[0] + entry[0],
                                    current_entry[1],
                                ]
                            else:
                                weekday_schedule.append(current_entry)
                                current_entry = entry
                        if current_entry:
                            weekday_schedule.append(current_entry)
                    schedule[weekday] = weekday_schedule
                self._schedule = schedule

        return self._schedule

    def _get_event_entry(self, event_time: datetime) -> Mts100ScheduleEntry | None:
        """event_time expressed in local time of the device (if it has any configured)"""
        schedule = self.schedule
        if not schedule:
            return None
        weekday_index = event_time.weekday()
        weekday_schedule = schedule.get(MTS100_SCHEDULE_WEEKDAY[weekday_index])
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
                return Mts100ScheduleEntry(
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
        self, event_entry: Mts100ScheduleEntry
    ) -> Mts100ScheduleEntry | None:
        with self.exception_warning("parsing internal schedule", timeout=14400):
            schedule = self.schedule
            if not schedule:
                return None
            weekday_index = event_entry.weekday_index
            weekday_schedule: list = schedule[MTS100_SCHEDULE_WEEKDAY[weekday_index]]
            schedule_index = event_entry.index + 1
            if schedule_index < len(weekday_schedule):
                event_day = event_entry.day
                schedule_minutes_begin = event_entry.minutes_end
            else:
                event_day = event_entry.day + timedelta(days=1)
                weekday_index = event_day.weekday()
                weekday_schedule = schedule[MTS100_SCHEDULE_WEEKDAY[weekday_index]]
                schedule_index = 0
                schedule_minutes_begin = 0
            schedule = weekday_schedule[schedule_index]
            return Mts100ScheduleEntry(
                weekday_index=weekday_index,
                index=schedule_index,
                minutes_begin=schedule_minutes_begin,
                minutes_end=schedule_minutes_begin + schedule[0],
                day=event_day,
                data=schedule,
            )

    @property
    def supported_features(self):
        return CalendarEntityFeature.CREATE_EVENT | CalendarEntityFeature.DELETE_EVENT

    @property
    def event(self) -> CalendarEvent | None:
        """Return the next upcoming event."""
        if self.climate._mts_onoff and self.climate._mts_mode == mc.MTS100_MODE_AUTO:
            event_index = self._get_event_entry(datetime.now(tz=self.device.tzinfo))
            if event_index is not None:
                return event_index.get_event()
        return None

    async def async_get_events(
        self,
        hass,
        start_date: datetime,
        end_date: datetime,
    ) -> list[CalendarEvent]:
        """Return calendar events within a datetime range."""
        events = []
        event_entry = self._get_event_entry(start_date.astimezone(self.device.tzinfo))
        while event_entry is not None:
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
            schedule = self.schedule
            if not schedule:
                raise Exception("Internal state unavailable")
            # get the number of maximum entries for the day from device state
            if self._schedule_entry_count < 1:
                raise Exception("Not enough schedule space available")
            event_start: datetime = kwargs[EVENT_START]
            event_end: datetime = kwargs[EVENT_END]
            try:
                event_temperature = int(
                    clamp(
                        float(kwargs[EVENT_SUMMARY]),
                        self.climate.min_temp,
                        self.climate.max_temp,
                    )
                    * 10
                )
            except Exception as error:
                raise Exception(
                    "Provide a valid °C temperature in the summary field"
                ) from error

            # allow only schedule up to midnight: i.e. not spanning multiple days
            event_day_start = event_start.replace(
                hour=0, minute=0, second=0, microsecond=0
            )
            event_minutes_start = event_start.hour * 60 + event_start.minute
            event_minutes_start -= event_minutes_start % self._scheduleunittime
            event_day_end = event_day_start + timedelta(days=1)
            if event_end > event_day_end:
                raise Exception("Events spanning multiple days are not allowed")
            if event_end == event_day_end:
                event_minutes_end = 1440
            else:
                # round up to the next self._scheduleunittime interval
                event_minutes_end = event_end.hour * 60 + event_end.minute
                event_minutes_end_remainder = event_minutes_end % self._scheduleunittime
                if event_minutes_end_remainder:
                    event_minutes_end += (
                        self._scheduleunittime - event_minutes_end_remainder
                    )
            event_minutes_duration = event_minutes_end - event_minutes_start
            if event_minutes_duration < self._scheduleunittime:
                raise Exception(
                    f"Minimum event duration is {self._scheduleunittime} minutes"
                )

            # recognize some basic recurrence scheme: typically the MTS100 has a weekly schedule
            # and that's by default but we let the user select a daily schedule to setup
            # the same entry among all of the week days
            recurrencedays: tuple
            if rrule := kwargs.get(EVENT_RRULE):
                rule_parts = dict(s.split("=", 1) for s in rrule.split(";"))
                if not (freq := rule_parts.get("FREQ")):
                    raise Exception("Recurrence rule did not contain FREQ")
                if freq == "DAILY":
                    if len(rule_parts) > 1:
                        raise Exception("Daily recurrence too complex")
                    recurrencedays = MTS100_SCHEDULE_WEEKDAY
                elif freq == "WEEKLY":
                    if len(rule_parts) > 1:
                        raise Exception("Weekly recurrence too complex")
                    recurrencedays = (MTS100_SCHEDULE_WEEKDAY[event_start.weekday()],)
                else:
                    raise Exception(f"Invalid frequency for rule: {rrule}")
            else:
                recurrencedays = (MTS100_SCHEDULE_WEEKDAY[event_start.weekday()],)

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
                            # schedule_entry totally overlapped from newer so we'll discard this
                            # and check the next
                            weekday_schedule.pop(schedule_index)
                            if (
                                _event_minutes_duration
                                == schedule_entry_minutes_duration
                            ):
                                break  # exit before accessing maybe non-existing schedule_index
                            assert (
                                _event_minutes_duration
                                >= schedule_entry_minutes_duration
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
                                _event_minutes_duration -= (
                                    schedule_entry_minutes_duration
                                )
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
                        weekday_schedule[schedule_index_insert - 1][
                            0
                        ] += schedule_entry[0]
                        continue
                    schedule_entries_after = (
                        len(weekday_schedule) - schedule_index_insert - 1
                    )
                    if schedule_entries_after > 1:
                        # remove event after
                        schedule_entry = weekday_schedule.pop(schedule_index_insert + 1)
                        # add its duration to its next
                        weekday_schedule[schedule_index_insert + 1][
                            0
                        ] += schedule_entry[0]
                        continue
                    # we're left with an array of 2 entry where 1 is the newly added so
                    # we discard the other and enlarge the last addition to cover the full day
                    schedule_entry = weekday_schedule.pop(1 - schedule_index_insert)
                    weekday_schedule[0][0] += schedule_entry[0]
                    # end while (len(weekday_schedule) > schedule_count):

                # end for weekday
            await self.async_request_schedule()

        except Exception as exception:
            self.log_exception_warning(exception, "async_create_event")
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
            schedule = self.schedule
            if not schedule:
                raise Exception("Internal state unavailable")

            uid_split = uid.split("#")
            weekday_schedule: list = schedule[uid_split[0]]
            # our schedule cannot be empty: it must fill the 24 hours
            if len(weekday_schedule) <= 1:
                raise Exception("The daily schedule must contain at least one event")
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
            await self.async_request_schedule()
        except Exception as error:
            raise HomeAssistantError(str(error)) from error

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

    def update_climate_modes(self):
        # since our state/active event is dependent on climate mode
        # we'll force a state update when the climate entity
        if self._hass_connected:
            self._async_write_ha_state()

    async def async_request_schedule(self):
        if schedule := self.schedule:
            payload = {}
            # the time duration step (minimum interval) of the schedule intervals
            schedule_entry_unittime = self._scheduleunittime
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

            payload[mc.KEY_ID] = self.subdevice.id

            def _ack_callback(acknowledge: bool, header: dict, payload: dict):
                if not acknowledge:
                    self.device.request(
                        mc.NS_APPLIANCE_HUB_MTS100_SCHEDULEB,
                        mc.METHOD_GET,
                        {mc.KEY_SCHEDULE: [{mc.KEY_ID: self.channel}]},
                    )

            await self.device.async_request(
                mc.NS_APPLIANCE_HUB_MTS100_SCHEDULEB,
                mc.METHOD_SET,
                {mc.KEY_SCHEDULE: [payload]},
                _ack_callback,
            )
