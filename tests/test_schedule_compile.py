"""Test the pure helpers.schedule module (HA schedule helper <-> native payload)"""

from datetime import time
import math

import pytest

from custom_components.meross_lan.helpers.schedule import (
    HA_SCHEDULE_WEEKDAY,
    KEY_COOL,
    KEY_HEAT,
    KEY_TEMPERATURE,
    MTS_SCHEDULE_WEEKDAY,
    ScheduleValidationError,
    compile_schedule,
    decompile_schedule,
    native_equal,
    native_same,
    normalize_temperature,
    normalize_time,
)

from tests.schedule_helpers import MTS100_TRACE, MTS100_TRACE_MONDAY, B, week

# device limits as set up by the climate schedule for the different families
MTS100 = dict(
    scale=10,
    unit_time=15,
    entry_count_max=6,
    entry_count_min=6,
    min_temp=5.0,
    max_temp=35.0,
)
MTS200 = dict(
    scale=10,
    unit_time=15,
    entry_count_max=8,
    entry_count_min=0,
    min_temp=5.0,
    max_temp=35.0,
)
MTS960 = dict(
    scale=100,
    unit_time=15,
    entry_count_max=12,
    entry_count_min=0,
    min_temp=0.0,
    max_temp=50.0,
)
# mts300 (provisional): two setpoints per entry, 12 slots per day (unconfirmed)
MTS300 = dict(
    scale=100,
    unit_time=15,
    entry_count_max=12,
    entry_count_min=0,
    min_temp=5.0,
    max_temp=35.0,
    keys=(KEY_HEAT, KEY_COOL),
)
MTS100_TEMP = dict(scale=10, min_temp=5.0, max_temp=35.0)
MTS100_TEMP_F = dict(MTS100_TEMP, fahrenheit=True)
MTS960_TEMP = dict(MTS100_TEMP, scale=100)
# °F device (whole °F grid, truncated to the device scale), helper in °C or °F
MTS100_TEMP_DF = dict(MTS100_TEMP, device_fahrenheit=True)
MTS100_TEMP_FF = dict(MTS100_TEMP_DF, fahrenheit=True)
MTS960_TEMP_DF = dict(MTS960_TEMP, device_fahrenheit=True)
MTS960_TEMP_FF = dict(MTS960_TEMP_DF, fahrenheit=True)
# helper values in °F (on a °F device for the mts300)
MTS100_F = dict(MTS100, fahrenheit=True)
MTS300_F = dict(MTS300, fahrenheit=True, device_fahrenheit=True)
# °F device
MTS200_DF = dict(MTS200, device_fahrenheit=True)
MTS300_DF = dict(MTS300, device_fahrenheit=True)
# verbatim from emulator_traces/U0123456789012345678901234567891F-Kpippo-mts960.csv
# (line 95, Appliance.Control.Thermostat.ScheduleB GETACK)
MTS960_TRACE = {
    "channel": 0,
    "section": 12,
    "sun": [[1440, 1900]],
    "mon": [[1440, 100]],
    "tue": [[1440, 2800]],
    "wed": [[1440, 100]],
    "thu": [[1440, 100]],
    "fri": [[15, 100], [1425, 2000]],
    "sat": [[1440, 100]],
}
# verbatim from emulator_traces/U01234567890123456789012345678925-Kpippo-mts300.json.txt
# (Appliance.Control.Thermostat.ScheduleB state: the same every day)
MTS300_TRACE_925 = {
    "pre": {"enable": 2, "time": 30},
    **{
        d: [[360, 2111, 2611], [900, 2111, 2388], [180, 2111, 2611]]
        for d in MTS_SCHEDULE_WEEKDAY
    },
    "channel": 0,
}
# verbatim from emulator_traces/U01234567890123456789012345678928-Kpippo-mts300.json.txt
# (Appliance.Control.Thermostat.ScheduleB GETACK)
MTS300_TRACE_928 = {
    "channel": 0,
    "pre": {"enable": 2, "time": 30},
    **{d: [[1440, 1900, 2560]] for d in MTS_SCHEDULE_WEEKDAY},
}
# verbatim from emulator_traces/U0123456789012345678901234567890F-Kpippo-msh300-2024-02-23_06-57-23.csv
# (line 95, Appliance.Hub.Mts100.ScheduleB GETACK): the leading 15-minute entries
# are the padding written by older meross_lan versions
MSH300_LEGACY_TRACE_8C11 = {
    "id": "01008C11",
    **{
        d: [[15, 180], [45, 180], [300, 230], [360, 180], [360, 240], [360, 180]]
        for d in ("mon", "tue", "thu", "fri", "sat", "sun")
    },
    "wed": [[15, 105], [45, 105], [300, 230], [360, 105], [360, 240], [360, 105]],
}
MSH300_LEGACY_TRACE_783A = {
    "id": "0100783A",
    "mon": [[15, 100], [15, 100], [15, 100], [15, 100], [300, 100], [1080, 100]],
    **{
        d: [[15, 100], [15, 100], [15, 100], [15, 100], [300, 230], [1080, 100]]
        for d in ("tue", "wed", "thu", "fri", "sat", "sun")
    },
}


