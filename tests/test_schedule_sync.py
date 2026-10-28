"""
Test the schedule helper -> device schedule sync controller (ScheduleHelperSelect)
and its status sensor (ScheduleSyncSensor) on the msh300 hub mts100 valves.
"""

import asyncio
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, patch

from homeassistant import const as hac
from homeassistant.exceptions import HomeAssistantError
from homeassistant.util.unit_system import METRIC_SYSTEM, US_CUSTOMARY_SYSTEM
import pytest

from custom_components.meross_lan import const as mlc, schedule_sync
from custom_components.meross_lan.calendar import MtsSchedule
from custom_components.meross_lan.devices.thermostat import (
    MtsSummerMode,
    MtsTempUnit,
)
from custom_components.meross_lan.devices.thermostat.mts960 import Mts960Climate
from custom_components.meross_lan.helpers.schedule import native_equal
from custom_components.meross_lan.merossclient.protocol import const as mc
from custom_components.meross_lan.merossclient.protocol.namespaces import (
    hub as mn_h,
    thermostat as mn_t,
)
from custom_components.meross_lan.schedule_sync import (
    ScheduleHelperSelect as Select,
    ScheduleSyncSensor as Sensor,
)
import emulator as emulator_module

from tests import const as tc, helpers
from tests.schedule_helpers import (
    DRIFTED_DAY,
    HELPER_BLOCKS,
    HELPER_BLOCKS_MTS300,
    HELPER_ENTITY_ID,
    MTS300_TRACE_PRE,
    OTHER_DEVICE_UUID,
    OTHER_SCHEDULE,
    OVERLAP_SCHEDULE,
    B,
    async_schedule_context,
    async_schedule_device,
    build_mts200_emulator,
    build_mts300_emulator,
    patch_read_helper,
    week,
)

if TYPE_CHECKING:
    from typing import Any

    from homeassistant.core import HomeAssistant

    from tests.schedule_helpers import ScheduleContext

SYNCED = (Sensor.STATE_SYNCED, None)


async def _async_link(ctx: "ScheduleContext", select: Select | None = None):
    """Links the helper and lets the (debounced) sync run."""
    await (select or ctx.select).async_select_option(HELPER_ENTITY_ID)
    await ctx.async_tick(Select.SYNC_DELAY)


def _patch_read_error():
    """The helper read fails (helper missing or not available)."""
    return patch.object(
        Select, "_async_read_helper", AsyncMock(side_effect=HomeAssistantError("n/a"))
    )


async def test_schedule_sync(request, hass: "HomeAssistant"):
    async with async_schedule_context(request, hass) as ctx:
        select = ctx.select
        assert HELPER_ENTITY_ID in select.options
        assert ctx.sensor_state().state == Sensor.STATE_OFF
        expected = ctx.compile(await select._async_read_helper(HELPER_ENTITY_ID))
        assert expected != ctx.emulator_schedule()

        # (a) link the helper: debounced write and confirmation
        await select.async_select_option(HELPER_ENTITY_ID)
        assert ctx.sensor_state().state == Sensor.STATE_PENDING
        await ctx.async_tick(Select.SYNC_DELAY)
        assert len(ctx.set_payloads) == 1
        assert ctx.emulator_schedule() == expected
        state = ctx.sensor_state()
        assert state.state == Sensor.STATE_SYNCED
        assert state.attributes[Sensor.ATTR_HELPER] == HELPER_ENTITY_ID
        assert Sensor.ATTR_REASON not in state.attributes
        assert ctx.climate.schedule.ns_value[mc.KEY_ID] == ctx.climate.id

        # (b) helper events without content changes: no writes
        ctx.fire_helper_event()
        await ctx.async_tick(Select.SYNC_DELAY)
        assert len(ctx.set_payloads) == 1
        assert ctx.sensor_state().state == Sensor.STATE_SYNCED
        assert await select.async_sync() == SYNCED
        assert len(ctx.set_payloads) == 1

        # (c) drift: the device schedule is changed elsewhere
        await ctx.async_drift()
        state = ctx.sensor_state()
        assert state.state == Sensor.STATE_DRIFT
        assert state.attributes[Sensor.ATTR_REASON] == Select.REASON_DRIFT
        # drift is flagged, not enforced, also after an error (which device
        # updates don't hide)
        with _patch_read_error():
            ctx.fire_helper_event()
            await ctx.async_tick(Select.SYNC_DELAY)
        await ctx.async_refresh_device_schedule()
        assert ctx.sensor_state().state == Sensor.STATE_ERROR
        ctx.fire_helper_event()
        await ctx.async_tick(Select.SYNC_DELAY)
        assert len(ctx.set_payloads) == 1
        assert ctx.sensor_state().state == Sensor.STATE_DRIFT

        # (d) a forced sync restores the device schedule
        assert await select.async_sync(force=True) == SYNCED
        assert len(ctx.set_payloads) == 2
        assert ctx.emulator_schedule() == expected

        # (e) the helper content changes: new write
        expected_other = ctx.compile(OTHER_SCHEDULE)
        assert expected_other != expected
        with patch_read_helper(OTHER_SCHEDULE):
            ctx.fire_helper_event()
            await ctx.async_tick(Select.SYNC_DELAY)
            assert len(ctx.set_payloads) == 3
            assert ctx.emulator_schedule() == expected_other
            assert ctx.sensor_state().state == Sensor.STATE_SYNCED

        # (f) the helper is removed
        hass.states.async_remove(HELPER_ENTITY_ID)
        await hass.async_block_till_done()
        state = ctx.sensor_state()
        assert state.state == Sensor.STATE_ERROR
        assert "was removed" in state.attributes[Sensor.ATTR_REASON]
        assert len(ctx.set_payloads) == 3


