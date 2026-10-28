"""Test the meross_lan.get_schedule / set_schedule / sync_schedule entity services"""

from copy import deepcopy
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, patch

from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.util.unit_system import US_CUSTOMARY_SYSTEM
import pytest

try:
    import probatio as vol
except ImportError:
    import voluptuous as vol

from custom_components.meross_lan import const as mlc, schedule_sync
from custom_components.meross_lan.calendar import MtsSchedule
from custom_components.meross_lan.helpers.schedule import (
    HA_SCHEDULE_WEEKDAY,
    MTS_SCHEDULE_WEEKDAY,
    native_equal,
)
from custom_components.meross_lan.merossclient.protocol import const as mc
from custom_components.meross_lan.schedule_sync import (
    ScheduleHelperSelect as Select,
    ScheduleSyncSensor as Sensor,
)

from tests.schedule_helpers import (
    HELPER_ENTITY_ID,
    MTS100_TRACE,
    MTS100_TRACE_MONDAY,
    MTS300_TRACE_DAY,
    MTS300_TRACE_PRE,
    OTHER_SCHEDULE,
    SCHEDULE_FULL,
    B,
    async_schedule_context,
    build_mts200_emulator,
    build_mts300_emulator,
    patch_read_helper,
)

if TYPE_CHECKING:
    from typing import Any

    from homeassistant.core import HomeAssistant

MTS100_TRACE_DAYS = {day: MTS100_TRACE[day] for day in MTS_SCHEDULE_WEEKDAY}


async def test_get_schedule(request, hass: "HomeAssistant"):
    async with async_schedule_context(request, hass) as ctx:
        climate = ctx.climate
        await ctx.async_set_device_schedule(MTS100_TRACE)
        assert native_equal(climate.schedule.ns_value, MTS100_TRACE)

        response = await ctx.async_call(mlc.SERVICE_GET_SCHEDULE, climate.entity_id)
        assert response
        result: "dict[str, Any]" = response[climate.entity_id]  # type: ignore
        assert result.keys() == {"schedule", "limits", "linked_helper"}
        assert result["schedule"]["monday"] == MTS100_TRACE_MONDAY
        assert result["limits"] == {
            "min_temp": climate.min_temp,
            "max_temp": climate.max_temp,
            "device_unit": "°C",
            "keys": ["temperature"],
            "unit_time": 15,
            "entry_count_max": 6,
        }
        assert result["linked_helper"] is None

        # the 'schedule' key can be pasted back as set_schedule input
        await ctx.async_call(
            mlc.SERVICE_SET_SCHEDULE, climate.entity_id, schedule=result["schedule"]
        )
        assert len(ctx.set_payloads) == 1
        assert ctx.emulator_schedule() == MTS100_TRACE_DAYS

        # unsupported device payload (3-value entries like mts300): device data,
        # not a user input error
        await ctx.async_set_device_schedule(
            {day: [[720, 180, 200], [720, 200, 220]] for day in MTS_SCHEDULE_WEEKDAY}
        )
        with pytest.raises(HomeAssistantError) as excinfo:
            await ctx.async_call(mlc.SERVICE_GET_SCHEDULE, climate.entity_id)
        assert not isinstance(excinfo.value, ServiceValidationError)


@pytest.mark.parametrize(
    "build_device, entry_count_max",
    [(lambda: mc.TYPE_MSH300, 6), (build_mts200_emulator, 8)],
    ids=["mts100", "mts200"],
)
async def test_set_schedule(
    request, hass: "HomeAssistant", build_device, entry_count_max
):
    """Hub.Mts100.ScheduleB (scheduleBMode slots per day) and Thermostat.Schedule
    (mts200: 'section')."""
    async with async_schedule_context(request, hass, build_device()) as ctx:
        climate = ctx.climate
        assert climate.schedule.limits["entry_count_max"] == entry_count_max
        expected = ctx.compile(SCHEDULE_FULL)
        assert not native_equal(ctx.emulator_schedule(), expected)
        await ctx.async_call(
            mlc.SERVICE_SET_SCHEDULE, climate.entity_id, schedule=SCHEDULE_FULL
        )
        assert len(ctx.set_payloads) == 1
        assert ctx.emulator_schedule() == expected
        # the device schedule is queried back after the write
        assert native_equal(climate.schedule.ns_value, expected)
        response = await ctx.async_call(mlc.SERVICE_GET_SCHEDULE, climate.entity_id)
        assert response[climate.entity_id]["schedule"] == SCHEDULE_FULL  # type: ignore