FULL_DAY = [
    B("00:00", "06:30", 15),
    B("06:30", "13:00", 18),
    B("13:00", "21:30", 21),
    B("21:30", "24:00", 15),
]
FULL_DAY_MTS100 = [[390, 150], [390, 180], [510, 210], [150, 150], [0, 150], [0, 150]]
FULL_DAY_MTS300 = [
    B("00:00", "06:00", heat=19, cool=26),
    B("06:00", "21:00", heat=21, cool=24),
    B("21:00", "24:00", heat=18, cool=27),
]


def raises_code(code: str, func, *args, **kwargs) -> ScheduleValidationError:
    with pytest.raises(ScheduleValidationError) as excinfo:
        func(*args, **kwargs)
    error = excinfo.value
    assert error.code == code, str(error)
    return error


# --------------------------------------------------------------------------
# normalize_time
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    "value, expected",
    [
        ("06:30", 390),
        ("6:30", 390),
        (" 06:30 ", 390),
        ("06:30:30", 390.5),
        ("00:00", 0),
        ("23:59", 1439),
        ("24:00", 1440),
        ("24:00:00", 1440),
        ("23:59:59.999999", 1440),
        (time(6, 30), 390),
        (time(0, 0), 0),
        (time.max, 1440),
    ],
)
def test_normalize_time(value, expected):
    assert normalize_time(value) == expected


@pytest.mark.parametrize(
    "value",
    # "٦:30": non-ASCII (arabic-indic) digit
    ["25:00", "06:60", "24:30", "24:00:01", "6h30", "٦:30", 630, 6.5, None, ""],
)
def test_normalize_time_invalid(value):
    error = raises_code("time_invalid", normalize_time, value)
    assert repr(value) in str(error)


# --------------------------------------------------------------------------
# normalize_temperature
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    "value, kwargs, expected, warning",
    [
        (21.5, MTS100_TEMP, 215, None),
        (21.3, MTS100_TEMP, 215, "temperature 21.3 adjusted to 21.5"),
        (21.2, MTS100_TEMP, 210, "temperature 21.2 adjusted to 21"),
        ("21.5", MTS100_TEMP, 215, None),
        (21, MTS100_TEMP, 210, None),
        (40, MTS100_TEMP, 350, "temperature 40 adjusted to 35"),
        (2, MTS100_TEMP, 50, "temperature 2 adjusted to 5"),
        (21.5, MTS960_TEMP, 2150, None),
        (1.0, dict(scale=100, min_temp=0.0, max_temp=50.0), 100, None),
        # result compared against the unscaled input
        (21.04, MTS100_TEMP, 210, "temperature 21.04 adjusted to 21"),
        # single rounding (no double rounding through the scaled int)
        (21.246, MTS960_TEMP, 2100, "temperature 21.246 adjusted to 21"),
        # °F helper, °C device: on the 0.5 °C grid after the conversion
        (70, MTS100_TEMP_F, 210, "temperature 70 adjusted to 69.8"),
        # °F device: nearest whole °F, truncated to the device scale
        (21, MTS960_TEMP_DF, 2111, "temperature 21 adjusted to 21.11"),
        (21, MTS100_TEMP_DF, 211, "temperature 21 adjusted to 21.1"),
        (70, MTS960_TEMP_FF, 2111, None),
        # a whole °F reads back as is
        (75, MTS960_TEMP_FF, 2388, None),
        (75.3, MTS960_TEMP_FF, 2388, "temperature 75.3 adjusted to 75"),
        (66, MTS100_TEMP_FF, 188, None),
        (68, MTS100_TEMP_FF, 200, None),
        (100, MTS100_TEMP_FF, 350, "temperature 100 adjusted to 95"),
        (32, MTS100_TEMP_FF, 50, "temperature 32 adjusted to 41"),
        # keep: values the device holds are not rounded (but still clamped)
        (21.25, dict(MTS960_TEMP_DF, keep={2125}), 2125, None),
        (21.25, MTS960_TEMP, 2150, "temperature 21.25 adjusted to 21.5"),
        (40, dict(MTS100_TEMP, keep={400}), 350, "temperature 40 adjusted to 35"),
    ],
)
def test_normalize_temperature(value, kwargs, expected, warning):
    scaled, text = normalize_temperature(value, **kwargs)
    assert (scaled, text) == (expected, warning)
    assert type(scaled) is int


@pytest.mark.parametrize(
    "value", [True, False, "warm", math.nan, math.inf, None, 1e308, "-1e308"]
)
def test_normalize_temperature_invalid(value):
    error = raises_code(
        "temperature_invalid", normalize_temperature, value, **MTS100_TEMP
    )
    assert repr(value) in str(error)


