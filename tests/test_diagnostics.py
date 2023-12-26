"""
    Tests the HA diagnostics and device tracing feature
"""
import math
import time

from homeassistant.core import HomeAssistant

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

        assert (device := context.device)

        result = await hass.config_entries.options.async_init(device.config_entry_id)

        trace_timeout = tc.MOCK_TRACE_TIMEOUT

        result = await hass.config_entries.options.async_configure(
            result["flow_id"],
            user_input={
                mlc.CONF_HOST: device.host,
                mlc.CONF_KEY: device.key,
                mlc.CONF_PROTOCOL: mlc.CONF_PROTOCOL_HTTP,
                mlc.CONF_TRACE: True,
                mlc.CONF_TRACE_TIMEOUT: trace_timeout,
            },
        )
        await hass.async_block_till_done()

        assert device._trace_file
        # the endtime of the trace is not checked 'absolutely' due to float rounding
        # so we just check it is close to expected
        assert (
            math.fabs(
                device._trace_endtime
                - (time.time() + trace_timeout - tc.MOCK_HTTP_RESPONSE_DELAY)
            )
            < tc.MOCK_HTTP_RESPONSE_DELAY
        )

        await context.async_warp(
            trace_timeout + device.polling_period,
            tick=mlc.PARAM_TRACING_ABILITY_POLL_TIMEOUT,
        )

        assert device._trace_file is None
