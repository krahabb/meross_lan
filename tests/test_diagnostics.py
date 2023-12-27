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


async def test_mqtthub_diagnostics(
    hass: HomeAssistant, hamqtt_mock: helpers.HAMQTTMocker
):
    async with helpers.MQTTHubEntryMocker(hass) as mqtthub_entry_mock:
        diagnostic = await async_get_config_entry_diagnostics(
            hass, mqtthub_entry_mock.config_entry
        )
        assert diagnostic


async def test_profile_diagnostics(hass: HomeAssistant):
    async with helpers.ProfileEntryMocker(hass) as profile_entry_mock:
        diagnostic = await async_get_config_entry_diagnostics(
            hass, profile_entry_mock.config_entry
        )
        assert diagnostic


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

        options_flow = hass.config_entries.options
        result = await options_flow.async_init(context.config_entry_id)
        result = await helpers.async_assert_flow_menu_to_step(
            options_flow, result, "menu", "diagnostics"
        )
        result = await options_flow.async_configure(
            result["flow_id"],
            user_input={mlc.CONF_TRACE_TIMEOUT: tc.MOCK_TRACE_TIMEOUT},
        )
        assert result["type"] == FlowResultType.CREATE_ENTRY  # type: ignore
        # after having choosen 'diagnostics' the config entry will reload and the device
        # should set to be tracing.
        await hass.async_block_till_done() # reload was 'taskerized'
        device = context.device
        assert device.trace_file
        # the endtime of the trace is not checked 'absolutely' due to float rounding
        # so we just check it is close to expected
        assert (
            math.fabs(
                device._trace_endtime
                - (time.time() + tc.MOCK_TRACE_TIMEOUT - tc.MOCK_HTTP_RESPONSE_DELAY)
            )
            < tc.MOCK_HTTP_RESPONSE_DELAY
        )
        # We now need to 'coldstart' again the device
        device = await context.perform_coldstart()
        await context.async_warp(
            tc.MOCK_TRACE_TIMEOUT + device.polling_period,
            tick=mlc.PARAM_TRACING_ABILITY_POLL_TIMEOUT,
        )
        assert not device.trace_file
