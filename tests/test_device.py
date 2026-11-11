"""Test various Device apis both from merossclient.device.Device and from meross_lan.helpers.device.Device."""

import asyncio
import json
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
import pytest

from custom_components.meross_lan.merossclient import async_available_timezones
from custom_components.meross_lan.merossclient.device import Device
from custom_components.meross_lan.merossclient.protocol import (
    const as mc,
    namespaces as mn,
)
from custom_components.meross_lan.merossclient.protocol.message import MerossMessage

from tests import const as tc, helpers


@pytest.mark.parametrize("allow_publish", [False, True])
async def test_async_connect_respects_mqtt_publish_setting(allow_publish):
    response = SimpleNamespace(json="{}")

    def create_task(coro, name, *, eager_start):
        return asyncio.create_task(coro, name=name, eager_start=eager_start)

    http = SimpleNamespace(
        TRANSPORT="http",
        create_task=create_task,
        async_request=AsyncMock(return_value=response),
    )
    mqtt = SimpleNamespace(
        TRANSPORT="mqtt",
        connection=SimpleNamespace(allow_publish=allow_publish, can_publish=False),
        create_task=create_task,
        async_request=AsyncMock(return_value=response),
    )
    device = SimpleNamespace(
        mqtt=mqtt,
        _clients={"mqtt": mqtt, "http": http},
        handler_all=SimpleNamespace(polling_request=("poll",), handle=Mock()),
        is_connected=False,
        on_connect=Mock(),
        timeout=1,
    )

    assert await Device.async_connect(device) is response
    http.async_request.assert_awaited_once_with("poll")
    if allow_publish:
        mqtt.async_request.assert_awaited_once_with("poll")
    else:
        mqtt.async_request.assert_not_called()
    assert device._clients["mqtt"] is mqtt
    device.handler_all.handle.assert_called_once_with(response)


@pytest.mark.parametrize(
    "uuid", ["01234567890123456789012345678913", "0123456789012345678901234567891C"]
)
async def test_msg200_http_heartbeat_is_polled_separately(request, hass, caplog, uuid):
    (emulator,) = helpers.build_emulators([uuid])
    async with helpers.DeviceContext(
        request, hass, emulator, auto_poll=True
    ) as context:
        device = context.device
        last_poll_epoch = device.handler_all.last_poll_epoch
        context.aioclient_mock.mock_calls.clear()
        caplog.clear()

        await context.time_mock.async_tick(device.HEARTBEAT_TIMEOUT + 1)

        requests = [
            json.loads(data) for _, _, data, _ in context.aioclient_mock.mock_calls
        ]
        namespaces = [msg[mc.KEY_HEADER][mc.KEY_NAMESPACE] for msg in requests]
        assert mn.Appliance_System_All in namespaces
        assert mn.Appliance_Control_Multiple in namespaces
        assert device.handler_all.last_poll_epoch > last_poll_epoch
        assert "JSONDecodeError" not in caplog.text


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
