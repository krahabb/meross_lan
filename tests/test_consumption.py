"""
    Test the ConsumptionxMixin works, especially on reset bugs (#264,#268)
"""

import datetime as dt
import typing
from zoneinfo import ZoneInfo

from homeassistant.const import STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant
import homeassistant.util.dt as dt_util
import pytest
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
)

from custom_components.meross_lan import const as mlc
from custom_components.meross_lan.devices.mss import (
    ConsumptionXSensor,
    ElectricitySensor,
)
from custom_components.meross_lan.merossclient import const as mc
from custom_components.meross_lan.sensor import MLNumericSensor
from emulator.mixins.electricity import (
    ConsumptionXMixin as EmulatorConsumptionMixin,
    ElectricityMixin as EmulatorElectricityMixin,
)

from tests import helpers

if typing.TYPE_CHECKING:
    from .helpers import DeviceContext


# set TEST_POWER and TEST_DURATION so they produce at least
# 1 Wh of energy
TEST_POWER = 1000  # unit: W
TEST_DURATION = int(3600 / TEST_POWER) + 1  # unit: secs
if TEST_DURATION < 5 * mlc.PARAM_ENERGY_UPDATE_PERIOD:
    TEST_DURATION = 5 * mlc.PARAM_ENERGY_UPDATE_PERIOD

# some exotic tzs to disalign the device midnight from HA
# "Asia/Bangkok" GMT + 7
# "Asia/Baku" GMT + 4
DEVICE_TIMEZONE = "Asia/Baku"


def _configure_dates(tz):
    today = dt.datetime.now(tz)
    today = dt.datetime(
        today.year,
        today.month,
        today.day,
        tzinfo=tz,
    )
    tomorrow = today + dt.timedelta(days=1)
    # make dates naive (representing UTC) and compatible with freezegun api
    # see freezegun.api.convert_to_timezone_naive
    offset = today.utcoffset()
    assert offset is not None
    today -= offset
    today = today.replace(tzinfo=None)
    offset = tomorrow.utcoffset()
    assert offset is not None
    tomorrow -= offset
    tomorrow = tomorrow.replace(tzinfo=None)
    todayseconds = (tomorrow - today).total_seconds()

    return today, tomorrow, todayseconds


async def _async_configure_context(context: "DeviceContext", timezone: str):
    emulator = context.emulator
    assert isinstance(emulator, EmulatorConsumptionMixin)
    assert isinstance(emulator, EmulatorElectricityMixin)
    emulator.set_timezone(timezone)
    emulator.set_power(TEST_POWER * 1000)

    assert await context.async_setup()

    device = context.device
    assert (
        device.polling_period < 60
    ), "Configured polling period is too exotic...the test will not work"

    await context.perform_coldstart()

    states = context.hass.states

    sensor_power = device.entities[mc.KEY_POWER]
    assert isinstance(sensor_power, MLNumericSensor)
    powerstate = states.get(sensor_power.entity_id)
    assert powerstate
    assert float(powerstate.state) == TEST_POWER

    sensor_consumption = device.entities[mlc.CONSUMPTIONX_SENSOR_KEY]
    assert isinstance(sensor_consumption, ConsumptionXSensor)
    consumptionstate = states.get(sensor_consumption.entity_id)
    assert consumptionstate
    assert int(consumptionstate.state) == 0

    sensor_electricity = device.entities[mlc.ELECTRICITY_SENSOR_KEY]
    assert isinstance(sensor_electricity, ElectricitySensor)
    # energy_estimate is disabled by default
    assert states.get(sensor_electricity.entity_id) is None

    return device, sensor_consumption, sensor_electricity


