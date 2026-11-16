"""Test various Device apis both from merossclient.device.Device and from meross_lan.helpers.device.Device."""

import logging

from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
import pytest

from custom_components.meross_lan.merossclient import async_available_timezones
from custom_components.meross_lan.merossclient.protocol import (
    const as mc,
    namespaces as mn,
)
from custom_components.meross_lan.merossclient.protocol.message import MerossMessage

from tests import const as tc, helpers


async def test_poll_with_handler_added_while_polling(
    request, hass: "HomeAssistant", caplog: pytest.LogCaptureFixture
):
    """
    A namespace handler might be added while the polling loop is awaiting (e.g. when
    an unsolicited PUSH for a not yet managed namespace is received). The polling
    cycle must not be aborted with 'dictionary changed size during iteration'.
    """
    async with helpers.DeviceContext(
        request, hass, mc.TYPE_MSS310, auto_poll=True
    ) as device_context:
        device = device_context.device
        unknown_ns = "Appliance.Test.PushWhilePolling"
        assert unknown_ns not in device.ns_handlers
        polled_nh = next(
            nh
            for nh in device.ns_handlers.values()
            if nh.polling_strategy and not nh.digest
        )
        polling_strategy = polled_nh.polling_strategy
        assert polling_strategy

        async def _polling_strategy_with_push(nh):
            # simulate a PUSH received while the poll is in progress
            device._handle(
                MerossMessage.build(
                    unknown_ns,
                    mc.METHOD_PUSH,
                    {"pushWhilePolling": {"value": 1}},
                    device.key,
                )
            )
            await polling_strategy(nh)

        polled_nh.polling_strategy = _polling_strategy_with_push
        await device_context.async_poll_single()

        assert unknown_ns in device.ns_handlers
        for record in caplog.records:
            if record.levelno >= logging.WARNING:
                assert "dictionary changed size" not in record.getMessage()


async def test_device_timedelta_ignores_zero_timestamp(request, hass: "HomeAssistant"):
    """
    A received message carrying a 0 (invalid) header timestamp must not be accounted
    in the device timestamp/timedelta tracking (a single one would otherwise add
    ~10% of the current epoch to the device_timedelta average).
    """
    async with helpers.DeviceContext(
        request, hass, mc.TYPE_MSS310, auto_poll=True
    ) as device_context:
        device = device_context.device
        tolerance = device.PARAM_DEVICE_TIMESTAMP_TOLERANCE
        assert abs(device.device_timedelta) < tolerance
        device_timestamp = device.device_timestamp
        assert device_timestamp

        message = MerossMessage.build(
            mn.Appliance_System_Online,
            mc.METHOD_PUSH,
            {mn.Appliance_System_Online.key: {mc.KEY_STATUS: mc.STATUS_ONLINE}},
            device.key,
        )
        message.header[mc.KEY_TIMESTAMP] = 0
        assert device.http
        device.on_rx(message, device.http)

        assert device.device_timestamp == device_timestamp
        assert abs(device.device_timedelta) < tolerance


async def test_async_configure_timezone(request, hass: "HomeAssistant"):
    """
    Verify timezone configuration works (useful to cover #687)
    """

    tznames = await async_available_timezones()
    emulator = helpers.build_emulator(mc.TYPE_MSS310, timezone="")

    async with helpers.DeviceContext(
        request, hass, emulator, auto_poll=True
    ) as device_context:

        device = device_context.device

        for tzname in tznames:
            assert device.descriptor.timezone != tzname
            await device.async_configure_timezone(tzname)
            assert device.descriptor.timezone == tzname