async def test_schedule_sync_warnings(request, hass: "HomeAssistant"):
    """A helper edit compiling to the same schedule (no write) replaces the
    warnings: the sensor always reflects the last compiled helper."""
    misaligned = week([B("00:00", "07:01", 16), B("07:01", "24:00", 20.5)])
    async with async_schedule_context(request, hass) as ctx:
        with patch_read_helper(misaligned):
            await _async_link(ctx)
        state = ctx.sensor_state()
        assert state.state == Sensor.STATE_SYNCED
        assert state.attributes[Sensor.ATTR_WARNINGS]
        with patch_read_helper(OTHER_SCHEDULE):  # 07:01 -> 07:00
            ctx.fire_helper_event()
            await ctx.async_tick(Select.SYNC_DELAY)
        assert len(ctx.set_payloads) == 1
        state = ctx.sensor_state()
        assert state.state == Sensor.STATE_SYNCED
        assert Sensor.ATTR_WARNINGS not in state.attributes


async def test_schedule_sync_borders(request, hass: "HomeAssistant"):
    """A helper edit only moving block borders between equal temperatures is
    written (the device mirrors the helper blocks) while the drift check ignores
    such differences."""
    # HELPER_BLOCKS with its 21 °C block split in two
    split_schedule = week(
        HELPER_BLOCKS[:2] + [B("08:00", "17:00", 21), B("17:00", "24:00", 21)]
    )
    async with async_schedule_context(request, hass) as ctx:
        select = ctx.select
        await _async_link(ctx)
        assert len(ctx.set_payloads) == 1
        unsplit = ctx.emulator_schedule()
        assert unsplit["mon"][:3] == [[390, 150], [90, 180], [960, 210]]

        with patch_read_helper(split_schedule):
            ctx.fire_helper_event()
            await ctx.async_tick(Select.SYNC_DELAY)
            assert len(ctx.set_payloads) == 2
            split = ctx.compile(split_schedule)
            assert split["mon"][:4] == [[390, 150], [90, 180], [540, 210], [420, 210]]
            assert ctx.emulator_schedule() == split
            assert ctx.sensor_state().state == Sensor.STATE_SYNCED

            # the device reports the same temperatures with other borders: no drift
            await ctx.async_set_device_schedule(unsplit)
            assert ctx.sensor_state().state == Sensor.STATE_SYNCED
            ctx.fire_helper_event()
            await ctx.async_tick(Select.SYNC_DELAY)
            assert len(ctx.set_payloads) == 2
            assert ctx.sensor_state().state == Sensor.STATE_SYNCED
            # an explicit selection writes the helper blocks
            await _async_link(ctx, select)
            assert len(ctx.set_payloads) == 3
            assert ctx.emulator_schedule() == split
            assert ctx.sensor_state().state == Sensor.STATE_SYNCED


async def test_schedule_sync_compile_error(request, hass: "HomeAssistant"):
    async with async_schedule_context(request, hass) as ctx:
        select = ctx.select
        emulator_schedule_initial = ctx.emulator_schedule()
        with patch_read_helper(OVERLAP_SCHEDULE):
            await _async_link(ctx)
            state = ctx.sensor_state()
            assert state.state == Sensor.STATE_ERROR
            assert "overlap" in state.attributes[Sensor.ATTR_REASON]
            assert not ctx.set_payloads
            assert ctx.emulator_schedule() == emulator_schedule_initial
            # a device update doesn't hide the error
            await ctx.async_refresh_device_schedule()
            assert ctx.sensor_state().state == Sensor.STATE_ERROR
            state, reason = await select.async_sync(force=True)
            assert state == Sensor.STATE_ERROR
            assert "overlap" in reason  # type: ignore
            assert not ctx.set_payloads

        # the helper is fixed
        ctx.fire_helper_event()
        await ctx.async_tick(Select.SYNC_DELAY)
        assert len(ctx.set_payloads) == 1
        assert ctx.sensor_state().state == Sensor.STATE_SYNCED


async def test_schedule_sync_off(request, hass: "HomeAssistant"):
    async with async_schedule_context(request, hass) as ctx:
        select = ctx.select
        await _async_link(ctx)
        assert ctx.sensor_state().state == Sensor.STATE_SYNCED
        assert len(ctx.set_payloads) == 1

        await select.async_select_option(hac.STATE_OFF)
        state = ctx.sensor_state()
        assert state.state == Sensor.STATE_OFF
        assert Sensor.ATTR_HELPER not in state.attributes
        with patch_read_helper(OTHER_SCHEDULE):
            ctx.fire_helper_event()
            await ctx.async_tick(Select.SYNC_DELAY)
            assert await select.async_sync(force=True) == (Sensor.STATE_OFF, None)
        assert len(ctx.set_payloads) == 1
        assert ctx.sensor_state().state == Sensor.STATE_OFF

        # re-linking a helper already matching the device: no write
        await _async_link(ctx)
        assert len(ctx.set_payloads) == 1
        assert ctx.sensor_state().state == Sensor.STATE_SYNCED


