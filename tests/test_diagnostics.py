"""
    Tests the HA diagnostics and device tracing feature
"""
import math
import time

from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

from custom_components.meross_lan import const as mlc
from custom_components.meross_lan.diagnostics import (
    async_get_config_entry_diagnostics,
    async_get_device_diagnostics,
)
from custom_components.meross_lan.merossclient import const as mc

from tests import const as tc, helpers


async def _async_configure_options_tracing(entry_mock: helpers.ConfigEntryMocker):
    hass = entry_mock.hass

    options_flow = hass.config_entries.options
    result = await options_flow.async_init(entry_mock.config_entry_id)
    result = await helpers.async_assert_flow_menu_to_step(
        options_flow, result, "menu", "diagnostics"
    )
    result = await options_flow.async_configure(
        result["flow_id"],
        user_input={
            mlc.CONF_CREATE_DIAGNOSTIC_ENTITIES: False,
            mlc.CONF_LOGGING_LEVEL: "default",
            mlc.CONF_OBFUSCATE: True,
            mlc.CONF_TRACE: True,
            mlc.CONF_TRACE_TIMEOUT: tc.MOCK_TRACE_TIMEOUT,
        },
    )
    assert result["type"] == FlowResultType.CREATE_ENTRY  # type: ignore
    # after having choosen 'diagnostics' the config entry will reload and
    # should set to be tracing.
    await hass.async_block_till_done()  # reload was 'taskerized'
    assert (manager := entry_mock.manager)
    assert manager._trace_file


async def _async_run_tracing(
    entry_mock: helpers.ConfigEntryMocker, time_mock: helpers.TimeMocker
):
    manager = entry_mock.manager
    await time_mock.async_warp(
        tc.MOCK_TRACE_TIMEOUT,
        tick=mlc.PARAM_TRACING_ABILITY_POLL_TIMEOUT,
    )
    assert not manager._trace_file


async def test_mqtthub_diagnostics(
    hass: HomeAssistant, hamqtt_mock: helpers.HAMQTTMocker
):
    async with helpers.MQTTHubEntryMocker(hass) as entry_mock:
        await entry_mock.async_test_config_entry_diagnostics()


async def test_mqtthub_tracing(
    hass: HomeAssistant,
    hamqtt_mock: helpers.HAMQTTMocker,
    time_mock: helpers.TimeMocker,
):
    async with helpers.MQTTHubEntryMocker(hass) as entry_mock:
        await _async_configure_options_tracing(entry_mock)
        await _async_run_tracing(entry_mock, time_mock)


async def test_profile_diagnostics(hass: HomeAssistant):
    async with helpers.ProfileEntryMocker(hass) as entry_mock:
        await entry_mock.async_test_config_entry_diagnostics()


async def test_profile_tracing(
    hass: HomeAssistant,
    hamqtt_mock: helpers.HAMQTTMocker,
    time_mock: helpers.TimeMocker,
):
    async with helpers.ProfileEntryMocker(hass) as entry_mock:
        await _async_configure_options_tracing(entry_mock)
        await _async_run_tracing(entry_mock, time_mock)


async def test_device_diagnostics(hass: HomeAssistant, aioclient_mock):
    async with helpers.DeviceContext(hass, mc.TYPE_MSS310, aioclient_mock) as context:
        await context.perform_coldstart()

        context.warp(tick=mlc.PARAM_TRACING_ABILITY_POLL_TIMEOUT)
        diagnostic = await async_get_device_diagnostics(
            hass, context.config_entry, None
        )
        await context.async_stopwarp()
        assert diagnostic


async def test_device_tracing(hass: HomeAssistant, aioclient_mock):
    async with helpers.DeviceContext(hass, mc.TYPE_MSS310, aioclient_mock) as context:
        await context.perform_coldstart()

        await _async_configure_options_tracing(context)
        # We now need to 'coldstart' again the device
        await context.perform_coldstart()
        await _async_run_tracing(context, context._time_mock)