# --------------------------------------------------------------------------
# compile_schedule: happy paths
# --------------------------------------------------------------------------
@pytest.mark.parametrize("kwargs", [MTS100, MTS200, MTS960])
def test_compile_full_day(kwargs):
    result = compile_schedule(week(FULL_DAY), **kwargs)
    assert result.warnings == []
    assert list(result.native) == list(MTS_SCHEDULE_WEEKDAY)
    # padded up to entry_count_min (mts100) repeating the first entry values
    count = max(4, kwargs["entry_count_min"])
    factor = kwargs["scale"] // 10
    expected = [[d, t * factor] for d, t in FULL_DAY_MTS100[:count]]
    assert result.native == {day: expected for day in MTS_SCHEDULE_WEEKDAY}
    # every entry is a fresh list (no aliasing)
    entries = [id(entry) for day in result.native.values() for entry in day]
    assert len(set(entries)) == len(entries) == 7 * count


def test_compile_mirrors_equal_temperatures():
    # every block is an entry: touching blocks with the same value are not merged
    blocks = [
        B("00:00", "06:00", 15),
        B("06:00", "08:00", 21),
        B("08:00", "12:00", 21),
        B("12:00", "24:00", 15),
    ]
    result = compile_schedule(week(blocks), **MTS200)
    assert result.native["mon"] == [[360, 150], [120, 210], [240, 210], [720, 150]]


def test_compile_unsorted_input():
    shuffled = [FULL_DAY[2], FULL_DAY[0], FULL_DAY[3], FULL_DAY[1]]
    assert (
        compile_schedule(week(shuffled), **MTS100).native
        == compile_schedule(week(FULL_DAY), **MTS100).native
    )


def test_compile_time_objects():
    blocks = [
        B(time(0, 0), time(6, 30), 15),
        B(time(6, 30), time(13, 0), 18),
        B(time(13, 0), time(21, 30), 21),
        B(time(21, 30), time.max, 15),
    ]
    result = compile_schedule(week(blocks), **MTS100)
    assert result.warnings == []
    assert result.native["mon"] == FULL_DAY_MTS100


def test_compile_entry_count_max_unlimited():
    blocks = [B(f"{h:02d}:00", f"{h + 1:02d}:00", 15 + (h % 2)) for h in range(20)]
    blocks.append(B("20:00", "24:00", 21))
    kwargs = dict(MTS200, entry_count_max=0)
    result = compile_schedule(week(blocks), **kwargs)
    assert len(result.native["mon"]) == 21
    assert sum(entry[0] for entry in result.native["mon"]) == 1440


def test_compile_temperature_warnings():
    blocks = [B("00:00", "12:00", 21.3), B("12:00", "24:00", 40)]
    result = compile_schedule(week(FULL_DAY, monday=blocks), **MTS200)
    assert result.native["mon"] == [[720, 215], [720, 350]]
    assert result.warnings == [
        "monday: block 00:00-12:00: temperature 21.3 adjusted to 21.5",
        "monday: block 12:00-24:00: temperature 40 adjusted to 35",
    ]


# --------------------------------------------------------------------------
# compile_schedule: alignment to unit_time
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    "blocks, unit_time, expected, warnings",
    [
        # nearest unit: touching blocks keep touching
        (
            [B("00:00", "06:10", 15), B("06:10", "08:05", 21), B("08:05", "24:00", 15)],
            15,
            [[375, 150], [105, 210], [960, 150]],
            [
                "block 00:00-06:10 aligned to 00:00-06:15 (15 minutes granularity)",
                "block 06:10-08:05 aligned to 06:15-08:00 (15 minutes granularity)",
                "block 08:05-24:00 aligned to 08:00-24:00 (15 minutes granularity)",
            ],
        ),
        # ties go up
        (
            [B("00:00", "07:07:30", 15), B("07:07:30", "24:00", 21)],
            15,
            [[435, 150], [1005, 210]],
            None,
        ),
        (
            [B("00:00", "07:07:29", 15), B("07:07:29", "24:00", 21)],
            15,
            [[420, 150], [1020, 210]],
            None,
        ),
        # under half a unit: dropped (no 'aligned' warning)
        (
            [B("00:00", "06:00", 15), B("06:00", "06:05", 21), B("06:05", "24:00", 15)],
            15,
            [[360, 150], [1080, 150]],
            [
                "block 06:00-06:05 shorter than 15 minutes dropped",
                "block 06:05-24:00 aligned to 06:00-24:00 (15 minutes granularity)",
            ],
        ),
        # over half a unit: kept
        (
            [B("00:00", "06:00", 15), B("06:00", "06:08", 21), B("06:08", "24:00", 15)],
            15,
            [[360, 150], [15, 210], [1065, 150]],
            None,
        ),
        # a short gap is closed
        (
            [B("00:00", "06:00", 15), B("06:05", "24:00", 21)],
            15,
            [[360, 150], [1080, 210]],
            None,
        ),
        # a 1 minute gap
        ([B("00:00", "23:59", 15)], 1, [[1439, 150], [1, 50]], []),
    ],
)
def test_compile_alignment(blocks, unit_time, expected, warnings):
    result = compile_schedule(week(blocks), **dict(MTS200, unit_time=unit_time))
    assert result.native["mon"] == expected
    if warnings is not None:
        assert [w for w in result.warnings if w.startswith("monday:")] == [
            f"monday: {w}" for w in warnings
        ]