async def test_set_schedule_partial_week(request, hass: "HomeAssistant"):
    """Uncovered time (missing days included) is off (the valve min_temp) and
    reads back as such (gaps). The compile warnings (here: a time rounded) are
    returned."""
    async with async_schedule_context(request, hass) as ctx:
        climate = ctx.climate
        entity_id = climate.entity_id
        off = round(climate.min_temp * 10)
        monday = [B("06:31", "21:00", 21), B("22:00", "24:00", 17)]
        response = await ctx.async_call(
            mlc.SERVICE_SET_SCHEDULE, entity_id, True, schedule={"monday": monday}
        )
        assert len(response[entity_id]["warnings"]) == 1  # type: ignore
        assert len(ctx.set_payloads) == 1
        assert ctx.emulator_schedule() == {
            day: (
                [[390, off], [870, 210], [60, off], [120, 170]] + [[0, off]] * 2
                if day == "mon"
                else [[1440, off]] + [[0, off]] * 5
            )
            for day in MTS_SCHEDULE_WEEKDAY
        }
        response = await ctx.async_call(mlc.SERVICE_GET_SCHEDULE, entity_id)
        schedule = response[entity_id]["schedule"]  # type: ignore
        assert schedule == {day: [] for day in HA_SCHEDULE_WEEKDAY} | {
            "monday": [B("06:30", "21:00", 21), B("22:00", "24:00", 17)]
        }
        # ...compiling back to the device schedule
        assert ctx.compile(schedule) == ctx.emulator_schedule()


@pytest.mark.parametrize(
    "schedule, error, match",
    [
        (
            {"monday": [B("00:00", "07:00", 16), B("06:00", "24:00", 21)]},
            ServiceValidationError,
            "overlap",
        ),
        ({}, ServiceValidationError, "no blocks"),
        # 7 blocks and a gap on mts100 (scheduleBMode: 6)
        (
            {
                "tuesday": [
                    B(f"{h:02d}:00", f"{h + 1:02d}:00", 16 + h) for h in range(7)
                ]
            },
            ServiceValidationError,
            "exceed the device limit",
        ),
        (
            {"friday": [{"from": "00:00", "to": "24:00"}]},
            ServiceValidationError,
            "temperature",
        ),
        # rejected by the service schema: missing 'to', unknown weekday
        (
            {"monday": [{"from": "00:00", "data": {"temperature": 16}}]},
            vol.Invalid,
            None,
        ),
        ({"mon": [B("00:00", "24:00", 16)]}, vol.Invalid, None),
    ],
)
async def test_set_schedule_invalid(
    request, hass: "HomeAssistant", schedule, error, match
):
    async with async_schedule_context(request, hass) as ctx:
        initial_state = deepcopy(ctx.emulator_schedule())
        with pytest.raises(error, match=match):
            await ctx.async_call(
                mlc.SERVICE_SET_SCHEDULE, ctx.climate.entity_id, schedule=schedule
            )
        # nothing was written
        assert not ctx.set_payloads
        assert ctx.emulator_schedule() == initial_state


