"""Test various Device apis both from merossclient.device.Device and from meross_lan.helpers.device.Device."""

from datetime import timedelta
import logging

import aiohttp
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
import pytest

from custom_components.meross_lan import const as mlc
from custom_components.meross_lan.merossclient import async_available_timezones
from custom_components.meross_lan.merossclient.client import Transport
from custom_components.meross_lan.merossclient.protocol import const as mc

from tests import const as tc, helpers


def _async_connect_warnings(caplog: pytest.LogCaptureFixture):
    return [
        record
        for record in caplog.records
        if record.levelno >= logging.WARNING
        and record.name.startswith("custom_components.meross_lan")
        and "async_connect" in record.getMessage()
    ]


def _ha_mqtt_publish_failure(message: str):
    # Each test uses its own exception message since the logging 'timeout' trap
    # is global and would otherwise mute the same warning across tests.
    async def _async_publish(*args, **kwargs):
        raise Exception(message)

    return _async_publish


async def test_async_connect_no_warning_when_other_transport_succeeds(
    request,
    hass: "HomeAssistant",
    hamqtt_mock: helpers.HAMQTTMocker,
    caplog: pytest.LogCaptureFixture,
):
    """
    Device.async_connect tries all of the transports concurrently: a transport failure
    must not be logged as a warning when another transport successfully connects.
    """
    async with (
        helpers.MQTTHubEntryMocker(request, hass),
        helpers.DeviceContext(
            request,
            hass,
            mc.TYPE_MSS310,
            data={mlc.CONF_PROTOCOL: Transport.AUTO.value},
        ) as device_context,
    ):
        hamqtt_mock.async_publish_mock.side_effect = _ha_mqtt_publish_failure(
            "mqtt publish failure (http ok)"
        )
        device = await device_context.perform_coldstart()
        assert device.mqtt and device.http
        assert device.http.is_connected
        assert not device.mqtt.is_connected
        assert not _async_connect_warnings(caplog)


async def test_async_connect_warnings_when_all_transports_fail(
    request,
    hass: "HomeAssistant",
    hamqtt_mock: helpers.HAMQTTMocker,
    caplog: pytest.LogCaptureFixture,
):
    """
    When no transport is able to connect, every failure is still logged as a warning.
    """
    async with (
        helpers.MQTTHubEntryMocker(request, hass),
        helpers.DeviceContext(
            request,
            hass,
            mc.TYPE_MSS310,
            data={mlc.CONF_PROTOCOL: Transport.AUTO.value},
        ) as device_context,
    ):
        hamqtt_mock.async_publish_mock.side_effect = _ha_mqtt_publish_failure(
            "mqtt publish failure (http failing)"
        )
        aioclient_mock = device_context.aioclient_mock
        aioclient_mock.clear_requests()
        aioclient_mock.post(
            f"http://{device_context.emulator_context.host}/config",
            exc=aiohttp.ClientConnectionError("http failure"),
        )
        await device_context.async_setup()
        device = device_context.device
        await device_context.time_mock.async_tick(
            timedelta(seconds=mlc.PARAM_COLDSTARTPOLL_DELAY)
        )
        assert not device.is_connected
        warnings = _async_connect_warnings(caplog)
        assert {record.getMessage().split(":", 1)[0] for record in warnings} >= {
            "HTTP",
            "MQTT",
        }, [record.getMessage() for record in warnings]
        for record in warnings:
            caplog.records.remove(record)


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
