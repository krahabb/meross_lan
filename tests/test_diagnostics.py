"""
    Tests the HA diagnostics and device tracing feature
"""
import math
import time

from homeassistant.core import HomeAssistant

from custom_components.meross_lan.const import (
    CONF_HOST,
    CONF_KEY,
    CONF_TRACE,
    CONF_TRACE_TIMEOUT_DEFAULT,
    PARAM_TRACING_ABILITY_POLL_TIMEOUT,
)
from custom_components.meross_lan.diagnostics import async_get_device_diagnostics
from custom_components.meross_lan.merossclient import const as mc

from .const import MOCK_HTTP_RESPONSE_DELAY
from .helpers import devicecontext


async def test_diagnostics(hass: HomeAssistant, aioclient_mock):

    async with devicecontext(mc.TYPE_MSS310, hass, aioclient_mock) as context:
        await context.perform_coldstart()

        context.warp(tick=PARAM_TRACING_ABILITY_POLL_TIMEOUT)
        diagnostic = await async_get_device_diagnostics(
            hass, context.config_entry, None
        )
        await context.async_stopwarp()
        assert diagnostic


async def test_tracing(hass: HomeAssistant, aioclient_mock):

    async with devicecontext(mc.TYPE_MSS310, hass, aioclient_mock) as context:
        await context.perform_coldstart()

        assert (device := context.device)

        result = await hass.config_entries.options.async_init(device.entry_id)

        result = await hass.config_entries.options.async_configure(
            result["flow_id"],
            user_input={
                CONF_HOST: device.host,
                CONF_KEY: device.key,
                CONF_TRACE: True,
            },
        )
        await hass.async_block_till_done()

        assert device._trace_file
        # the endtime of the trace is not checked 'absolutely' due to float rounding
        # so we just check it is close to expected
        assert (
            math.fabs(
                device._trace_endtime
                - (time.time() + CONF_TRACE_TIMEOUT_DEFAULT - MOCK_HTTP_RESPONSE_DELAY)
            )
            < MOCK_HTTP_RESPONSE_DELAY
        )

        await context.async_warp(
            CONF_TRACE_TIMEOUT_DEFAULT + device.polling_period,
            tick=PARAM_TRACING_ABILITY_POLL_TIMEOUT,
        )

        assert device._trace_file is None