async def test_sync_schedule(request, hass: "HomeAssistant"):
    """meross_lan.sync_schedule."""
    async with async_schedule_context(request, hass) as ctx:
        climate = ctx.climate

        async def _async_sync_schedule():
            await ctx.async_call(mlc.SERVICE_SYNC_SCHEDULE, climate.entity_id)

        with pytest.raises(ServiceValidationError, match="No schedule helper linked"):
            await _async_sync_schedule()

        select = ctx.select
        await select.async_select_option(HELPER_ENTITY_ID)
        # it runs now (no debounce)
        await _async_sync_schedule()
        assert len(ctx.set_payloads) == 1
        expected = ctx.compile(await select._async_read_helper(HELPER_ENTITY_ID))
        assert ctx.emulator_schedule() == expected
        assert native_equal(climate.schedule.ns_value, expected)
        assert ctx.sensor_state().state == Sensor.STATE_SYNCED
        # forced: written even if unchanged
        await _async_sync_schedule()
        assert len(ctx.set_payloads) == 2
        response = await ctx.async_call(mlc.SERVICE_GET_SCHEDULE, climate.entity_id)
        assert response[climate.entity_id]["linked_helper"] == HELPER_ENTITY_ID  # type: ignore

        # device schedule not known: the (still forced) run is deferred
        with patch.object(type(climate), "schedule_limits_known", return_value=False):
            with pytest.raises(HomeAssistantError, match="sync will be retried"):
                await _async_sync_schedule()
            assert ctx.sensor_state().state == Sensor.STATE_PENDING
        assert len(ctx.set_payloads) == 2
        await ctx.async_tick(Select.RETRY_DELAY)
        assert len(ctx.set_payloads) == 3
        assert ctx.sensor_state().state == Sensor.STATE_SYNCED

        # the device acknowledges the write but keeps its schedule (also when
        # queried again: no delay, the time is frozen while the service runs)
        await ctx.async_drift()
        with (
            patch.object(
                MtsSchedule,
                "async_request_payload",
                AsyncMock(return_value=SimpleNamespace(method=mc.METHOD_SETACK)),
            ),
            patch.object(schedule_sync, "READBACK_DELAY", 0),
        ):
            with pytest.raises(HomeAssistantError, match="did not keep"):
                await _async_sync_schedule()
        assert ctx.sensor_state().state == Sensor.STATE_DRIFT


async def test_sync_schedule_offline(request, hass: "HomeAssistant"):
    """On an offline device the climate entity is unavailable and HA skips it:
    set_schedule and sync_schedule do nothing (no error) while get_schedule
    fails since no entity can respond."""
    async with async_schedule_context(request, hass) as ctx:
        climate = ctx.climate
        await ctx.select.async_select_option(HELPER_ENTITY_ID)
        await ctx.async_tick(Select.SYNC_DELAY)
        assert len(ctx.set_payloads) == 1

        ctx.context.device.on_disconnect()
        assert not climate.available
        await ctx.async_call(mlc.SERVICE_SYNC_SCHEDULE, climate.entity_id)
        await ctx.async_call(
            mlc.SERVICE_SET_SCHEDULE, climate.entity_id, schedule=SCHEDULE_FULL
        )
        with pytest.raises(HomeAssistantError):
            await ctx.async_call(mlc.SERVICE_GET_SCHEDULE, climate.entity_id)
        assert len(ctx.set_payloads) == 1


async def test_set_schedule_wins(request, hass: "HomeAssistant"):
    """set_schedule overrides the linked helper (reported as drift): a pending
    helper write is dropped and only later helper edits are written."""
    async with async_schedule_context(request, hass) as ctx:
        entity_id = ctx.climate.entity_id
        select = ctx.select
        await select.async_select_option(HELPER_ENTITY_ID)
        await ctx.async_tick(Select.SYNC_DELAY)
        assert len(ctx.set_payloads) == 1
        expected = ctx.compile(SCHEDULE_FULL)
        with patch_read_helper(OTHER_SCHEDULE):
            # (a) helper edited and pushed while the device is offline: deferred
            with patch.object(ctx.climate, "is_connected", False):
                with pytest.raises(HomeAssistantError, match="sync will be retried"):
                    await select.async_push()
            await ctx.async_call(
                mlc.SERVICE_SET_SCHEDULE, entity_id, schedule=SCHEDULE_FULL
            )
            await ctx.async_tick(Select.RETRY_DELAY)
            ctx.fire_helper_event()  # e.g. the helper next block
            await ctx.async_tick(Select.SYNC_DELAY)
            assert len(ctx.set_payloads) == 2
            assert ctx.emulator_schedule() == expected
            assert ctx.sensor_state().state == Sensor.STATE_DRIFT
        # a later edit (back to HELPER_BLOCKS) is written
        ctx.fire_helper_event()
        await ctx.async_tick(Select.SYNC_DELAY)
        assert len(ctx.set_payloads) == 3
        assert ctx.sensor_state().state == Sensor.STATE_SYNCED
        helper_native = ctx.emulator_schedule()

        # (b) the helper is selected while set_schedule writes: written
        async with ctx.async_hold_writes() as async_write_started:
            task = hass.async_create_task(
                ctx.async_call(
                    mlc.SERVICE_SET_SCHEDULE, entity_id, schedule=SCHEDULE_FULL
                )
            )
            await async_write_started()
            await select.async_select_option(HELPER_ENTITY_ID)
        await task
        await ctx.async_tick(Select.SYNC_DELAY)
        assert len(ctx.set_payloads) == 5
        assert ctx.emulator_schedule() == helper_native
        assert ctx.sensor_state().state == Sensor.STATE_SYNCED