async def test_consumption(hass: HomeAssistant, aioclient_mock):
    """
    Basic test with device timezone set the same as HA localtime
    so the consumption and the meross_lan estimate resets at the same
    time i.e. at midnight local time. The test will ensure the correctness
    at startup, right before midnight, and check if the reset 'BUG' is
    correctly managed at day start
    """
    hass_states_get = hass.states.get
    today, tomorrow, todayseconds = _configure_dates(dt_util.DEFAULT_TIME_ZONE)

    async with helpers.DeviceContext(
        hass, mc.TYPE_MSS310, aioclient_mock, time=today
    ) as context:
        device, sensor_consumption, sensor_electricity = await _async_configure_context(
            context, dt_util.DEFAULT_TIME_ZONE.key  # type: ignore
        )

        polling_tick = dt.timedelta(seconds=device.polling_period)

        def _check_energy_states(power, duration, msg):
            # consumption values are hard to predict due to the polling
            # discrete nature ...so we just check for 'reasonable' values
            # They too are 'natively' rounded off due to float -> int
            # energy = power * duration / 3600
            # energy_low might be off since the consumption endpoint
            # is polled on PARAM_ENERGY_UPDATE_PERIOD timeout
            energy_low = int(power * (duration - mlc.PARAM_ENERGY_UPDATE_PERIOD) / 3600)
            # even though energy_high should be accurate, the emulator
            # (and real devices) will carry a 'quantum' of energy from the day before
            energy_high = (
                int(power * (duration + mlc.PARAM_ENERGY_UPDATE_PERIOD) / 3600) + 1
            )
            consumptionstate = hass_states_get(sensor_consumption.entity_id)
            assert consumptionstate, msg
            assert (
                energy_low <= int(consumptionstate.state) <= energy_high + 1
            ), f"consumption in {msg}"
            assert (
                energy_low <= sensor_electricity.native_value <= energy_high  # type: ignore
            ), f"estimate in {msg}"

        await context.async_poll_timeout(TEST_DURATION)
        _check_energy_states(TEST_POWER, TEST_DURATION, "boot measures")

        # since the device polling callback checks for timeouts in communication,
        # this next 'tick' will move the time close to midnight
        # without tripping and should just trigger the 'heartbeat'
        # we need to also ensure the ConsumptionMixin logic catches the last
        # consumption transition before end of day in order for the
        # offset bug correction logic to kick in correctly so we move the time
        # to a point before midnight where the reported consumption 'will' change
        # before midnight
        await context.time.async_move_to(
            tomorrow
            - dt.timedelta(seconds=TEST_DURATION + mlc.PARAM_ENERGY_UPDATE_PERIOD)
        )
        # now the device polling state is good. We'll tick the states across
        # midnight and check the ongoing updates
        while True:
            await context.async_poll_single()
            if context.time() + polling_tick >= tomorrow:
                # the next poll will be after midnight
                # so we're checking last values before the trip
                _check_energy_states(
                    TEST_POWER, todayseconds, "end of the day measures"
                )
                yesterday_consumption = sensor_consumption.native_value
                assert yesterday_consumption is not None
                # the estimate should be reset right at midnight
                await context.time.async_move_to(tomorrow)
                assert sensor_electricity.native_value == 0
                break

        await context.async_poll_timeout(TEST_DURATION)
        _check_energy_states(TEST_POWER, TEST_DURATION, "begin of the day measures")

        # our emulator 'BUG' doesnt reset consumption so the new day offset
        # should be right equal to 'yesterday_consumption' or 1 off
        consumptionstate = hass_states_get(sensor_consumption.entity_id)
        assert consumptionstate
        assert "offset" in consumptionstate.attributes
        assert (
            consumptionstate.attributes["offset"] == yesterday_consumption - 1
            or consumptionstate.attributes["offset"] == yesterday_consumption
        )