async def test_schedule_sync_offline(request, hass: "HomeAssistant"):
    """The valve is offline while the hub is online: deferred."""
    async with async_schedule_context(request, hass) as ctx:
        assert ctx.context.device.is_connected
        with (
            patch.object(ctx.climate, "is_connected", False),
            patch.object(
                Select,
                "_async_read_helper",
                autospec=True,
                side_effect=Select._async_read_helper,
            ) as read_spy,
        ):
            await _async_link(ctx)
            state = ctx.sensor_state()
            assert state.state == Sensor.STATE_PENDING
            assert state.attributes[Sensor.ATTR_REASON] == Select.REASON_OFFLINE
            # climate updates (flushes) don't run it while offline
            for _ in range(3):
                ctx.climate.flush_state()
                await ctx.async_tick(1)
            assert read_spy.await_count == 1
            assert not ctx.set_payloads
        # the device is back: the retry syncs
        await ctx.async_tick(Select.RETRY_DELAY)
        assert len(ctx.set_payloads) == 1
        assert ctx.sensor_state().state == Sensor.STATE_SYNCED


async def test_schedule_sync_write_error(request, hass: "HomeAssistant"):
    """A write rejected by the device is an error while a failed request is
    retried (on the retry timer only)."""
    async with async_schedule_context(request, hass) as ctx:
        with patch.object(
            MtsSchedule,
            "async_request_payload",
            AsyncMock(
                return_value=SimpleNamespace(
                    method=mc.METHOD_ERROR, payload={mc.KEY_ERROR: {mc.KEY_CODE: -1}}
                )
            ),
        ):
            await _async_link(ctx)
            state = ctx.sensor_state()
            assert state.state == Sensor.STATE_ERROR
            assert "rejected" in state.attributes[Sensor.ATTR_REASON]
        with patch.object(
            MtsSchedule,
            "async_request_payload",
            AsyncMock(side_effect=Exception("timeout")),
        ) as request_mock:
            assert await ctx.select.async_sync() == (
                Sensor.STATE_PENDING,
                "device write failed: timeout",
            )
            for _ in range(3):
                ctx.climate.flush_state()
                await ctx.async_tick(1)
            assert request_mock.await_count == 1
        assert not ctx.set_payloads
        await ctx.async_tick(Select.RETRY_DELAY)
        assert len(ctx.set_payloads) == 1
        assert ctx.sensor_state().state == Sensor.STATE_SYNCED


async def test_schedule_sync_options(request, hass: "HomeAssistant"):
    """The select options follow the schedule helpers added/removed at runtime."""
    async with async_schedule_context(request, hass) as ctx:
        select = ctx.select
        await _async_link(ctx)
        assert ctx.sensor_state().state == Sensor.STATE_SYNCED
        assert select.options == [hac.STATE_OFF, HELPER_ENTITY_ID]

        other_entity_id = "schedule.other"
        hass.states.async_set(other_entity_id, hac.STATE_OFF, {})
        await hass.async_block_till_done()
        assert select.options == [hac.STATE_OFF, HELPER_ENTITY_ID, other_entity_id]
        select_state = hass.states.get(select.entity_id)  # type: ignore
        assert select_state
        assert select_state.attributes["options"] == select.options
        assert select_state.state == HELPER_ENTITY_ID

        hass.states.async_remove(other_entity_id)
        await hass.async_block_till_done()
        assert select.options == [hac.STATE_OFF, HELPER_ENTITY_ID]
        await ctx.async_tick(Select.SYNC_DELAY)
        assert select.linked_helper == HELPER_ENTITY_ID
        assert ctx.sensor_state().state == Sensor.STATE_SYNCED
        assert len(ctx.set_payloads) == 1

        # the linked helper is removed: it stays selected (and listed) while
        # the status reports the error
        helper_state = hass.states.get(HELPER_ENTITY_ID)
        assert helper_state
        hass.states.async_remove(HELPER_ENTITY_ID)
        await hass.async_block_till_done()
        assert select.linked_helper == HELPER_ENTITY_ID
        assert select.options == [hac.STATE_OFF, HELPER_ENTITY_ID]
        assert hass.states.get(select.entity_id).state == HELPER_ENTITY_ID  # type: ignore
        assert ctx.sensor_state().state == Sensor.STATE_ERROR

        # ...and it comes back: the existing subscriptions resync
        hass.states.async_set(
            HELPER_ENTITY_ID, helper_state.state, helper_state.attributes
        )
        await hass.async_block_till_done()
        assert select.options == [hac.STATE_OFF, HELPER_ENTITY_ID]
        await ctx.async_tick(Select.SYNC_DELAY)
        assert ctx.sensor_state().state == Sensor.STATE_SYNCED
        assert len(ctx.set_payloads) == 1


@pytest.mark.parametrize("unlink", [True, False], ids=["unlinked", "helper_removed"])
async def test_schedule_sync_superseded_while_writing(
    request, hass: "HomeAssistant", unlink: bool
):
    """A run superseded while awaiting the device (link changed or helper removed)
    doesn't touch the status."""
    async with async_schedule_context(request, hass) as ctx:
        async with ctx.async_hold_writes() as async_write_started:
            await ctx.select.async_select_option(HELPER_ENTITY_ID)
            # (not async_tick: it would wait for the held write)
            ctx.context.time_mock.tick(Select.SYNC_DELAY)
            await async_write_started()
            state = ctx.sensor_state()
            assert state.attributes[Sensor.ATTR_REASON] == Select.REASON_WRITING
            if unlink:
                await ctx.select.async_select_option(hac.STATE_OFF)
            else:
                hass.states.async_remove(HELPER_ENTITY_ID)

        assert len(ctx.set_payloads) == 1  # the write landed anyway
        expected = Sensor.STATE_OFF if unlink else Sensor.STATE_ERROR
        assert ctx.sensor_state().state == expected
        # device updates don't change it
        await ctx.async_refresh_device_schedule()
        await ctx.async_tick(Select.RETRY_DELAY)
        assert ctx.sensor_state().state == expected
        assert len(ctx.set_payloads) == 1


