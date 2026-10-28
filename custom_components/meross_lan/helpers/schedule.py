"""
Pure (Home Assistant free) conversion between the HA schedule helper format and
the Meross thermostats native Schedule/ScheduleB payload.

Helper format (schedule.get_schedule, meross_lan.set_schedule), times as strings
("HH:MM[:SS[.ffffff]]", "24:00" meaning end of day) or datetime.time (time.max):

    {"monday": [{"from": "06:30", "to": "08:00", "data": {"temperature": 21.5}}]}

Native format: one scaled °C value per key ([minutes, heat, cool] on the mts300),
each day summing to 1440 minutes:

    {"mon": [[duration_minutes, temperature_scaled], ...], ..., "sun": [...]}
"""

from collections.abc import Mapping
import dataclasses
from datetime import time
import math
import re
import typing

if typing.TYPE_CHECKING:
    from collections.abc import Collection, Iterable, Sequence
    from typing import Any

    HelperScheduleType = Mapping[str, Sequence[Mapping[str, Any]] | None]
    NativeEntryType = list[int]
    NativeDayType = list[NativeEntryType]
    NativeScheduleType = dict[str, NativeDayType]
    NativeMappingType = Mapping[str, Any]
    # aligned block: (begin, end, raw "HH:MM-HH:MM" range, scaled values)
    BlockType = tuple[int, int, str, list[Any]]


MTS_SCHEDULE_WEEKDAY = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
HA_SCHEDULE_WEEKDAY = (
    "monday",
    "tuesday",
    "wednesday",
    "thursday",
    "friday",
    "saturday",
    "sunday",
)
MINUTES_PER_DAY = 1440

KEY_FROM = "from"
KEY_TO = "to"
KEY_DATA = "data"
KEY_TEMPERATURE = "temperature"
KEY_HEAT = "heat"
KEY_COOL = "cool"

_TIME_RE = re.compile(r"^([0-9]{1,2}):([0-9]{2})(?::([0-9]{2})(?:\.([0-9]{1,6}))?)?$")