async def test_schedule_services_fahrenheit(request, hass: "HomeAssistant"):
    """Schedule values are in the HA unit system (°F here)."""
    hass.config.units = US_CUSTOMARY_SYSTEM
    async with async_schedule_context(request, hass) as ctx:
        climate = ctx.climate
        await ctx.async_set_device_schedule(MTS100_TRACE)
        response = await ctx.async_call(mlc.SERVICE_GET_SCHEDULE, climate.entity_id)
        result: "dict[str, Any]" = response[climate.entity_id]  # type: ignore
        temperatures = [b["data"]["temperature"] for b in result["schedule"]["monday"]]
        assert temperatures == [59, 64.4, 64.4, 69.8, 69.8, 59]
        limits = result["limits"]
        # bounds in the HA unit, the grid in the device unit
        assert limits["min_temp"] == climate.min_temp * 1.8 + 32
        assert limits["max_temp"] == climate.max_temp * 1.8 + 32
        assert limits["device_unit"] == "°C"
        # pasted back unchanged
        await ctx.async_call(
            mlc.SERVICE_SET_SCHEDULE, climate.entity_id, schedule=result["schedule"]
        )
        assert len(ctx.set_payloads) == 1
        assert ctx.emulator_schedule() == MTS100_TRACE_DAYS
        # values are snapped (0.5 °C) after their conversion
        response = await ctx.async_call(
            mlc.SERVICE_SET_SCHEDULE,
            climate.entity_id,
            True,
            schedule={"monday": [B("00:00", "24:00", 70)]},
        )
        assert len(response[climate.entity_id]["warnings"]) == 1  # type: ignore
        assert ctx.emulator_schedule()["mon"][0] == [1440, 210]


async def test_schedule_services_mts300(request, hass: "HomeAssistant"):
    """mts300 (provisional): heat/cool block data, [minutes, heat, cold] entries."""
    async with async_schedule_context(request, hass, build_mts300_emulator()) as ctx:
        climate = ctx.climate
        entity_id = climate.entity_id
        response = await ctx.async_call(mlc.SERVICE_GET_SCHEDULE, entity_id)
        result: "dict[str, Any]" = response[entity_id]  # type: ignore

        # the trace schedule
        trace_day = [
            B("00:00", "06:00", heat=21.11, cool=26.11),
            B("06:00", "21:00", heat=21.11, cool=23.88),
            B("21:00", "24:00", heat=21.11, cool=26.11),
        ]
        assert result["schedule"] == {day: trace_day for day in HA_SCHEDULE_WEEKDAY}
        assert result["limits"] == {
            "min_temp": climate.min_temp,
            "max_temp": climate.max_temp,
            "device_unit": "°C",
            "keys": ["heat", "cool"],
            "unit_time": 15,
            "entry_count_max": 12,  # not reported by the device
        }

        # round trip: the (off-grid) trace values are written back unchanged
        # (the device holds them)
        native = {day: MTS300_TRACE_DAY for day in MTS_SCHEDULE_WEEKDAY}
        await ctx.async_call(
            mlc.SERVICE_SET_SCHEDULE, entity_id, schedule=result["schedule"]
        )
        assert len(ctx.set_payloads) == 1
        ((set_payload,),) = ctx.set_payloads[0].values()
        assert {day: set_payload[day] for day in MTS_SCHEDULE_WEEKDAY} == native
        # the device 'pre' block is echoed
        assert set_payload["pre"] == MTS300_TRACE_PRE
        assert ctx.emulator_schedule() == native
        assert native_equal(climate.schedule.ns_value, native)

        # a single-setpoint schedule doesn't compile
        with pytest.raises(ServiceValidationError, match="'heat' value"):
            await ctx.async_call(
                mlc.SERVICE_SET_SCHEDULE, entity_id, schedule=SCHEDULE_FULL
            )
        assert len(ctx.set_payloads) == 1