async def test_schedule_sync_unconfirmed(request, hass: "HomeAssistant"):
    """The write is acknowledged but the confirmation query fails: retried."""
    async with async_schedule_context(request, hass) as ctx:
        expected = ctx.compile(await ctx.select._async_read_helper(HELPER_ENTITY_ID))
        handler = ctx.climate.schedule.handler_ns
        with patch.object(
            handler, "async_get", AsyncMock(side_effect=Exception("timeout"))
        ):
            await _async_link(ctx)
            assert len(ctx.set_payloads) == 1
            state = ctx.sensor_state()
            assert state.state == Sensor.STATE_PENDING
            assert state.attributes[Sensor.ATTR_REASON] == Select.REASON_CONFIRM
            # climate updates (flushes) don't run it before the retry timer
            for _ in range(3):
                ctx.climate.flush_state()
                await ctx.async_tick(1)
            assert len(ctx.set_payloads) == 1
            assert ctx.sensor_state().state == Sensor.STATE_PENDING
        # the device didn't report its schedule meanwhile: the retry writes again
        await ctx.async_tick(Select.RETRY_DELAY)
        assert len(ctx.set_payloads) == 2
        assert ctx.sensor_state().state == Sensor.STATE_SYNCED
        assert ctx.emulator_schedule() == expected


async def test_schedule_sync_stale_readback(request, hass: "HomeAssistant"):
    """The device reports its former schedule right after the write (as a hub
    might for its valves): queried again a bit later."""
    async with async_schedule_context(request, hass) as ctx:
        handler = ctx.climate.schedule.handler_ns
        async_get = handler.async_get
        queries = []
        stale_query = asyncio.Event()

        async def _async_get(*indexes):
            queries.append(indexes)
            if len(queries) == 1:  # finds the former schedule
                stale_query.set()
            else:
                await async_get(*indexes)

        with patch.object(handler, "async_get", _async_get):
            await ctx.select.async_select_option(HELPER_ENTITY_ID)
            # (not async_tick: it would wait for the delayed query)
            ctx.context.time_mock.tick(Select.SYNC_DELAY)
            await asyncio.wait_for(stale_query.wait(), 5)
            assert len(ctx.set_payloads) == 1
            assert ctx.sensor_state().state == Sensor.STATE_PENDING
            await ctx.async_tick(schedule_sync.READBACK_DELAY)
            assert len(queries) == 2
        assert len(ctx.set_payloads) == 1
        assert ctx.sensor_state().state == Sensor.STATE_SYNCED


async def test_schedule_partial_update(request, hass: "HomeAssistant"):
    """A partial device schedule (the PUSH of a day edited in the app) received
    while the schedule is not known (e.g. after going offline) is not taken as
    the whole schedule: the full one is queried instead."""
    async with async_schedule_context(request, hass) as ctx:
        schedule = ctx.climate.schedule
        ctx._emulator_ns_state(ctx.climate)["mon"] = DRIFTED_DAY
        expected = ctx.emulator_schedule()
        schedule.set_unavailable()
        assert not schedule.ns_value
        schedule({**schedule.index, "mon": DRIFTED_DAY})
        await hass.async_block_till_done()
        assert native_equal(schedule.ns_value, expected)


async def test_schedule_sync_no_scheduleb_mode(request, hass: "HomeAssistant"):
    """A valve not reporting its slots per day (no scheduleBMode in
    Hub.Mts100.All) takes the MTS100/MTS150 default (6): the sync doesn't wait
    for it."""
    emulator = helpers.build_emulator(mc.TYPE_MSH300)
    ns = mn_h.Appliance_Hub_Mts100_All
    for p_subdevice in (
        *emulator.namespaces[ns][ns.key],
        *emulator.descriptor.digest[mc.KEY_HUB][mc.KEY_SUBDEVICE],
    ):
        p_subdevice.pop(mc.KEY_SCHEDULEBMODE, None)
    async with async_schedule_context(request, hass, emulator) as ctx:
        climate = ctx.climate
        assert mc.KEY_SCHEDULEBMODE not in climate.extra_state_attributes
        assert climate.schedule.limits["entry_count_max"] == 6
        await _async_link(ctx)
        assert len(ctx.set_payloads) == 1
        assert ctx.sensor_state().state == Sensor.STATE_SYNCED


async def test_schedule_sync_error_logged(request, hass: "HomeAssistant", caplog):
    """A sync failure is logged naming the thermostat (the log dedupe is by message:
    the same failure on another thermostat would be dropped)."""
    async with async_schedule_context(request, hass) as ctx:
        # (a message not logged by the other tests)
        with patch_read_helper({"monday": [B("00:00", "24:00", "warm")]}):
            await _async_link(ctx)
        assert f"Schedule sync failed for {ctx.climate.entity_id}: " in caplog.text


async def test_schedule_sync_reconnect(request, hass: "HomeAssistant"):
    """The helper is edited while the device is offline: the sync runs as soon as
    the device reports its schedule (not at the retry timeout)."""
    async with async_schedule_context(request, hass) as ctx:
        await _async_link(ctx)
        assert ctx.sensor_state().state == Sensor.STATE_SYNCED
        with patch_read_helper(OTHER_SCHEDULE):
            with patch.object(ctx.climate, "is_connected", False):
                ctx.fire_helper_event()
                await ctx.async_tick(Select.SYNC_DELAY)
                state = ctx.sensor_state()
                assert state.state == Sensor.STATE_PENDING
                assert state.attributes[Sensor.ATTR_REASON] == Select.REASON_OFFLINE
            assert len(ctx.set_payloads) == 1
            # back online: the device reports its (unchanged) schedule
            await ctx.async_refresh_device_schedule()
            await ctx.async_tick(1)
            assert len(ctx.set_payloads) == 2
            assert ctx.emulator_schedule() == ctx.compile(OTHER_SCHEDULE)
            assert ctx.sensor_state().state == Sensor.STATE_SYNCED