class ScheduleValidationError(ValueError):
    """A schedule (helper or native) that can't be converted: `code` identifies the
    error kind. Programming errors (inconsistent limits) raise plain ValueError."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclasses.dataclass(slots=True)
class CompileResult:
    """The native schedule (the 7 mts weekdays) and the adjustments made."""

    native: "NativeScheduleType"
    warnings: list[str]


def _fmt(minutes: float) -> str:
    """Format minutes since midnight as "HH:MM" ("HH:MM:SS" if not whole).
    1440 is rendered as "24:00"."""
    total_seconds = int(round(minutes * 60))
    h, rem = divmod(total_seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}" if not s else f"{h:02d}:{m:02d}:{s:02d}"


def _fmt_range(begin: float, end: float) -> str:
    return f"{_fmt(begin)}-{_fmt(end)}"


def _round_half_up(value: float) -> int:
    return math.floor(value + 0.5)


def _round_to_unit(minutes: float, unit_time: int) -> int:
    """Rounds minutes to the nearest multiple of unit_time (ties go up)."""
    return _round_half_up(minutes / unit_time) * unit_time


def _to_grid(celsius: float, scale: int, device_fahrenheit: bool) -> int:
    """celsius on the device grid, scaled: the nearest 0.5 °C or, on a °F
    device, the nearest whole °F converted to °C and truncated to the device
    scale (as the devices store it: 75 °F is 2388 at scale 100)."""
    if device_fahrenheit:
        fahrenheit = _round_half_up(celsius * 1.8 + 32)
        return math.trunc((fahrenheit - 32) * 5 * scale / 9)
    return _round_half_up(celsius * 2) * scale // 2


def _to_helper_unit(
    scaled: int, scale: int, fahrenheit: bool, device_fahrenheit: bool = False
) -> float:
    """A device (scaled °C) value in the helper unit (°C or °F): the whole °F a
    °F device grid point represents or else the shortest decimal (up to 2
    digits) converting back to the same scaled value."""
    value = scaled / scale * 1.8 + 32 if fahrenheit else scaled / scale
    if fahrenheit and device_fahrenheit:
        whole = _round_half_up(value)
        if _to_grid((whole - 32) / 1.8, scale, True) == scaled:
            return float(whole)
    for digits in (0, 1):
        rounded = round(value, digits)
        celsius = (rounded - 32) / 1.8 if fahrenheit else rounded
        if _round_half_up(celsius * scale) == scaled:
            return rounded
    return round(value, 2)


def normalize_time(value: "Any") -> float:
    """(Fractional) minutes since midnight of a "HH:MM[:SS[.ffffff]]" string or a
    datetime.time, end of day ("24:00", time.max) being 1440. Raises
    ScheduleValidationError("time_invalid")."""
    if isinstance(value, time):
        h, m, s, us = value.hour, value.minute, value.second, value.microsecond
    elif isinstance(value, str) and (match := _TIME_RE.match(value.strip())):
        hour, minute, second, fraction = match.groups()
        h, m, s = int(hour), int(minute), int(second or 0)
        us = int((fraction or "0").ljust(6, "0"))
        if (h, m, s, us) == (24, 0, 0, 0):
            return float(MINUTES_PER_DAY)
        if h > 23 or m > 59 or s > 59:
            raise ScheduleValidationError(
                "time_invalid", f"Invalid time value: {value!r}"
            )
    else:
        raise ScheduleValidationError("time_invalid", f"Invalid time value: {value!r}")
    if (h, m, s, us) == (23, 59, 59, 999999):
        # time.max is how HA represents 24:00
        return float(MINUTES_PER_DAY)
    return h * 60 + m + s / 60 + us / 60_000_000


def normalize_temperature(
    value: "Any",
    *,
    scale: int,
    min_temp: float,
    max_temp: float,
    fahrenheit: bool = False,
    device_fahrenheit: bool = False,
    keep: "Collection[int]" = (),
) -> tuple[int, str | None]:
    """Converts a temperature (number or numeric string, °F with fahrenheit) to the
    device scaled °C, rounded to the device grid (see _to_grid) unless its exact
    value is in keep, then clamped to [min_temp, max_temp]. Returns (scaled,
    warning), warning None when it reads back (as decompile_schedule does) as the
    input. Raises ScheduleValidationError("temperature_invalid")."""
    try:
        if isinstance(value, bool):
            raise TypeError
        temperature = float(value)
        celsius = (temperature - 32) / 1.8 if fahrenheit else temperature
        # rounding raises ValueError for nan, OverflowError for inf/huge values
        result = _round_half_up(celsius * scale)
        if result not in keep:
            result = _to_grid(celsius, scale, device_fahrenheit)
    except (TypeError, ValueError, OverflowError):
        raise ScheduleValidationError(
            "temperature_invalid", f"Invalid temperature: {value!r}"
        ) from None
    result = max(
        _round_half_up(min_temp * scale),
        min(_round_half_up(max_temp * scale), result),
    )
    adjusted = _to_helper_unit(result, scale, fahrenheit, device_fahrenheit)
    if adjusted == temperature:
        return result, None
    return result, f"temperature {temperature:g} adjusted to {adjusted:g}"


def compile_schedule(
    helper: "HelperScheduleType",
    *,
    scale: int,
    min_temp: float,
    max_temp: float,
    unit_time: int = 15,
    entry_count_max: int = 0,
    entry_count_min: int = 0,
    keys: "Sequence[str]" = (KEY_TEMPERATURE,),
    fahrenheit: bool = False,
    device_fahrenheit: bool = False,
    off_values: "Sequence[int] | None" = None,
    keep: "Collection[int]" = (),
) -> CompileResult:
    """
    Compiles an HA schedule helper into the native device payload: every block is
    an entry (equal ones are not merged) and the time not covered by any block (gaps,
    missing or empty days) is off, an entry carrying off_values (default: max_temp
    for 'cool', min_temp else). Block boundaries are rounded to the nearest
    unit_time (ties up: touching blocks keep touching, zero-length ones are dropped),
    temperatures as normalize_temperature does (helper values in °F with
    fahrenheit). Days over entry_count_max (0: unlimited) raise, those under
    entry_count_min are padded with zero-duration entries. With several keys
    (mts300 heat/cool) a missing value is carried forward (see _carry_forward) and
    heat must be below cool.
    """
    if unit_time < 1 or MINUTES_PER_DAY % unit_time:
        raise ValueError(
            f"unit_time must be a divisor of {MINUTES_PER_DAY}: {unit_time}"
        )
    if entry_count_max and entry_count_min > entry_count_max:
        raise ValueError("entry_count_min cannot exceed entry_count_max")
    if min_temp > max_temp:
        raise ValueError("min_temp cannot exceed max_temp")
    if off_values is None:
        off_values = [
            _round_half_up((max_temp if key == KEY_COOL else min_temp) * scale)
            for key in keys
        ]
    elif len(off_values) != len(keys):
        raise ValueError("off_values must carry one value per key")

    for key in helper:
        if key not in HA_SCHEDULE_WEEKDAY:
            raise ScheduleValidationError("unknown_weekday", f"Unknown weekday '{key}'")

    warnings: list[str] = []
    temp_kwargs = {
        "scale": scale,
        "min_temp": min_temp,
        "max_temp": max_temp,
        "fahrenheit": fahrenheit,
        "device_fahrenheit": device_fahrenheit,
        "keep": keep,
    }
    week = {
        day: _parse_day(
            day,
            helper.get(day) or (),
            keys=keys,
            unit_time=unit_time,
            temp_kwargs=temp_kwargs,
            warnings=warnings,
        )
        for day in HA_SCHEDULE_WEEKDAY
    }
    if not any(week.values()):
        raise ScheduleValidationError("schedule_empty", "the schedule has no blocks")
    if len(keys) > 1:
        # invalid blocks raise: fewer blocks kept means some were dropped
        dropped = sum(len(b or ()) for b in helper.values()) > sum(
            map(len, week.values())
        )
        note = f" (blocks shorter than {unit_time} minutes are dropped)"
        _carry_forward(week, keys, warnings, note if dropped else "")
    if tuple(keys) == (KEY_HEAT, KEY_COOL):
        for day, blocks in week.items():
            for _, _, rng, values in blocks:
                if values[0] >= values[1]:
                    heat, cool = (
                        _to_helper_unit(v, scale, fahrenheit, device_fahrenheit)
                        for v in values
                    )
                    raise ScheduleValidationError(
                        "setpoint_order",
                        f"{day}: block {rng}: {KEY_HEAT} {heat:g} must be below "
                        f"{KEY_COOL} {cool:g}",
                    )
    native: NativeScheduleType = {
        mts_day: _compile_day(
            ha_day,
            week[ha_day],
            off_values,
            entry_count_max=entry_count_max,
            entry_count_min=entry_count_min,
        )
        for ha_day, mts_day in zip(HA_SCHEDULE_WEEKDAY, MTS_SCHEDULE_WEEKDAY)
    }
    return CompileResult(native, warnings)


def _parse_day(
    day: str,
    blocks: "Iterable[Any]",
    *,
    keys: "Sequence[str]",
    unit_time: int,
    temp_kwargs: "dict[str, Any]",
    warnings: list[str],
) -> "list[BlockType]":
    """Parses and validates the blocks of a single (HA named) weekday, then aligns
    them to unit_time. Returns the blocks kept, sorted by time. Missing values
    (multi-key mode only) are None."""
    parsed: list[tuple[float, float, str, list[Any]]] = []
    for index, block in enumerate(blocks):
        if (
            not isinstance(block, Mapping)
            or KEY_FROM not in block
            or KEY_TO not in block
        ):
            raise ScheduleValidationError(
                "block_invalid",
                f"{day}: block #{index} must contain '{KEY_FROM}' and '{KEY_TO}'",
            )
        data = block.get(KEY_DATA)
        if not isinstance(data, Mapping):
            data = {}
        try:
            begin = normalize_time(block[KEY_FROM])
            end = normalize_time(block[KEY_TO])
            rng = _fmt_range(begin, end)
            values: list[int | None] = []
            for key in keys:
                if (value := data.get(key)) is None:
                    if len(keys) == 1:
                        raise ScheduleValidationError(
                            "block_missing_temperature", f"no '{key}' value"
                        )
                    values.append(None)
                    continue
                scaled, warning = normalize_temperature(value, **temp_kwargs)
                if warning:
                    label = f"{key} " if len(keys) > 1 else ""
                    warnings.append(f"{day}: block {rng}: {label}{warning}")
                values.append(scaled)
        except ScheduleValidationError as e:
            raise ScheduleValidationError(
                e.code, f"{day}: block #{index}: {e}"
            ) from None
        if begin >= end:
            raise ScheduleValidationError(
                "block_invalid_range",
                f"{day}: block {rng} must end after it starts "
                "(blocks cannot cross midnight)",
            )
        parsed.append((begin, end, rng, values))

    # rounding is monotonic so it cannot introduce overlaps: check the raw times
    parsed.sort(key=lambda b: (b[0], b[1]))
    for prev, cur in zip(parsed, parsed[1:]):
        if cur[0] < prev[1]:
            raise ScheduleValidationError(
                "block_overlap", f"{day}: blocks {prev[2]} and {cur[2]} overlap"
            )

    result: list[BlockType] = []
    for begin, end, rng, values in parsed:
        r_begin = _round_to_unit(begin, unit_time)
        r_end = _round_to_unit(end, unit_time)
        if r_begin == r_end:
            warnings.append(
                f"{day}: block {rng} shorter than {unit_time} minutes dropped"
            )
            continue
        if r_begin != begin or r_end != end:
            warnings.append(
                f"{day}: block {rng} aligned to {_fmt_range(r_begin, r_end)} "
                f"({unit_time} minutes granularity)"
            )
        result.append((r_begin, r_end, rng, values))
    return result


def _carry_forward(
    week: "dict[str, list[BlockType]]",
    keys: "Sequence[str]",
    warnings: list[str],
    note: str,
):
    """Multi-key mode: a block missing a key takes the value of the previous kept
    block in time (monday wraps to sunday). Raises ScheduleValidationError
    ("missing_value", 'note' appended) when no block carries the key."""
    timeline = [values for day in HA_SCHEDULE_WEEKDAY for *_, values in week[day]]
    for i, key in enumerate(keys):
        # the week wraps: the last value of the week precedes monday
        current = next(
            (values[i] for values in reversed(timeline) if values[i] is not None),
            None,
        )
        if current is None:
            raise ScheduleValidationError(
                "missing_value", f"no block in the week has a '{key}' value{note}"
            )
        count = 0
        for values in timeline:
            if values[i] is None:
                values[i] = current
                count += 1
            else:
                current = values[i]
        if count:
            plural = "s" if count > 1 else ""
            warnings.append(f"{key} carried forward into {count} block{plural}")


def _compile_day(
    day: str,
    blocks: "list[BlockType]",
    off_values: "Sequence[int]",
    *,
    entry_count_max: int,
    entry_count_min: int,
) -> "NativeDayType":
    """Builds the native entries of a single (HA named) weekday: one per aligned
    block and one carrying off_values per uncovered span."""
    entries: NativeDayType = []
    cursor = 0
    for begin, end, _, values in blocks:
        if begin > cursor:
            entries.append([begin - cursor, *off_values])
        entries.append([end - begin, *values])
        cursor = end
    if cursor < MINUTES_PER_DAY:
        entries.append([MINUTES_PER_DAY - cursor, *off_values])

    if entry_count_max and len(entries) > entry_count_max:
        raise ScheduleValidationError(
            "too_many_entries",
            f"{day}: {len(entries)} slots exceed the device limit of "
            f"{entry_count_max} (gaps take a slot each, blocks with the same "
            "temperature count separately)",
        )
    for _ in range(entry_count_min - len(entries)):
        entries.append([0, *entries[0][1:]])
    return entries


def decompile_schedule(
    native: "NativeMappingType",
    *,
    scale: int,
    keys: "Sequence[str]" = (KEY_TEMPERATURE,),
    fahrenheit: bool = False,
    device_fahrenheit: bool = False,
    off_values: "Sequence[int] | None" = None,
) -> "dict[str, list[dict[str, Any]]]":
    """
    Converts the weekdays of a native payload to the helper format ("HH:MM" times,
    values in °C or, with fahrenheit, °F: see _to_helper_unit). Every entry is a
    block except zero-duration ones and, with off_values, off entries (gaps): an
    off entry following another one stays a block (two gaps would compile to one
    entry), as all do when the week would have no blocks. Raises
    ScheduleValidationError("native_invalid") for malformed payloads.
    """
    off = None if off_values is None else list(off_values)
    shape = f"[minutes, {', '.join(keys)}]"
    result: dict[str, list[dict[str, Any]]] = {}
    for ha_day, mts_day in zip(HA_SCHEDULE_WEEKDAY, MTS_SCHEDULE_WEEKDAY):
        if mts_day not in native:
            continue
        day_entries = native[mts_day]
        if not isinstance(day_entries, (list, tuple)):
            raise ScheduleValidationError(
                "native_invalid", f"{mts_day}: entries must be a list"
            )
        blocks = result[ha_day] = []
        cursor = 0
        previous = None
        for index, entry in enumerate(day_entries):
            if not (
                isinstance(entry, (list, tuple))
                and len(entry) == 1 + len(keys)
                and all(type(value) is int for value in entry)
            ):
                raise ScheduleValidationError(
                    "native_invalid", f"{mts_day}: entry #{index} is not {shape}"
                )
            duration, *values = entry
            if duration < 0 or cursor + duration > MINUTES_PER_DAY:
                raise ScheduleValidationError(
                    "native_invalid",
                    f"{mts_day}: entry #{index} has an invalid duration {duration}",
                )
            if duration == 0:
                continue
            begin, cursor = cursor, cursor + duration
            gap = values == off and previous != off
            previous = values
            if gap:
                continue
            blocks.append(
                {
                    KEY_FROM: _fmt(begin),
                    KEY_TO: _fmt(cursor),
                    KEY_DATA: {
                        key: _to_helper_unit(
                            value, scale, fahrenheit, device_fahrenheit
                        )
                        for key, value in zip(keys, values)
                    },
                }
            )
        if day_entries and cursor != MINUTES_PER_DAY:
            raise ScheduleValidationError(
                "native_invalid",
                f"{mts_day}: entries cover {cursor} minutes "
                f"instead of {MINUTES_PER_DAY}",
            )
    if off is not None and not any(result.values()):
        # only off entries: kept as blocks
        return decompile_schedule(
            native,
            scale=scale,
            keys=keys,
            fahrenheit=fahrenheit,
            device_fahrenheit=device_fahrenheit,
        )
    return result


def _native_week(
    native: "NativeMappingType", merge: bool
) -> "list[NativeDayType | None]":
    """The 7 mts weekdays of a native payload (None when missing) without zero
    (or negative) duration entries and, with merge, with consecutive entries
    carrying equal values merged."""
    week: list[NativeDayType | None] = []
    for day in MTS_SCHEDULE_WEEKDAY:
        if (entries := native.get(day)) is not None:
            result: NativeDayType = []
            for duration, *values in entries:
                if duration <= 0:
                    continue
                if merge and result and result[-1][1:] == values:
                    result[-1][0] += duration
                else:
                    result.append([duration, *values])
            entries = result
        week.append(entries)
    return week


def native_equal(a: "NativeMappingType", b: "NativeMappingType") -> bool:
    """
    True when two native payloads run the same schedule: their 7 mts weekdays
    are compared ignoring padding and borders between entries with equal values
    (other keys like channel/section/id are ignored; a weekday present on one
    side only makes them different).
    """
    return _native_week(a, True) == _native_week(b, True)


def native_same(a: "NativeMappingType", b: "NativeMappingType") -> bool:
    """
    True when two native payloads carry the same entries: like native_equal but
    split entries with equal values differ from a single one.
    """
    return _native_week(a, False) == _native_week(b, False)