# --------------------------------------------------------------------------
# compile_schedule: uncovered time is off
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    "blocks, kwargs, expected",
    [
        # gap, trailing
        (
            [B("00:00", "06:00", 15), B("08:00", "22:00", 21)],
            MTS200,
            [[360, 150], [120, 50], [840, 210], [120, 50]],
        ),
        # gap next to a block carrying the off value (not merged)
        (
            [B("00:00", "06:00", 5), B("08:00", "24:00", 21)],
            MTS200,
            [[360, 50], [120, 50], [960, 210]],
        ),
        # leading, custom off value
        (
            [B("06:30", "24:00", 21)],
            dict(MTS200, off_values=[100]),
            [[390, 100], [1050, 210]],
        ),
        # after the alignment, padding repeats the first entry
        (
            [B("00:00", "08:00", 15), B("08:10", "24:00", 21)],
            MTS100,
            [[480, 150], [15, 50], [945, 210]] + [[0, 150]] * 3,
        ),
        # mts300 default: heat min_temp, cool max_temp
        (
            [B("06:00", "24:00", heat=21, cool=24)],
            MTS300,
            [[360, 500, 3500], [1080, 2100, 2400]],
        ),
        (
            [B("06:00", "24:00", heat=21, cool=24)],
            dict(MTS300, off_values=(1000, 3000)),
            [[360, 1000, 3000], [1080, 2100, 2400]],
        ),
    ],
)
def test_compile_off(blocks, kwargs, expected):
    assert compile_schedule(week(blocks), **kwargs).native["mon"] == expected


@pytest.mark.parametrize("empty", ["missing", None, []])
def test_compile_empty_day(empty):
    helper = week(FULL_DAY, tuesday=[B("00:00", "12:00", 15), B("12:00", "24:00", 21)])
    if empty == "missing":
        del helper["wednesday"]
    else:
        helper["wednesday"] = empty
    result = compile_schedule(helper, **MTS100)
    assert result.warnings == []
    assert result.native["wed"] == [[1440, 50]] + [[0, 50]] * 5
    assert result.native["thu"] == FULL_DAY_MTS100


def test_compile_single_day():
    friday = [B("06:30", "22:00", 21), B("22:00", "24:00", 17)]
    result = compile_schedule({"friday": friday}, **MTS200)
    assert result.warnings == []
    assert result.native == {
        day: [[390, 50], [930, 210], [120, 170]] if day == "fri" else [[1440, 50]]
        for day in MTS_SCHEDULE_WEEKDAY
    }


@pytest.mark.parametrize(
    "helper",
    [
        {},
        {day: [] for day in HA_SCHEDULE_WEEKDAY},
        {day: None for day in HA_SCHEDULE_WEEKDAY},
        # every block dropped (shorter than 15 minutes)
        week([B("06:00", "06:05", 21, heat=20, cool=26)]),
    ],
)
@pytest.mark.parametrize("kwargs", [MTS100, MTS300])
def test_error_schedule_empty(helper, kwargs):
    error = raises_code("schedule_empty", compile_schedule, helper, **kwargs)
    assert str(error) == "the schedule has no blocks"


# --------------------------------------------------------------------------
# compile_schedule: errors
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    "helper, code, message",
    [
        (
            {"monday": [B("06:30", "24:00", 21), B("00:00", "07:00", 15)]},
            "block_overlap",
            "monday: blocks 00:00-07:00 and 06:30-24:00 overlap",
        ),
        (
            {"monday": [B("06:00", "06:00", 15)]},
            "block_invalid_range",
            "monday: block 06:00-06:00 must end after it starts",
        ),
        (
            {"monday": [B("08:00", "06:00", 15)]},
            "block_invalid_range",
            "monday: block 08:00-06:00 must end after it starts",
        ),
        *(
            (
                {"friday": [B("00:00", "06:00", 15), block]},
                "block_invalid",
                "friday: block #1 must contain 'from' and 'to'",
            )
            for block in (
                {"from": "00:00", "data": {"temperature": 15}},
                {"to": "24:00", "data": {"temperature": 15}},
                "00:00-24:00",
                ["00:00", "24:00", 15],
                None,
            )
        ),
        (
            {"monday": [B("00:00", "25:00", 15)]},
            "time_invalid",
            "monday: block #0: Invalid time value: '25:00'",
        ),
        *(
            (
                {"monday": [block]},
                "block_missing_temperature",
                "monday: block #0: no 'temperature' value",
            )
            for block in ({"from": "00:00", "to": "24:00"}, B("00:00", "24:00"))
        ),
        (
            {"monday": [B("00:00", "24:00", "warm")]},
            "temperature_invalid",
            "monday: block #0: Invalid temperature: 'warm'",
        ),
        # weekday keys are exact (lowercase) names
        ({"mon": FULL_DAY}, "unknown_weekday", "Unknown weekday 'mon'"),
        ({"Monday": FULL_DAY}, "unknown_weekday", "Unknown weekday 'Monday'"),
    ],
)
def test_compile_error(helper, code, message):
    error = raises_code(code, compile_schedule, helper, **MTS100)
    assert str(error).startswith(message)


