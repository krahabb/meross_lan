"""Shared setup for the schedule helper sync and schedule services tests."""

import asyncio
import contextlib
import json
from typing import TYPE_CHECKING
from unittest.mock import patch

from homeassistant.core import State
from homeassistant.helpers.entity_component import DATA_INSTANCES
from homeassistant.setup import async_setup_component
from pytest_homeassistant.common import mock_restore_cache_with_extra_data

from custom_components.meross_lan import const as mlc
from custom_components.meross_lan.calendar import MtsSchedule
from custom_components.meross_lan.climate import MtsClimate
from custom_components.meross_lan.helpers.schedule import (
    HA_SCHEDULE_WEEKDAY,
    MTS_SCHEDULE_WEEKDAY,
    compile_schedule,
)
from custom_components.meross_lan.merossclient.protocol import (
    const as mc,
    namespaces as mn,
)
from custom_components.meross_lan.merossclient.protocol.namespaces import (
    thermostat as mn_t,
)
from custom_components.meross_lan.schedule_sync import (
    ScheduleHelperSelect,
    schedule_compile_kwargs,
)
import emulator as emulator_module

from tests import const as tc, helpers

if TYPE_CHECKING:
    from typing import Any

    from homeassistant.core import Context, HomeAssistant
    from pytest import FixtureRequest

    from custom_components.meross_lan.button import Button
    from custom_components.meross_lan.merossclient.protocol.namespaces import (
        Namespace,
    )
    from emulator import Emulator

HELPER_ENTITY_ID = "schedule.heating"


def B(begin, end, temperature=None, **data):
    """A schedule helper block."""
    if temperature is not None:
        data["temperature"] = temperature
    return {"from": begin, "to": end, "data": data}


def week(blocks, **overrides):
    """A helper week with the same blocks every day, some days overridden."""
    return {day: list(blocks) for day in HA_SCHEDULE_WEEKDAY} | overrides


# the helper (YAML) blocks, the same for every day
HELPER_BLOCKS = [
    B("00:00", "06:30", 15),
    B("06:30", "08:00", 18),
    B("08:00", "24:00", 21),
]
# mts300 (heat/cool) helper blocks: the second one has no 'cool' (carried forward)
# and 22:00-24:00 is off
HELPER_BLOCKS_MTS300 = [
    B("00:00", "06:30", heat=17, cool=27),
    B("06:30", "08:00", heat=20),
    B("08:00", "22:00", heat=19, cool=25),
]
# verbatim from emulator_traces/U01234567890123456789012345678912-Kpippo-msh300hk-mts150.csv
# (line 25, Appliance.Hub.Mts100.ScheduleB GETACK, subdevice 03002B0E)
MTS100_TRACE: "dict[str, Any]" = {
    "id": "03002B0E",
    **{
        d: [[390, 150], [90, 180], [300, 180], [300, 210], [210, 210], [150, 150]]
        for d in ("mon", "tue", "wed", "thu", "fri")
    },
    **{
        d: [[390, 155], [165, 170], [225, 210], [300, 210], [195, 210], [165, 150]]
        for d in ("sat", "sun")
    },
}
# MTS100_TRACE monday (to friday) in the helper format: a block per entry
MTS100_TRACE_MONDAY = [
    B("00:00", "06:30", 15),
    B("06:30", "08:00", 18),
    B("08:00", "13:00", 18),
    B("13:00", "18:00", 21),
    B("18:00", "21:30", 21),
    B("21:30", "24:00", 15),
]
# this mts300 trace ScheduleB state carries per-day [minutes, heat, cold] entries
# (the same every day) and a 'pre' block
MTS300_TRACE = "U01234567890123456789012345678925-Kpippo-mts300.json.txt"
MTS300_TRACE_DAY = [[360, 2111, 2611], [900, 2111, 2388], [180, 2111, 2611]]
MTS300_TRACE_PRE = {"enable": 2, "time": 30}
# helper-formatted schedules (as returned by schedule.get_schedule or as
# accepted by meross_lan.set_schedule)
OTHER_SCHEDULE = week([B("00:00", "07:00", 16), B("07:00", "24:00", 20.5)])
# not compilable (overlapping blocks)
OVERLAP_SCHEDULE = week([B("00:00", "08:00", 16), B("07:00", "24:00", 20)])
SCHEDULE_FULL = week(
    [
        B("00:00", "06:30", 16),
        B("06:30", "08:00", 21),
        B("08:00", "17:00", 18),
        B("17:00", "22:30", 21.5),
        B("22:30", "24:00", 16),
    ]
)
# a device schedule day differing from HELPER_BLOCKS (see async_drift)
DRIFTED_DAY = [[720, 200], [720, 220]] + [[0, 220]] * 4


# a second device in the same hass (thermostats of different models sharing a helper)
OTHER_DEVICE_UUID = "01234567890123456789001122334466"