async def test_schedule_sync_restore(request, hass: "HomeAssistant"):
    """Restoring the link (HA restart/entry reload) writes the helper only if it
    doesn't compile to the restored reference anymore (edited meanwhile)."""
    async with async_schedule_context(request, hass) as ctx:
        await _async_link(ctx)
        assert ctx.sensor_state().state == Sensor.STATE_SYNCED
        assert len(ctx.set_payloads) == 1
        expected = ctx.emulator_schedule()

        # (a) device matching the helper
        await ctx.async_restart()
        assert ctx.select.linked_helper == HELPER_ENTITY_ID
        assert ctx.sensor_state().state == Sensor.STATE_SYNCED
        assert len(ctx.set_payloads) == 1

        # (b) device schedule changed elsewhere
        await ctx.async_drift()
        await ctx.async_restart()
        assert ctx.sensor_state().state == Sensor.STATE_DRIFT
        assert len(ctx.set_payloads) == 1
        # helper state updates (block boundaries) don't write either
        ctx.fire_helper_event()
        await ctx.async_tick(Select.SYNC_DELAY)
        assert ctx.sensor_state().state == Sensor.STATE_DRIFT
        assert len(ctx.set_payloads) == 1
        # an explicit selection writes
        await _async_link(ctx)
        assert len(ctx.set_payloads) == 2
        assert ctx.emulator_schedule() == expected
        assert ctx.sensor_state().state == Sensor.STATE_SYNCED

        # (c) helper changed while HA was down: written
        with patch_read_helper(OTHER_SCHEDULE):
            await ctx.async_restart()
            assert len(ctx.set_payloads) == 3
            assert ctx.emulator_schedule() == ctx.compile(OTHER_SCHEDULE)
            assert ctx.sensor_state().state == Sensor.STATE_SYNCED

        # (d) no stored reference (first start after an upgrade): adopted
        await ctx.async_restart(stored=False)
        assert ctx.sensor_state().state == Sensor.STATE_DRIFT
        assert len(ctx.set_payloads) == 3


async def test_schedule_sync_ha_unit_changed(request, hass: "HomeAssistant"):
    """HA switched to °F: the helper values (no unit) would be read as °F, so the
    sync holds (error, no write, also across a restart) until the helper is
    edited or pushed."""
    async with async_schedule_context(request, hass) as ctx:
        await _async_link(ctx)
        assert len(ctx.set_payloads) == 1
        hass.config.units = US_CUSTOMARY_SYSTEM
        for _ in range(2):
            ctx.fire_helper_event()
            await ctx.async_tick(Select.SYNC_DELAY)
            state = ctx.sensor_state()
            assert state.state == Sensor.STATE_ERROR
            assert state.attributes[Sensor.ATTR_REASON] == Select.REASON_HA_UNIT
            await ctx.async_restart()
        assert len(ctx.set_payloads) == 1
        # edited in °F: written
        schedule_f = week([B("00:00", "07:00", 61), B("07:00", "24:00", 68)])
        with patch_read_helper(schedule_f):
            ctx.fire_helper_event()
            await ctx.async_tick(Select.SYNC_DELAY)
        assert len(ctx.set_payloads) == 2
        assert ctx.emulator_schedule()["mon"][:2] == [[420, 160], [1020, 200]]
        assert ctx.sensor_state().state == Sensor.STATE_SYNCED

        # back to °C: held again until a forced push (written as read)
        hass.config.units = METRIC_SYSTEM
        ctx.fire_helper_event()
        await ctx.async_tick(Select.SYNC_DELAY)
        assert ctx.sensor_state().state == Sensor.STATE_ERROR
        assert len(ctx.set_payloads) == 2
        await ctx.select.async_push()
        assert len(ctx.set_payloads) == 3
        assert ctx.sensor_state().state == Sensor.STATE_SYNCED


async def test_schedule_sync_restore_offline(request, hass: "HomeAssistant"):
    """Restoring the link with the valve offline: the unchanged helper is adopted
    (not written) when the valve is back."""
    async with async_schedule_context(request, hass) as ctx:
        await _async_link(ctx)
        await ctx.async_restart(sync=False)
        with patch.object(ctx.climate, "is_connected", False):
            await ctx.async_tick(Select.SYNC_DELAY)
            state = ctx.sensor_state()
            assert state.attributes[Sensor.ATTR_REASON] == Select.REASON_OFFLINE
        # back online: the device reports its schedule
        await ctx.async_refresh_device_schedule()
        await ctx.async_tick(1)
        assert ctx.sensor_state().state == Sensor.STATE_SYNCED
        assert len(ctx.set_payloads) == 1