@pytest.mark.parametrize(
    "start, values, end",
    [
        ((0, 1, 2, 3, 4, 5, 6), (15, 16, 15, 16, 15, 16, 21), "24:00"),
        # never merged to fit
        ((0, 1, 2, 3, 4, 5, 6), (15,) * 7, "24:00"),
        # uncovered time takes a slot, also next to a block with the off value
        ((1, 2, 3, 4, 5, 6), (21, 18, 21, 18, 21, 18), "24:00"),
        ((1, 2, 3, 4, 5, 6), (5, 18, 21, 18, 21, 18), "24:00"),
        ((0, 1, 2, 3, 4, 5), (21, 18, 21, 18, 21, 18), "06:00"),
        ((1, 3, 5), (21, 18, 21), "06:00"),
        # fits: 5 blocks and the leading gap
        ((1, 2, 3, 4, 5), (21, 18, 21, 18, 21), "24:00"),
    ],
)
def test_entry_count_max(start, values, end):
    blocks = [B(f"{h:02d}:00", f"{h + 1:02d}:00", v) for h, v in zip(start, values)]
    blocks[-1]["to"] = end
    helper = week(FULL_DAY, monday=blocks)
    if len(blocks) == 5:
        assert len(compile_schedule(helper, **MTS100).native["mon"]) == 6
        return
    error = raises_code("too_many_entries", compile_schedule, helper, **MTS100)
    assert str(error) == (
        "monday: 7 slots exceed the device limit of 6 (gaps take a slot each, "
        "blocks with the same temperature count separately)"
    )


@pytest.mark.parametrize(
    "limits",
    [
        {"unit_time": 7},
        {"unit_time": 0},
        {"unit_time": -15},
        {"min_temp": 40.0},
        {"entry_count_min": 8},
        {"off_values": (50, 350)},
    ],
)
def test_error_bad_limits(limits):
    with pytest.raises(ValueError) as excinfo:
        compile_schedule(week(FULL_DAY), **(MTS100 | limits))
    # a programming error, not a ScheduleValidationError
    assert type(excinfo.value) is ValueError


# --------------------------------------------------------------------------
# compile_schedule: multiple keys (mts300 heat/cool)
# --------------------------------------------------------------------------
def test_compile_mts300():
    result = compile_schedule(week(FULL_DAY_MTS300), **MTS300)
    assert result.warnings == []
    for day in MTS_SCHEDULE_WEEKDAY:
        assert result.native[day] == [
            [360, 1900, 2600],
            [900, 2100, 2400],
            [180, 1800, 2700],
        ]


def test_compile_mts300_temperature_warnings():
    blocks = [B("00:00", "24:00", heat=21.333, cool=40)]
    result = compile_schedule(week(FULL_DAY_MTS300, monday=blocks), **MTS300)
    assert result.native["mon"] == [[1440, 2150, 3500]]
    assert result.warnings == [
        "monday: block 00:00-24:00: heat temperature 21.333 adjusted to 21.5",
        "monday: block 00:00-24:00: cool temperature 40 adjusted to 35",
    ]


@pytest.mark.parametrize(
    "overrides, day, expected, warnings",
    [
        # unsorted: 'previous' is in time, not in the list
        (
            {
                "monday": [
                    B("08:00", "24:00", cool=24),
                    B("00:00", "06:00", heat=19, cool=26),
                    B("06:00", "08:00", heat=21),
                ]
            },
            "mon",
            [[360, 1900, 2600], [120, 2100, 2600], [960, 2100, 2400]],
            ["heat carried forward into 1 block", "cool carried forward into 1 block"],
        ),
        # tuesday starts from monday last block (heat 18, cool 27)
        (
            {"tuesday": [B("00:00", "12:00"), B("12:00", "24:00", heat=20)]},
            "tue",
            [[720, 1800, 2700], [720, 2000, 2700]],
            ["heat carried forward into 1 block", "cool carried forward into 2 blocks"],
        ),
        # monday starts from sunday last block
        (
            {
                "monday": [
                    B("00:00", "12:00", heat=20),
                    B("12:00", "24:00", heat=21, cool=25),
                ],
                "sunday": [B("00:00", "24:00", heat=17, cool=28)],
            },
            "mon",
            [[720, 2000, 2800], [720, 2100, 2500]],
            ["cool carried forward into 1 block"],
        ),
        # a dropped block (shorter than 15 minutes) carries nothing
        (
            {
                "monday": [
                    B("00:00", "06:00", heat=19, cool=26),
                    B("06:00", "06:05", heat=20, cool=30),
                    B("06:05", "24:00", heat=21),
                ]
            },
            "mon",
            [[360, 1900, 2600], [1080, 2100, 2600]],
            ["cool carried forward into 1 block"],
        ),
    ],
)
def test_compile_mts300_carry_forward(overrides, day, expected, warnings):
    helper = week(FULL_DAY_MTS300, **overrides)
    before = repr(helper)
    result = compile_schedule(helper, **MTS300)
    assert result.native[day] == expected
    assert [w for w in result.warnings if "carried" in w] == warnings
    assert repr(helper) == before  # not modified