async def test_consumption_with_timezone(hass: HomeAssistant, aioclient_mock):
    """
    test with device timezone set different than HA localtime so the consumption
    and the meross_lan estimate resets at different times. The test will ensure
    the correctness of consumption (the estimate was already tested) at startup,
    right before device midnight, and check if the reset 'BUG' is correctly
    managed at day start (in device local time)
    """
    hass_states_get = hass.states.get
    today, tomorrow, todayseconds = _configure_dates(ZoneInfo(DEVICE_TIMEZONE))

    async with helpers.DeviceContext(
        hass, mc.TYPE_MSS310, aioclient_mock, time=today
    ) as context:
        device, sensor_consumption, sensor_electricity = await _async_configure_context(
            context, DEVICE_TIMEZONE
        )

        polling_tick = dt.timedelta(seconds=device.polling_period)

        def _check_energy_states(power, duration, msg):
            # consumption values are hard to predict due to the polling
            # discrete nature ...so we just check for 'reasonable' values
            # They too are 'natively' rounded off due to float -> int
            # energy = power * duration / 3600
            # energy_low might be off since the consumption endpoint
            # is polled on PARAM_ENERGY_UPDATE_PERIOD timeout
            energy_low = int(power * (duration - mlc.PARAM_ENERGY_UPDATE_PERIOD) / 3600)
            # even though energy_high should be accurate, the emulator
            # (and real devices) will carry a 'quantum' of energy from the day before
            energy_high = (
                int(power * (duration + mlc.PARAM_ENERGY_UPDATE_PERIOD) / 3600) + 1
            )
            consumptionstate = hass_states_get(sensor_consumption.entity_id)
            assert consumptionstate, msg
            assert (
                energy_low <= int(consumptionstate.state) <= energy_high + 1
            ), f"consumption in {msg}"

        await context.async_poll_timeout(TEST_DURATION)
        _check_energy_states(TEST_POWER, TEST_DURATION, "boot measures")

        # since the device polling callback checks for timeouts in communication,
        # this next 'tick' will move the time close to midnight
        # without tripping and should just trigger the 'heartbeat'
        # we need to also ensure the ConsumptionMixin logic catches the last
        # consumption transition before end of day in order for the
        # offset bug correction logic to kick in correctly so we move the time
        # to a point before midnight where the reported consumption 'will' change
        # before midnight
        await context.time.async_move_to(
            tomorrow
            - dt.timedelta(seconds=TEST_DURATION + mlc.PARAM_ENERGY_UPDATE_PERIOD)
        )
        # now the device polling state is good. We'll tick the states across
        # midnight and check the ongoing updates
        while True:
            await context.async_poll_single()
            if context.time() + polling_tick >= tomorrow:
                # the next poll will be after midnight
                # so we're checking last values before the trip
                _check_energy_states(
                    TEST_POWER, todayseconds, "end of the day measures"
                )
                yesterday_consumption = sensor_consumption.native_value
                assert yesterday_consumption is not None
                break

        await context.async_poll_timeout(TEST_DURATION)
        _check_energy_states(TEST_POWER, TEST_DURATION, "begin of the day measures")

        # our emulator 'BUG' doesnt reset consumption so the new day offset
        # should be right equal to 'yesterday_consumption' or 1 off
        consumptionstate = hass_states_get(sensor_consumption.entity_id)
        assert consumptionstate
        assert "offset" in consumptionstate.attributes
        assert (
            consumptionstate.attributes["offset"] == yesterday_consumption - 1
            or consumptionstate.attributes["offset"] == yesterday_consumption
        )


