"""
    Tests the HA diagnostics and device tracing feature
"""
import asyncio
import time

from homeassistant.core import HomeAssistant

from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.meross_lan.diagnostics import async_get_device_diagnostics
from custom_components.meross_lan.merossclient import const as mc
from custom_components.meross_lan.const import (
    CONF_HOST,
    CONF_KEY,
    CONF_TRACE,
    CONF_TRACE_TIMEOUT_DEFAULT,
    PARAM_TRACING_ABILITY_POLL_TIMEOUT
)

from .helpers import devicecontext

async def test_diagnostics(hass: HomeAssistant, aioclient_mock):

    async with devicecontext(mc.TYPE_MSS310, hass, aioclient_mock) as context:
        await context.perform_coldstart()

        context.warp(tick=PARAM_TRACING_ABILITY_POLL_TIMEOUT)
        diagnostic = await async_get_device_diagnostics(hass, context.config_entry, None)
        await context.async_stopwarp()
        assert diagnostic


async def test_tracing(hass: HomeAssistant, aioclient_mock):

    async with devicecontext(mc.TYPE_MSS310, hass, aioclient_mock) as context:
        await context.perform_coldstart()

        result = await hass.config_entries.options.async_init(
            context.device.entry_id
        )

        result = await hass.config_entries.options.async_configure(
            result["flow_id"],
            user_input={
                CONF_HOST: context.device.host,
                CONF_KEY: context.device.key,
                CONF_TRACE: True
            },
        )
        await hass.async_block_till_done()

        assert context.device._trace_file
        assert context.device._trace_endtime == time.time() + CONF_TRACE_TIMEOUT_DEFAULT


        await context.async_warp(
            CONF_TRACE_TIMEOUT_DEFAULT + context.device.polling_period,
            tick=PARAM_TRACING_ABILITY_POLL_TIMEOUT)
        """
        # use count as a bounding limit for the test loop
        # but we don't really know how many abilities get queried since the device
        # has TRACE_ABILITY_EXCLUDE in place
        count = len(context.device.descriptor.ability)
        while count and context.device._trace_ability_iter is not None:
            context.time.tick(delta=PARAM_TRACING_ABILITY_POLL_TIMEOUT) # type: ignore
            async_fire_time_changed(hass)
            await hass.async_block_till_done()
            count -=1

        # lets finish tracing
        context.time.tick(delta=CONF_TRACE_TIMEOUT_DEFAULT) # type: ignore
        async_fire_time_changed(hass)
        await hass.async_block_till_done()
        """

        assert context.device._trace_file is None