def test_error_mts300_reported_on_its_block():
    # the invalid value is on sunday: monday (compiled first) carries it forward
    helper = week(
        FULL_DAY_MTS300,
        monday=[B("00:00", "24:00", heat=20)],
        sunday=[B("00:00", "24:00", heat=17, cool="warm")],
    )
    error = raises_code("temperature_invalid", compile_schedule, helper, **MTS300)
    assert str(error) == "sunday: block #0: Invalid temperature: 'warm'"
    # a bad time is a time error even when the block misses a value
    helper = week(FULL_DAY_MTS300, friday=[B("00:00", "25:00", heat=20)])
    error = raises_code("time_invalid", compile_schedule, helper, **MTS300)
    assert str(error) == "friday: block #0: Invalid time value: '25:00'"


@pytest.mark.parametrize(
    "blocks, key, note",
    [
        ([B("00:00", "24:00", heat=20)], "cool", ""),
        ([B("00:00", "24:00", cool=26)], "heat", ""),
        # single setpoint helper
        (FULL_DAY, "heat", ""),
        # the only block with a 'cool' value is dropped
        (
            [
                B("00:00", "12:00", heat=20),
                B("12:00", "12:05", cool=26),
                B("12:05", "24:00", heat=21),
            ],
            "cool",
            " (blocks shorter than 15 minutes are dropped)",
        ),
    ],
)
def test_error_mts300_missing_value(blocks, key, note):
    error = raises_code("missing_value", compile_schedule, week(blocks), **MTS300)
    assert str(error) == f"no block in the week has a '{key}' value{note}"


@pytest.mark.parametrize(
    "heat, cool, kwargs, message",
    [
        (24, 24, MTS300, "heat 24 must be below cool 24"),
        (25, 24, MTS300, "heat 25 must be below cool 24"),
        # after clamping to the device range
        (36, 40, MTS300, "heat 35 must be below cool 35"),
        # in the helper unit
        (75, 74, MTS300_F, "heat 75 must be below cool 74"),
    ],
)
def test_error_mts300_setpoint_order(heat, cool, kwargs, message):
    helper = {"wednesday": [B("06:00", "24:00", heat=heat, cool=cool)]}
    error = raises_code("setpoint_order", compile_schedule, helper, **kwargs)
    assert str(error) == f"wednesday: block 06:00-24:00: {message}"


# --------------------------------------------------------------------------
# decompile_schedule
# --------------------------------------------------------------------------
def test_decompile_mts100_trace():
    helper = decompile_schedule(MTS100_TRACE, scale=10)
    assert list(helper) == list(HA_SCHEDULE_WEEKDAY)
    for day in HA_SCHEDULE_WEEKDAY[:5]:
        assert helper[day] == MTS100_TRACE_MONDAY
    assert helper["sunday"][:2] == [B("00:00", "06:30", 15.5), B("06:30", "09:15", 17)]


def test_decompile_mts960_trace():
    helper = decompile_schedule(MTS960_TRACE, scale=100)
    assert helper["friday"] == [
        {"from": "00:00", "to": "00:15", "data": {"temperature": 1.0}},
        {"from": "00:15", "to": "24:00", "data": {"temperature": 20.0}},
    ]
    assert helper["monday"] == [
        {"from": "00:00", "to": "24:00", "data": {"temperature": 1.0}}
    ]
    assert helper["tuesday"] == [
        {"from": "00:00", "to": "24:00", "data": {"temperature": 28.0}}
    ]
    assert set(helper) == set(HA_SCHEDULE_WEEKDAY)