def set_channel_state(emulator: "Emulator", ns: "Namespace", key: str, value):
    """Sets the channel 0 state of ns ({key: value}) adding the ability if missing.
    (The emulator default states are shared: other tests may change them.)"""
    emulator.descriptor.ability[str.__str__(ns)] = {}
    emulator.namespaces[ns] = {ns.key: [{mc.KEY_CHANNEL: 0, key: value}]}


def build_mts300_emulator(uuid: str = tc.MOCK_DEVICE_UUID) -> "Emulator":
    """An mts300 in °C."""
    emulator = emulator_module.build_emulator(
        tc.EMULATOR_TRACES_PATH + MTS300_TRACE, key=tc.MOCK_KEY, uuid=uuid
    )
    set_channel_state(
        emulator, mn.Appliance_Control_TempUnit, mc.KEY_TEMPUNIT, mc.TEMPUNIT_CELSIUS
    )
    return emulator


def build_mts200_emulator(
    uuid: str = tc.MOCK_DEVICE_UUID,
    temp_unit: int | None = None,
    summer_mode: int | None = None,
) -> "Emulator":
    """An mts200 (Thermostat.Schedule, 8 slots per day): the trace carries no
    schedule so one is seeded. With temp_unit/summer_mode, it has the display unit
    (Appliance.Control.TempUnit)/summer mode setting set to it."""
    emulator = helpers.build_emulator(mc.TYPE_MTS200, uuid=uuid)
    if temp_unit:
        set_channel_state(
            emulator, mn.Appliance_Control_TempUnit, mc.KEY_TEMPUNIT, temp_unit
        )
    if summer_mode:
        set_channel_state(
            emulator,
            mn_t.Appliance_Control_Thermostat_SummerMode,
            mc.KEY_MODE,
            summer_mode,
        )
    ns = mn_t.Appliance_Control_Thermostat_Schedule
    emulator.namespaces[ns] = {
        ns.key: [
            {mc.KEY_CHANNEL: 0, mc.KEY_SECTION: 8}
            | {day: [[1440, 200]] + [[0, 200]] * 7 for day in MTS_SCHEDULE_WEEKDAY}
        ]
    }
    return emulator


def patch_read_helper(ha_schedule: "dict[str, Any]"):
    """Patches the helper read (the HA schedule helper content)."""

    async def _async_read_helper(self, entity_id, /):
        return ha_schedule

    return patch.object(ScheduleHelperSelect, "_async_read_helper", _async_read_helper)