async def test_schedule_sync_restore_errors(request, hass: "HomeAssistant"):
    """Errors while restoring the link: a missing helper (e.g. its integration
    failed to load) stays linked and is adopted when it appears while a compile
    error ends the adoption (the fixed helper is written)."""
    async with async_schedule_context(request, hass) as ctx:
        await _async_link(ctx)
        assert len(ctx.set_payloads) == 1

        # (a) missing helper, device schedule changed meanwhile
        await ctx.async_drift()
        helper_state = hass.states.get(HELPER_ENTITY_ID)
        assert helper_state
        hass.states.async_remove(HELPER_ENTITY_ID)
        with _patch_read_error():
            await ctx.async_restart()
        select = ctx.select
        assert select.linked_helper == HELPER_ENTITY_ID
        assert select.options == [hac.STATE_OFF, HELPER_ENTITY_ID]
        assert ctx.sensor_state().state == Sensor.STATE_ERROR
        # it appears: adopted (drift, no write)
        hass.states.async_set(
            HELPER_ENTITY_ID, helper_state.state, helper_state.attributes
        )
        await ctx.async_tick(Select.SYNC_DELAY)
        assert ctx.sensor_state().state == Sensor.STATE_DRIFT
        assert len(ctx.set_payloads) == 1

        # (b) helper not compilable at restore: error, no write. Once fixed
        # (a user edit) it is written
        with patch_read_helper(OVERLAP_SCHEDULE):
            await ctx.async_restart()
            state = ctx.sensor_state()
            assert state.state == Sensor.STATE_ERROR
            assert "overlap" in state.attributes[Sensor.ATTR_REASON]
        assert len(ctx.set_payloads) == 1
        with patch_read_helper(OTHER_SCHEDULE):
            ctx.fire_helper_event()
            await ctx.async_tick(Select.SYNC_DELAY)
            assert len(ctx.set_payloads) == 2
            assert ctx.emulator_schedule() == ctx.compile(OTHER_SCHEDULE)
            assert ctx.sensor_state().state == Sensor.STATE_SYNCED


def _blocks_schedule(count: int) -> "dict[str, Any]":
    """'count' slots on monday: 'count' - 1 one hour blocks and a gap."""
    return {
        "monday": [
            B(f"{h:02d}:00", f"{h + 1:02d}:00", 16 + h) for h in range(count - 1)
        ]
    }


async def test_schedule_sync_group(request, hass: "HomeAssistant"):
    """A helper shared by thermostats with different limits (mts100 valve: 6 slots
    per day, mts200: 8) is all or nothing: written only if it fits all of them."""
    async with (
        async_schedule_context(request, hass) as ctx,
        async_schedule_device(
            request, hass, build_mts200_emulator(OTHER_DEVICE_UUID)
        ) as ctx_mts200,
    ):
        valve, mts200 = ctx.climate, ctx_mts200.climate
        assert mts200.schedule.limits["entry_count_max"] == 8
        members = ((ctx, valve), (ctx_mts200, mts200))

        def _check(state: str, writes: int):
            for member_ctx, climate in members:
                assert ctx.sensor_state(climate).state == state
                assert len(member_ctx.set_payloads) == writes

        async def _async_edit():
            ctx.fire_helper_event()
            await ctx.async_tick(Select.SYNC_DELAY)

        for _, climate in members:
            await climate.select_schedule_helper.async_select_option(HELPER_ENTITY_ID)
        await ctx.async_tick(Select.SYNC_DELAY)
        _check(Sensor.STATE_SYNCED, 1)

        # (a) 7 slots on monday: written to none, both report the error
        with patch_read_helper(_blocks_schedule(7)):
            await _async_edit()
        _check(Sensor.STATE_ERROR, 1)
        reason = ctx.sensor_state(valve).attributes[Sensor.ATTR_REASON]
        assert "exceed the device limit of 6" in reason
        reason = ctx.sensor_state(mts200).attributes[Sensor.ATTR_REASON]
        assert reason.startswith(f"does not fit {valve.entity_id}: monday")

        # (b) 6 slots: written to both
        six_slots = _blocks_schedule(6)
        with patch_read_helper(six_slots):
            await _async_edit()
        _check(Sensor.STATE_SYNCED, 2)
        for member_ctx, climate in members:
            assert member_ctx.emulator_schedule(climate) == ctx.compile(
                six_slots, climate
            )

        # (c) the valve schedule (limits) is not known: skipped until it reports
        with patch_read_helper(_blocks_schedule(7)):
            with patch.object(type(valve), "schedule_limits_known", return_value=False):
                await _async_edit()
                state = ctx.sensor_state(valve)
                assert state.attributes[Sensor.ATTR_REASON] == Select.REASON_UNKNOWN
            assert ctx.sensor_state(mts200).state == Sensor.STATE_SYNCED
            assert len(ctx_mts200.set_payloads) == 3
            # the valve reports: the helper doesn't fit it
            await ctx.async_refresh_device_schedule(valve)
            await ctx.async_tick(1)
        assert ctx.sensor_state(valve).state == Sensor.STATE_ERROR
        assert len(ctx.set_payloads) == 2
        assert ctx.sensor_state(mts200).state == Sensor.STATE_SYNCED

        # unlinking one doesn't affect the other
        await valve.select_schedule_helper.async_select_option(hac.STATE_OFF)
        assert ctx.sensor_state(valve).state == Sensor.STATE_OFF
        assert ctx.sensor_state(mts200).state == Sensor.STATE_SYNCED