def test_decompile_mts300_traces():
    keys = MTS300["keys"]
    helper = decompile_schedule(MTS300_TRACE_925, scale=100, keys=keys)
    assert list(helper) == list(HA_SCHEDULE_WEEKDAY)
    for day in HA_SCHEDULE_WEEKDAY:
        assert helper[day] == [
            {"from": "00:00", "to": "06:00", "data": {"heat": 21.11, "cool": 26.11}},
            {"from": "06:00", "to": "21:00", "data": {"heat": 21.11, "cool": 23.88}},
            {"from": "21:00", "to": "24:00", "data": {"heat": 21.11, "cool": 26.11}},
        ]
    helper = decompile_schedule(MTS300_TRACE_928, scale=100, keys=keys)
    for day in HA_SCHEDULE_WEEKDAY:
        assert helper[day] == [
            {"from": "00:00", "to": "24:00", "data": {"heat": 19.0, "cool": 25.6}}
        ]
    # in °F: the shortest decimal mapping back to the device value or, on a °F
    # device, the whole °F it represents
    for device_fahrenheit, cool in ((False, 74.98), (True, 75.0)):
        helper = decompile_schedule(
            MTS300_TRACE_925,
            scale=100,
            keys=keys,
            fahrenheit=True,
            device_fahrenheit=device_fahrenheit,
        )
        assert helper["monday"][1]["data"] == {"heat": 70.0, "cool": cool}
    # the entry width must match the keys
    raises_code("native_invalid", decompile_schedule, MTS300_TRACE_925, scale=100)
    raises_code("native_invalid", decompile_schedule, MTS100_TRACE, scale=10, keys=keys)


def test_decompile_skips_zero_duration():
    native = {"mon": [[360, 150], [0, 210], [360, 150], [720, 210], [0, 150]]}
    assert decompile_schedule(native, scale=10) == {
        "monday": [
            {"from": "00:00", "to": "06:00", "data": {"temperature": 15.0}},
            {"from": "06:00", "to": "12:00", "data": {"temperature": 15.0}},
            {"from": "12:00", "to": "24:00", "data": {"temperature": 21.0}},
        ]
    }


def test_decompile_partial_week():
    native = {"mon": [[1440, 150]], "tue": []}
    assert decompile_schedule(native, scale=10) == {
        "monday": [{"from": "00:00", "to": "24:00", "data": {"temperature": 15.0}}],
        "tuesday": [],
    }


@pytest.mark.parametrize(
    "monday, kwargs, off_values, expected",
    [
        # gaps (padding skipped)
        (
            [[390, 50], [90, 180], [960, 50], [0, 180], [0, 50], [0, 50]],
            MTS100,
            [50],
            [B("06:30", "08:00", 18.0)],
        ),
        # an off entry right after another one stays a block (gaps would merge)
        (
            [[300, 50], [0, 210], [300, 50], [840, 210]],
            MTS200,
            [50],
            [B("05:00", "10:00", 5.0), B("10:00", "24:00", 21.0)],
        ),
        # mts300: off when both values are
        (
            [[360, 500, 3500], [720, 500, 2600], [360, 1900, 3500]],
            MTS300,
            [500, 3500],
            [
                B("06:00", "18:00", heat=5.0, cool=26.0),
                B("18:00", "24:00", heat=19.0, cool=35.0),
            ],
        ),
        # only off entries in the week: kept (an empty schedule doesn't compile)
        ([[1440, 50]], MTS200, [50], [B("00:00", "24:00", 5.0)]),
    ],
)
def test_decompile_off_values(monday, kwargs, off_values, expected):
    keys = kwargs.get("keys", (KEY_TEMPERATURE,))
    native = {"mon": monday}
    helper = decompile_schedule(
        native, scale=kwargs["scale"], keys=keys, off_values=off_values
    )
    assert helper == {"monday": expected}
    # the gaps compile back to the off entries
    result = compile_schedule(helper, **(kwargs | {"off_values": off_values}))
    assert native_same({"mon": result.native["mon"]}, native)


@pytest.mark.parametrize(
    "day",
    [
        [[720, 150], [710, 210]],
        [[-10, 150], [1450, 210]],
        [[720, 150], [730, 210]],
        [[1440, 150, 200]],
        [[0, 150], [0, 150]],
        [[1440]],
        [[]],
        [None],
        [["a", 150]],
        [[1440, 21.5]],
        [[1440.0, 150]],
        [[1440, True]],
        None,
    ],
)
def test_decompile_invalid(day):
    error = raises_code("native_invalid", decompile_schedule, {"mon": day}, scale=10)
    assert str(error).startswith("mon: ")