class ScheduleContext:
    """Test state holder: the device context and its thermostats. The entities
    are looked up on access so that they're still valid after an entry reload."""

    def __init__(self, context: helpers.DeviceContext, /):
        self.context = context
        self.hass = context.hass
        self.emulator = context.emulator
        self.set_payloads: list["dict[str, Any]"] = []
        """Schedule SET payloads received by the emulator."""

    @property
    def climates(self) -> "list[MtsClimate]":
        device = self.context.device
        entities = (
            *device.entities.values(),
            *getattr(device, "subdevices", {}).values(),
        )
        # dict: keep the order, drop duplicates
        return list({id(e): e for e in entities if isinstance(e, MtsClimate)}.values())

    @property
    def climate(self):
        return self.climates[0]

    @property
    def select(self) -> ScheduleHelperSelect:
        return self.climate.select_schedule_helper

    def sensor_state(self, climate: MtsClimate | None = None):
        sensor = (climate or self.climate).sensor_schedule_sync
        state = self.hass.states.get(sensor.entity_id)
        assert state
        return state

    def _emulator_ns_state(self, climate: MtsClimate) -> "dict[str, Any]":
        ns = climate.SCHEDULE_NS
        ((index_key, index_value),) = climate.schedule.index.items()
        return next(
            p
            for p in self.emulator.namespaces[ns][ns.key]
            if p[index_key] == index_value
        )

    def emulator_schedule(self, climate: MtsClimate | None = None):
        """The (emulated) device schedule (weekdays only)."""
        p_state = self._emulator_ns_state(climate or self.climate)
        return {day: p_state[day] for day in MTS_SCHEDULE_WEEKDAY}

    async def async_set_device_schedule(
        self, native: "dict[str, Any]", climate: MtsClimate | None = None
    ):
        """Changes the emulated device schedule (as if done in the Meross app)
        and lets the climate (calendar) entity know about it."""
        climate = climate or self.climate
        p_state = self._emulator_ns_state(climate)
        for day in MTS_SCHEDULE_WEEKDAY:
            p_state[day] = [list(entry) for entry in native[day]]
        await self.async_refresh_device_schedule(climate)

    async def async_drift(self):
        """Changes the device schedule elsewhere (monday: DRIFTED_DAY)."""
        await self.async_set_device_schedule(
            self.emulator_schedule() | {"mon": DRIFTED_DAY}
        )

    async def async_refresh_device_schedule(self, climate: MtsClimate | None = None):
        schedule = (climate or self.climate).schedule
        await schedule.handler_ns.async_get(schedule.index)
        await self.hass.async_block_till_done()

    def compile(self, ha_schedule, climate: MtsClimate | None = None):
        return compile_schedule(
            ha_schedule, **schedule_compile_kwargs(climate or self.climate, self.hass)
        ).native

    async def async_tick(self, delay: float):
        await self.context.time_mock.async_tick(delay)

    async def async_restart(
        self, linked: str = HELPER_ENTITY_ID, sync: bool = True, stored: bool = True
    ):
        """Reloads the entry restoring the select state 'linked' and its stored data
        (none when stored=False, as on the first start after an upgrade) as on HA
        restart, and lets the (debounced) sync run unless sync=False."""
        select = self.select
        # (stored as json)
        extra_data = (
            json.loads(json.dumps(select.extra_restore_state_data.as_dict()))
            if stored
            else {}
        )
        assert await self.context.async_unload()
        mock_restore_cache_with_extra_data(
            self.hass, [(State(select.entity_id, linked), extra_data)]
        )
        await self.context.perform_coldstart()
        if sync:
            await self.async_tick(ScheduleHelperSelect.SYNC_DELAY)

    @contextlib.asynccontextmanager
    async def async_hold_writes(self):
        """Holds the schedule SET requests until the context exits (then waits for
        the pending tasks). Yields a coroutine function waiting (5 s at most) for
        a held request."""
        started, gate = asyncio.Event(), asyncio.Event()
        async_request_payload = MtsSchedule.async_request_payload

        async def _async_request_payload(schedule, payload, /):
            started.set()
            await gate.wait()
            return await async_request_payload(schedule, payload)

        with patch.object(MtsSchedule, "async_request_payload", _async_request_payload):
            try:
                yield lambda: asyncio.wait_for(started.wait(), 5)
            finally:
                gate.set()
            await self.hass.async_block_till_done()

    def button(self, entity_key: str, climate: MtsClimate | None = None) -> "Button":
        """The schedule_pull/schedule_push button of the climate."""
        climate = climate or self.climate
        return next(
            entity
            for entity in climate.parent.entities.values()
            if entity.entity_key == entity_key
            and entity.device_info == climate.device_info
        )  # type: ignore

    async def async_press(
        self,
        entity_key: str,
        climate: MtsClimate | None = None,
        context: "Context | None" = None,
    ):
        await self.hass.services.async_call(
            "button",
            "press",
            {"entity_id": self.button(entity_key, climate).entity_id},
            blocking=True,
            context=context,
        )

    def fire_helper_event(self):
        """Re-writes the helper state unchanged (a state_reported event)."""
        state = self.hass.states.get(HELPER_ENTITY_ID)
        assert state
        self.hass.states.async_set(HELPER_ENTITY_ID, state.state, state.attributes)

    async def async_call(
        self, service: str, entity_id: str, /, response: bool = False, **data
    ):
        return await self.hass.services.async_call(
            mlc.DOMAIN,
            service,
            {"entity_id": entity_id, **data},
            blocking=True,
            return_response=response or service == mlc.SERVICE_GET_SCHEDULE,
        )


@contextlib.asynccontextmanager
async def async_schedule_device(
    request: "FixtureRequest", hass: "HomeAssistant", device: "Emulator | str"
):
    """Sets up the device spying its schedule SETs."""
    async with helpers.DeviceContext(request, hass, device, auto_poll=True) as context:
        ctx = ScheduleContext(context)
        emulator_class = type(ctx.emulator)
        _handle_message = emulator_class._handle_message
        schedule_namespaces = {climate.SCHEDULE_NS for climate in ctx.climates}

        def _handle_message_spy(emulator, header, payload, /):
            if (
                (emulator is ctx.emulator)
                and (header[mc.KEY_METHOD] == mc.METHOD_SET)
                and (header[mc.KEY_NAMESPACE] in schedule_namespaces)
            ):
                ctx.set_payloads.append(payload)
            return _handle_message(emulator, header, payload)

        with patch.object(emulator_class, "_handle_message", _handle_message_spy):
            yield ctx


@contextlib.asynccontextmanager
async def async_schedule_context(
    request: "FixtureRequest",
    hass: "HomeAssistant",
    device: "Emulator | str" = mc.TYPE_MSH300,
    helper_blocks: "list[dict[str, Any]]" = HELPER_BLOCKS,
):
    """Sets up the 'schedule.heating' (YAML) helper (helper_blocks every day) and
    the device (see async_schedule_device)."""
    assert await async_setup_component(
        hass,
        "schedule",
        {
            "schedule": {
                "heating": {"name": "Heating"}
                | {day: helper_blocks for day in HA_SCHEDULE_WEEKDAY}
            }
        },
    )
    try:
        async with async_schedule_device(request, hass, device) as ctx:
            yield ctx
    finally:
        # remove the helpers so that their (state update) timers don't linger
        for helper in list(hass.data[DATA_INSTANCES]["schedule"].entities):
            await helper.async_remove()