@pytest.mark.usefixtures("recorder_mock")
async def test_consumption_with_reload(hass: HomeAssistant, aioclient_mock):
    """
    This test will ensure the state is restored correctly when the device
    config_entry is reloaded due to a configuration change. This in turns also
    checks the homeassistant reload since the state is restored the same way
    """
    hass_states_get = hass.states.get
    today, tomorrow, todayseconds = _configure_dates(dt_util.DEFAULT_TIME_ZONE)

    async with helpers.DeviceContext(
        hass, mc.TYPE_MSS310, aioclient_mock, time=today
    ) as context:
        device, sensor_consumption, sensor_electricity = await _async_configure_context(
            context, dt_util.DEFAULT_TIME_ZONE.key  # type: ignore
        )

        polling_tick = dt.timedelta(seconds=device.polling_period)
        sensor_consumption_entity_id = sensor_consumption.entity_id
        sensor_estimate_entity_id = sensor_electricity.entity_id

        device = await context.async_enable_entity(sensor_estimate_entity_id)
        # 'async_enable_entity' will invalidate our references
        sensor_consumption = device.entities[mlc.CONSUMPTIONX_SENSOR_KEY]
        assert isinstance(sensor_consumption, ConsumptionXSensor)
        sensor_electricity = device.entities[mlc.ELECTRICITY_SENSOR_KEY]
        assert isinstance(sensor_electricity, ElectricitySensor)

        def _check_energy_states(power, duration, msg):
            # consumption values are hard to predict due to the polling
            # discrete nature ...so we just check for 'reasonable' values
            # They too are 'natively' rounded off due to float -> int
            # energy = power * duration / 3600
            # energy_low might be off since the consumption endpoint
            # is polled on PARAM_ENERGY_UPDATE_PERIOD timeout
            energy_low = int(power * (duration - mlc.PARAM_ENERGY_UPDATE_PERIOD) / 3600)
            # even though energy_high should be accurate, the emulator
            # (and real devices) will carry a 'quantum' of energy from the day before
            energy_high = (
                int(power * (duration + mlc.PARAM_ENERGY_UPDATE_PERIOD) / 3600) + 1
            )
            consumptionstate = hass_states_get(sensor_consumption_entity_id)
            assert consumptionstate, msg
            assert (
                energy_low <= int(consumptionstate.state) <= energy_high + 1
            ), f"consumption in {msg}"

        async def _async_unload_reload(msg: str, offset: int):
            estimatestate = hass_states_get(sensor_estimate_entity_id)
            assert estimatestate
            saved_estimated_energy_value = estimatestate.state

            assert await context.async_unload()
            # device has been destroyed and entities should be unavailable
            consumptionstate = hass_states_get(sensor_consumption_entity_id)
            assert consumptionstate and (consumptionstate.state == STATE_UNAVAILABLE)
            estimatestate = hass_states_get(sensor_estimate_entity_id)
            assert estimatestate and (estimatestate.state == STATE_UNAVAILABLE)

            await async_wait_recording_done(hass)

            # move the time before reloading to make the emulator accumulate some energy
            await context.time.async_tick(dt.timedelta(seconds=2 * TEST_DURATION))

            assert await context.async_setup()
            device = context.device
            sensor_consumption = device.entities[mlc.CONSUMPTIONX_SENSOR_KEY]
            assert isinstance(sensor_consumption, ConsumptionXSensor)
            sensor_electricity = device.entities[mlc.ELECTRICITY_SENSOR_KEY]
            assert isinstance(sensor_electricity, ElectricitySensor)

            # sensor states should have been restored
            assert sensor_consumption.offset == offset
            consumptionstate = hass_states_get(sensor_consumption_entity_id)
            assert consumptionstate and consumptionstate.state == STATE_UNAVAILABLE
            estimatestate = hass_states_get(sensor_estimate_entity_id)
            assert estimatestate and estimatestate.state == saved_estimated_energy_value

            # online the device
            await context.perform_coldstart()
            # check the real consumption
            _check_energy_states(TEST_POWER, 3 * TEST_DURATION, msg)
            return device, sensor_consumption, sensor_electricity

        await context.async_poll_timeout(TEST_DURATION)
        _check_energy_states(TEST_POWER, TEST_DURATION, "boot measures")

        device, sensor_consumption, sensor_electricity = await _async_unload_reload(
            "reboot no offset", 0
        )

        # since the device polling callback checks for timeouts in communication,
        # this next 'tick' will move the time close to midnight
        # without tripping and should just trigger the 'heartbeat'
        # we need to also ensure the ConsumptionMixin logic catches the last
        # consumption transition before end of day in order for the
        # offset bug correction logic to kick in correctly so we move the time
        # to a point before midnight where the reported consumption 'will' change
        # before midnight
        await context.time.async_move_to(
            tomorrow
            - dt.timedelta(seconds=TEST_DURATION + mlc.PARAM_ENERGY_UPDATE_PERIOD)
        )
        # now the device polling state is good. We'll tick the states across
        # midnight and check the ongoing updates
        while True:
            await context.async_poll_single()
            if context.time() + polling_tick >= tomorrow:
                # the next poll will be after midnight
                # so we're checking last values before the trip
                _check_energy_states(
                    TEST_POWER, todayseconds, "end of the day measures"
                )
                yesterday_consumption = sensor_consumption.native_value
                assert yesterday_consumption is not None
                # the estimate should be reset right at midnight
                await context.time.async_move_to(tomorrow)
                assert sensor_electricity.native_value == 0
                break

        await context.async_poll_timeout(TEST_DURATION)
        _check_energy_states(TEST_POWER, TEST_DURATION, "begin of the day measures")

        # our emulator 'BUG' doesnt reset consumption so the new day offset
        # should be right equal to 'yesterday_consumption' or 1 off
        consumptionstate = hass_states_get(sensor_consumption_entity_id)
        assert consumptionstate
        assert "offset" in consumptionstate.attributes
        today_offset = consumptionstate.attributes["offset"]
        assert (
            today_offset == yesterday_consumption - 1
            or today_offset == yesterday_consumption
        )

        # new we unload/reload/reboot again in order to see
        # if the consumption offset gets restored
        await _async_unload_reload("reboot with offset", today_offset)