# --------------------------------------------------------------------------
# round trips (decompile, then compile)
# --------------------------------------------------------------------------
def _grid_case(kwargs, device_fahrenheit):
    """Every grid point from 5 to 35 °C (0.5 °C or whole °F) as monday entries."""
    scale = kwargs["scale"]
    if device_fahrenheit:
        grid = [(f - 32) * 5 * scale // 9 for f in range(41, 96)]
    else:
        grid = [h * scale // 2 for h in range(10, 71)]
    native = {"mon": [[15, v] for v in grid] + [[1440 - 15 * len(grid), grid[-1]]]}
    kwargs = dict(kwargs, entry_count_max=0, device_fahrenheit=device_fahrenheit)
    return native, kwargs, grid[:1], True


def _compiled_case(kwargs):
    """A compiled helper: times and values adjusted, a gap (cool 5 above heat)."""
    keys = kwargs.get("keys", (KEY_TEMPERATURE,))
    blocks = [
        B(begin, end, **{key: value + 5 * i for i, key in enumerate(keys)})
        for begin, end, value in (
            ("00:00", "06:10", 15),
            ("06:10", "08:05", 21.3),
            ("17:00", "24:00", 15),
        )
    ]
    native = compile_schedule(week(blocks), **kwargs).native
    # the gap (third entry) carries the off values
    return native, kwargs, native["mon"][2][1:], True


@pytest.mark.parametrize("fahrenheit", [False, True])
@pytest.mark.parametrize("gaps", [False, True])
@pytest.mark.parametrize(
    "native, kwargs, off_values, on_grid",
    [
        (MTS100_TRACE, MTS100, [150], True),
        # entries with equal values (legacy 15-minute padding too) are kept
        (MSH300_LEGACY_TRACE_8C11, MTS100, [180], True),
        (MSH300_LEGACY_TRACE_783A, MTS100, [100], True),
        (MTS960_TRACE, MTS960, [100], True),
        # whole °F (21.11 is 70 °F): on the °F device grid
        (MTS300_TRACE_925, MTS300_DF, [2111, 2611], True),
        # 25.6 is on no grid: exact only when kept (only off entries: no gaps)
        (MTS300_TRACE_928, MTS300_DF, [1900, 2560], False),
        *(_grid_case(kw, df) for kw in (MTS200, MTS960) for df in (False, True)),
        *map(_compiled_case, (MTS100, MTS200, MTS960, MTS300, MTS200_DF, MTS300_DF)),
    ],
)
def test_roundtrip(native, kwargs, off_values, on_grid, gaps, fahrenheit):
    """Device schedules decompile to helpers compiling back to them exactly (no
    warnings) when the values the device holds are kept (keep) and, if on the
    device grid, also when not. With gaps, off entries are left as gaps."""
    keys = kwargs.get("keys", (KEY_TEMPERATURE,))
    device_fahrenheit = kwargs.get("device_fahrenheit", False)
    helper = decompile_schedule(
        native,
        scale=kwargs["scale"],
        keys=keys,
        fahrenheit=fahrenheit,
        device_fahrenheit=device_fahrenheit,
        off_values=off_values if gaps else None,
    )
    if fahrenheit and device_fahrenheit and on_grid:
        # whole °F on a °F device
        blocks = [block for blocks in helper.values() for block in blocks]
        assert all(v == round(v) for b in blocks for v in b["data"].values())
    days = {day: native[day] for day in MTS_SCHEDULE_WEEKDAY if day in native}
    kwargs = kwargs | {"fahrenheit": fahrenheit, "off_values": off_values}
    held = {v for entries in days.values() for _, *values in entries for v in values}
    for keep in (held, ()):
        result = compile_schedule(helper, keep=keep, **kwargs)
        result_days = {day: result.native[day] for day in days}
        if keep or on_grid:
            assert (result_days, result.warnings) == (days, [])
        else:
            assert result_days != days


# --------------------------------------------------------------------------
# native_equal / native_same
# --------------------------------------------------------------------------
def _native(**overrides):
    result = {day: [[720, 150], [720, 210]] for day in MTS_SCHEDULE_WEEKDAY}
    result.update(overrides)
    return result


NO_SUNDAY = {day: [[720, 150], [720, 210]] for day in MTS_SCHEDULE_WEEKDAY[:6]}


@pytest.mark.parametrize(
    "a, b, equal, same",
    [
        # padding, extra keys and tuples don't matter
        (_native(), _native(mon=[[720, 150], [720, 210], [0, 150]]), True, True),
        (_native(), {**_native(), "id": "03002B0E", "channel": 0}, True, True),
        (_native(), _native(mon=((720, 150), (720, 210))), True, True),
        # borders between equal values only matter to native_same
        (_native(), _native(mon=[[360, 150], [360, 150], [720, 210]]), True, False),
        (_native(), _native(mon=[[720, 150], [720, 215]]), False, False),
        (_native(), _native(mon=[[705, 150], [735, 210]]), False, False),
        # a weekday missing on one side only
        (_native(), NO_SUNDAY, False, False),
        (NO_SUNDAY, dict(NO_SUNDAY), True, True),
        # any entry width
        (
            {"mon": [[300, 200, 250], [0, 180, 260], [300, 200, 250], [840, 180, 260]]},
            {"mon": [[600, 200, 250], [840, 180, 260]]},
            True,
            False,
        ),
    ],
)
def test_native_equal_same(a, b, equal, same):
    before = repr((a, b))
    for x, y in ((a, b), (b, a)):
        assert native_equal(x, y) == equal
        assert native_same(x, y) == same
    # inputs are not modified
    assert repr((a, b)) == before
