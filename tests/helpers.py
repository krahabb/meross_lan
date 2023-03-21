""""""
from asyncio import Future, run_coroutine_threadsafe
from contextlib import asynccontextmanager, contextmanager
from datetime import datetime, timedelta

from freezegun.api import FrozenDateTimeFactory, StepTickTimeFactory, freeze_time
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed_exact,
)
from pytest_homeassistant_custom_component.test_util.aiohttp import (
    AiohttpClientMocker,
    AiohttpClientMockResponse,
)

from custom_components.meross_lan import MerossApi, MerossDevice
from custom_components.meross_lan.const import (
    CONF_DEVICE_ID,
    CONF_HOST,
    CONF_KEY,
    CONF_PAYLOAD,
    CONF_POLLING_PERIOD,
    CONF_PROTOCOL,
    CONF_PROTOCOL_HTTP,
    DOMAIN,
    PARAM_COLDSTARTPOLL_DELAY,
)
from custom_components.meross_lan.merossclient import const as mc
import emulator
from emulator import MerossEmulator

from .const import (
    EMULATOR_TRACES_MAP,
    EMULATOR_TRACES_PATH,
    MOCK_DEVICE_UUID,
    MOCK_KEY,
    MOCK_POLLING_PERIOD,
)


def build_emulator(model: str) -> MerossEmulator:
    # Watchout: this call will not use the uuid and key set
    # in the filename, just DEFAULT_UUID and DEFAULT_KEY
    return emulator.build_emulator(
        EMULATOR_TRACES_PATH + EMULATOR_TRACES_MAP[model], MOCK_DEVICE_UUID, MOCK_KEY
    )


def build_emulator_config_entry(emulator: MerossEmulator):

    device_uuid = emulator.descriptor.uuid
    return MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_DEVICE_ID: device_uuid,
            CONF_HOST: device_uuid,
            CONF_KEY: emulator.key,
            CONF_PAYLOAD: {
                mc.KEY_ALL: emulator.descriptor.all,
                mc.KEY_ABILITY: emulator.descriptor.ability,
            },
            CONF_PROTOCOL: CONF_PROTOCOL_HTTP,
            CONF_POLLING_PERIOD: MOCK_POLLING_PERIOD,
        },
        unique_id=device_uuid,
        version=1,
    )


@contextmanager
def emulator_mock(
    emulator_: MerossEmulator | str, aioclient_mock: "AiohttpClientMocker"
):
    """
    This context provides an emulator working on HTTP  by leveraging
    the aioclient_mock.
    This is a basic mock which is not polluting HA
    """
    try:
        if isinstance(emulator_, str):
            emulator_ = build_emulator(emulator_)

        async def _handle_http_request(method, url, data):
            response = emulator_.handle(data)
            return AiohttpClientMockResponse(method, url, json=response)

        # we'll use the uuid so we can mock multiple at the same time
        # and the aioclient_mock will route accordingly
        aioclient_mock.post(
            f"http://{emulator_.descriptor.uuid}/config",
            side_effect=_handle_http_request,
        )

        yield emulator_

    finally:
        # remove the mock from aioclient
        aioclient_mock.clear_requests()


class DeviceContext:
    config_entry: MockConfigEntry
    api: MerossApi
    emulator: MerossEmulator
    device: MerossDevice
    time: FrozenDateTimeFactory | StepTickTimeFactory
    _warp_task: Future | None = None
    _warp_run: bool

    async def perform_coldstart(self):
        """
        to be called after setting up a device (context) to actually
        execute the cold-start polling sequence.
        After this the device should be online and all the polling
        namespaces done
        """
        self.time.tick(timedelta(seconds=PARAM_COLDSTARTPOLL_DELAY))
        async_fire_time_changed_exact(self.api.hass)
        await self.api.hass.async_block_till_done()
        assert self.device.online

    def warp(self, tick: float | int | timedelta = 0.5):
        """
        starts an asynchronous task which manipulates our
        freze_time so the time passes and get advanced to
        time.time() + timeout.
        While passing it tries to perform HA events rollout
        every tick seconds
        """
        assert self._warp_task is None

        if not isinstance(tick, timedelta):
            tick = timedelta(seconds=tick)
        hass = self.api.hass

        async def _async_tick():
            self.time.tick(delta=tick)
            async_fire_time_changed_exact(hass)
            await hass.async_block_till_done()

        def _warp():

            while self._warp_run:
                run_coroutine_threadsafe(_async_tick(), hass.loop)

        self._warp_run = True
        self._warp_task = hass.async_add_executor_job(_warp)

    async def async_stopwarp(self):
        assert self._warp_task
        self._warp_run = False
        await self._warp_task
        self._warp_task = None

    async def async_warp(
        self,
        timeout: float | int | timedelta | datetime,
        tick: float | int | timedelta = 1,
    ):

        if not isinstance(timeout, datetime):
            if isinstance(timeout, timedelta):
                timeout = self.time() + timeout
            else:
                timeout = self.time() + timedelta(seconds=timeout)
        if not isinstance(tick, timedelta):
            tick = timedelta(seconds=tick)

        hass = self.api.hass
        while self.time() < timeout:
            self.time.tick(delta=tick)
            async_fire_time_changed_exact(hass)
            await hass.async_block_till_done()


@asynccontextmanager
async def devicecontext(
    emulator: MerossEmulator | str,
    hass: HomeAssistant,
    aioclient_mock: "AiohttpClientMocker",
):
    """
    This is a 'full featured' context providing an emulator and setting it
    up as a configured device in HA
    It also provides timefreezing
    """
    with emulator_mock(emulator, aioclient_mock) as emulator:
        with freeze_time() as frozen_time:

            config_entry = build_emulator_config_entry(emulator)
            config_entry.add_to_hass(hass)
            assert await hass.config_entries.async_setup(config_entry.entry_id)
            await hass.async_block_till_done()
            try:
                context = DeviceContext()
                context.config_entry = config_entry
                context.emulator = emulator
                context.time = frozen_time
                context.api = hass.data[DOMAIN]
                context.device = context.api.devices[config_entry.unique_id]
                assert not context.device.online
                yield context

            finally:
                assert await hass.config_entries.async_unload(config_entry.entry_id)
                await hass.async_block_till_done()
                assert config_entry.unique_id not in hass.data[DOMAIN].devices