async def test_schedule_sync_group_mts300(request, hass: "HomeAssistant"):
    """An mts300 (heat/cool) and another model don't share a temperature helper."""
    async with (
        async_schedule_context(request, hass) as ctx,
        async_schedule_device(
            request, hass, build_mts300_emulator(OTHER_DEVICE_UUID)
        ) as ctx_mts300,
    ):
        valve, mts300 = ctx.climate, ctx_mts300.climate
        for climate in (valve, mts300):
            await climate.select_schedule_helper.async_select_option(HELPER_ENTITY_ID)
        await ctx.async_tick(Select.SYNC_DELAY)
        state = ctx.sensor_state(mts300)
        assert state.state == Sensor.STATE_ERROR
        assert "'heat'" in state.attributes[Sensor.ATTR_REASON]
        state = ctx.sensor_state(valve)
        assert state.state == Sensor.STATE_ERROR
        assert state.attributes[Sensor.ATTR_REASON].startswith(
            f"does not fit {mts300.entity_id}: "
        )
        assert not ctx.set_payloads
        assert not ctx_mts300.set_payloads


async def test_schedule_sync_mts300(request, hass: "HomeAssistant"):
    """mts300 (provisional): heat/cool blocks compiled to [minutes, heat, cold]
    entries ('cool' carried forward where missing, off: heat min, cool max) and
    'pre' echoed."""
    async with async_schedule_context(
        request, hass, build_mts300_emulator(), HELPER_BLOCKS_MTS300
    ) as ctx:
        await _async_link(ctx)
        assert len(ctx.set_payloads) == 1
        ((set_payload,),) = ctx.set_payloads[0].values()
        assert set_payload["pre"] == MTS300_TRACE_PRE
        assert ctx._emulator_ns_state(ctx.climate)["pre"] == MTS300_TRACE_PRE
        assert ctx.emulator_schedule()["mon"] == [
            [390, 1700, 2700],
            [90, 2000, 2700],
            [840, 1900, 2500],
            [120, 500, 3500],
        ]
        state = ctx.sensor_state()
        assert state.state == Sensor.STATE_SYNCED
        assert state.attributes[Sensor.ATTR_WARNINGS] == [
            "cool carried forward into 7 blocks"
        ]


async def test_schedule_sync_mts300_fahrenheit(request, hass: "HomeAssistant"):
    """HA in °F: a helper seeded from the device schedule (as get_schedule reads it)
    compiles back to it exactly: linking it writes nothing."""
    hass.config.units = US_CUSTOMARY_SYSTEM
    # MTS300_TRACE_DAY in °F
    helper_blocks = [
        B("00:00", "06:00", heat=70, cool=79),
        B("06:00", "21:00", heat=70, cool=74.98),
        B("21:00", "24:00", heat=70, cool=79),
    ]
    async with async_schedule_context(
        request, hass, build_mts300_emulator(), helper_blocks
    ) as ctx:
        await _async_link(ctx)
        state = ctx.sensor_state()
        assert state.state == Sensor.STATE_SYNCED
        assert Sensor.ATTR_WARNINGS not in state.attributes
        assert not ctx.set_payloads


async def test_schedule_sync_races(request, hass: "HomeAssistant"):
    """A push superseded by a link change fails. The select removed (e.g.
    disabled): the status is off, get_schedule reports no linked helper and a push
    fails."""
    async with async_schedule_context(request, hass) as ctx:
        select = ctx.select
        await _async_link(ctx)
        async with ctx.async_hold_writes() as async_write_started:
            push = hass.async_create_task(select.async_push())
            await async_write_started()
            await select.async_select_option(HELPER_ENTITY_ID)
        with pytest.raises(HomeAssistantError, match="superseded"):
            await push

        await select.async_remove()
        state = ctx.sensor_state()
        assert state.state == Sensor.STATE_OFF
        assert Sensor.ATTR_HELPER not in state.attributes
        entity_id = ctx.climate.entity_id
        response = await ctx.async_call(mlc.SERVICE_GET_SCHEDULE, entity_id)
        assert response[entity_id]["linked_helper"] is None  # type: ignore
        with pytest.raises(HomeAssistantError, match="select is disabled"):
            await select.async_push()


async def test_schedule_sync_device_fahrenheit(request, hass: "HomeAssistant"):
    """A helper shared by a °C valve and a °F mts200 (display unit): each is
    written on its own grid (0.5 °C, whole °F truncated to the device scale) and
    both are synced. A device unit switch reruns the sync: one write."""
    async with (
        async_schedule_context(request, hass) as ctx,
        async_schedule_device(
            request,
            hass,
            build_mts200_emulator(OTHER_DEVICE_UUID, mc.TEMPUNIT_FAHRENHEIT),
        ) as ctx_mts200,
    ):
        valve, mts200 = ctx.climate, ctx_mts200.climate
        for climate in (valve, mts200):
            await climate.select_schedule_helper.async_select_option(HELPER_ENTITY_ID)
        await ctx.async_tick(Select.SYNC_DELAY)
        # HELPER_BLOCKS: 15, 18, 21 °C
        assert ctx.emulator_schedule(valve)["mon"][:3] == [
            [390, 150],
            [90, 180],
            [960, 210],
        ]
        # 59 °F, 64 °F (17.78 °C), 70 °F (21.11 °C)
        assert ctx_mts200.emulator_schedule(mts200)["mon"][:3] == [
            [390, 150],
            [90, 177],
            [960, 211],
        ]
        for member_ctx, climate in ((ctx, valve), (ctx_mts200, mts200)):
            assert ctx.sensor_state(climate).state == Sensor.STATE_SYNCED
            assert len(member_ctx.set_payloads) == 1
        response = await ctx.async_call(mlc.SERVICE_GET_SCHEDULE, mts200.entity_id)
        limits = response[mts200.entity_id]["limits"]  # type: ignore
        assert limits["device_unit"] == "°F"

        # the mts200 is switched to °C: written on the 0.5 °C grid
        unit_select = next(
            entity
            for entity in mts200.parent.entities.values()
            if isinstance(entity, MtsTempUnit)
        )
        await unit_select.async_select_option(hac.UnitOfTemperature.CELSIUS)
        await ctx.async_tick(1)
        assert len(ctx_mts200.set_payloads) == 2
        assert ctx_mts200.emulator_schedule(mts200)["mon"][:3] == [
            [390, 150],
            [90, 180],
            [960, 210],
        ]
        assert ctx.sensor_state(mts200).state == Sensor.STATE_SYNCED
        # ...and back to °F: every value moves to the °F grid (also those the
        # device holds)
        await unit_select.async_select_option(hac.UnitOfTemperature.FAHRENHEIT)
        await ctx.async_tick(1)
        assert len(ctx_mts200.set_payloads) == 3
        assert ctx_mts200.emulator_schedule(mts200)["mon"][:3] == [
            [390, 150],
            [90, 177],
            [960, 211],
        ]
        await ctx.async_tick(Select.RETRY_DELAY)
        assert len(ctx_mts200.set_payloads) == 3
        assert len(ctx.set_payloads) == 1
        # an unknown device unit defers the sync (and get_schedule fails)
        with patch.object(unit_select, "ns_value", None):
            assert not mts200.schedule_limits_known()
            with pytest.raises(HomeAssistantError, match="not available yet"):
                await ctx.async_call(mlc.SERVICE_GET_SCHEDULE, mts200.entity_id)


