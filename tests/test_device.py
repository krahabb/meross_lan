"""Test various Device apis both from merossclient.device.Device and from meross_lan.helpers.device.Device."""

import logging

from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
import pytest

from custom_components.meross_lan import const as mlc
from custom_components.meross_lan.merossclient import async_available_timezones
from custom_components.meross_lan.merossclient.client import Transport
from custom_components.meross_lan.merossclient.protocol import const as mc

from tests import const as tc, helpers

# Device configured with 'auto' transport and not bound to any configured
# Meross cloud profile (the emulator userId doesn't match any loaded profile)
_AUTO_TRANSPORT_DATA = {mlc.CONF_PROTOCOL: Transport.AUTO.value}


async def test_auto_transport_without_ha_mqtt(
    request, hass: "HomeAssistant", caplog: pytest.LogCaptureFixture
):
    """
    When the device is not bound to a (loaded) Meross cloud profile and the
    HA MQTT integration/hub is not available, the device must not be linked to
    the local HA MQTT broker (ComponentApi) since that would install a non-working
    MQTT transport failing with 'mqtt_not_setup_cannot_publish'.
    """
    async with helpers.DeviceContext(
        request, hass, mc.TYPE_MSS310, data=_AUTO_TRANSPORT_DATA, auto_poll=True
    ) as device_context:
        device = device_context.device
        assert device.profile is None
        assert device.mqtt is None
        assert device.http and device.http.is_connected
        assert device.preferred_transport is Transport.HTTP
        assert device.transport is Transport.HTTP

        await device.async_poll_full()

        for record in caplog.records:
            if record.levelno >= logging.WARNING:
                assert "mqtt_not_setup" not in record.getMessage()
                assert "Preferred transport" not in record.getMessage()


async def test_auto_transport_with_ha_mqtt_hub(
    request, hass: "HomeAssistant", hamqtt_mock: helpers.HAMQTTMocker
):
    """
    When the HA MQTT hub is configured, a device not bound to a Meross cloud profile
    is still linked to the local HA MQTT broker (unchanged behavior).
    """

    async def _publish_failure(*args, **kwargs):
        # no device is listening on the mocked HA broker: fail fast instead of
        # leaving the MQTT request pending (time is frozen in DeviceContext)
        raise Exception("no broker")

    hamqtt_mock.async_publish_mock.side_effect = _publish_failure
    async with (
        helpers.MQTTHubEntryMocker(request, hass) as hub_context,
        helpers.DeviceContext(
            request, hass, mc.TYPE_MSS310, data=_AUTO_TRANSPORT_DATA, auto_poll=True
        ) as device_context,
    ):
        api = hub_context.api
        device = device_context.device
        assert api.mqtt_connection.mqtt_is_subscribed
        assert device.profile is api
        assert device.mqtt and device.mqtt.connection is api.mqtt_connection


async def test_auto_transport_ha_mqtt_hub_setup_after_device(
    request, hass: "HomeAssistant", hamqtt_mock: helpers.HAMQTTMocker
):
    """
    When the HA MQTT hub entry is set up after the device, the device gets
    linked to the local HA MQTT broker as soon as the hub is available.
    """
    async with helpers.DeviceContext(
        request, hass, mc.TYPE_MSS310, data=_AUTO_TRANSPORT_DATA, auto_poll=True
    ) as device_context:
        device = device_context.device
        api = device_context.api
        assert not api.mqtt_connection.mqtt_is_subscribed
        assert device.profile is None
        assert device.mqtt is None

        async with helpers.MQTTHubEntryMocker(request, hass):
            assert api.mqtt_connection.mqtt_is_subscribed
            assert device.profile is api
            assert device.mqtt and device.mqtt.connection is api.mqtt_connection


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