# a 07:00-22:00 block: the rest of the day is off
GAP_BLOCKS = [B("07:00", "22:00", 21)]


async def _async_check_gaps(ctx: "ScheduleContext", writes: int, off: float):
    """The GAP_BLOCKS helper is synced with its gaps at 'off' (°C), which
    get_schedule reads back as gaps."""
    assert len(ctx.set_payloads) == writes
    scale = ctx.climate.temperature_scale
    off = round(off * scale)
    assert ctx.emulator_schedule()["mon"][:3] == [
        [420, off],
        [900, 21 * scale],
        [120, off],
    ]
    assert ctx.sensor_state().state == Sensor.STATE_SYNCED
    entity_id = ctx.climate.entity_id
    response = await ctx.async_call(mlc.SERVICE_GET_SCHEDULE, entity_id)
    assert response[entity_id]["schedule"]["monday"] == GAP_BLOCKS  # type: ignore


async def test_schedule_sync_mts960(request, hass: "HomeAssistant"):
    """mts960: the time not covered by the helper is off by the working mode
    (heat: min_temp, cool: max_temp) and the device bounds (CtlRange): the sync
    waits for them. A working mode or bounds change reruns the sync: one write
    each."""
    emulator = emulator_module.build_emulator(
        tc.EMULATOR_TRACES_PATH + "U0123456789012345678901234567891F-Kpippo-mts960.csv",
        key=tc.MOCK_KEY,
        uuid=tc.MOCK_DEVICE_UUID,
    )
    async with async_schedule_context(request, hass, emulator, GAP_BLOCKS) as ctx:
        climate = ctx.climate
        await climate.async_set_preset_mode(Mts960Climate.Preset.SCHEDULE_HEATING)
        # the device bounds not received yet (the defaults): deferred
        climate._mts_ctlrange = None
        climate.min_temp, climate.max_temp = 5, 35
        await _async_link(ctx)
        state = ctx.sensor_state()
        assert state.attributes[Sensor.ATTR_REASON] == Select.REASON_UNKNOWN
        assert not ctx.set_payloads
        # received: written with them
        ns = mn_t.Appliance_Control_Thermostat_CtlRange
        await climate.parent.ns_handlers[ns].async_get(climate.index)
        await ctx.async_tick(1)
        assert (climate.min_temp, climate.max_temp) == (0, 50)
        await _async_check_gaps(ctx, 1, 0)
        await climate.async_set_preset_mode(Mts960Climate.Preset.SCHEDULE_COOLING)
        await ctx.async_tick(1)
        await _async_check_gaps(ctx, 2, climate.max_temp)
        # the device bounds change
        ctx.emulator.namespaces[ns][ns.key][0][mc.KEY_CTLMAX] = 3000
        await climate.parent.ns_handlers[ns].async_get(climate.index)
        await ctx.async_tick(1)
        assert climate.max_temp == 30
        await _async_check_gaps(ctx, 3, 30)
        await ctx.async_tick(Select.RETRY_DELAY)
        assert len(ctx.set_payloads) == 3


async def test_schedule_sync_mts200_summer_mode(request, hass: "HomeAssistant"):
    """mts200: the time not covered by the helper is off by the summer mode (off:
    min_temp, on/cooling: max_temp). Switching it reruns the sync: one write."""
    emulator = build_mts200_emulator(summer_mode=mc.MTS200_SUMMERMODE_HEAT)
    async with async_schedule_context(request, hass, emulator, GAP_BLOCKS) as ctx:
        climate = ctx.climate
        await _async_link(ctx)
        await _async_check_gaps(ctx, 1, climate.min_temp)
        summer_mode = next(
            entity
            for entity in climate.parent.entities.values()
            if isinstance(entity, MtsSummerMode)
        )
        await summer_mode.async_turn_on()
        await ctx.async_tick(1)
        await _async_check_gaps(ctx, 2, climate.max_temp)
        await ctx.async_tick(Select.RETRY_DELAY)
        assert len(ctx.set_payloads) == 2
        # an unknown summer mode (e.g. reconnecting) defers the sync
        with patch.object(summer_mode, "ns_value", None):
            assert not climate.schedule_limits_known()
