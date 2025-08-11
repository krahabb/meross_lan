"""Test for meross_lan.request service calls"""

import typing
from unittest.mock import ANY

from custom_components.meross_lan import const as mlc
from custom_components.meross_lan.merossclient import json_dumps
from custom_components.meross_lan.merossclient.protocol import (
    const as mc,
    namespaces as mn,
)

from tests import const as tc, helpers

if typing.TYPE_CHECKING:
    from homeassistant.core import HomeAssistant


async def test_request_on_mqtt(
    request, hass: "HomeAssistant", hamqtt_mock: helpers.HAMQTTMocker
):
    """
    Test service call routed through mqtt without being forwarded to
    Device. This happens when we want to send request to
    devices not registered in HA
    """
    async with helpers.MQTTHubEntryMocker(request, hass):
        await hass.services.async_call(
            mlc.DOMAIN,
            mlc.SERVICE_REQUEST,
            service_data={
                mlc.CONF_DEVICE_ID: tc.MOCK_DEVICE_UUID,
                mc.KEY_NAMESPACE: mn.Appliance_System_All.name,
                mc.KEY_METHOD: mc.METHOD_GET,
            },
            blocking=True,
        )
        # this call, with no devices registered in configuration
        # will just try to publish on mqtt so we'll check the mock
        hamqtt_mock.async_publish_mock.assert_called_once_with(
            hass, mc.TOPIC_REQUEST.format(tc.MOCK_DEVICE_UUID), ANY
        )


async def test_request_on_device(
    request,
    hass: "HomeAssistant",
    hamqtt_mock: helpers.HAMQTTMocker,
):
    """
    Test service calls routed through a device
    """
    async with helpers.DeviceContext(request, hass, mc.TYPE_MSS310) as context:
        # let the device perform it's poll and come online
        await context.perform_coldstart()

        # get the actual state of the emulator
        digest = context.emulator.descriptor.digest
        initialstate = digest[mc.KEY_TOGGLEX][0][mc.KEY_ONOFF]
        # when routing the call through a device the service data 'key' is not used
        await hass.services.async_call(
            mlc.DOMAIN,
            mlc.SERVICE_REQUEST,
            service_data={
                mlc.CONF_DEVICE_ID: context.device_id,
                mc.KEY_NAMESPACE: mn.Appliance_Control_ToggleX.name,
                mc.KEY_METHOD: mc.METHOD_SET,
                mc.KEY_PAYLOAD: json_dumps(
                    {
                        mn.Appliance_Control_ToggleX.key: {
                            mc.KEY_CHANNEL: 0,
                            mc.KEY_ONOFF: 1 - initialstate,
                        }
                    }
                ),
            },
            blocking=True,
        )
        # the device api will spawn a task to execute the call on async_http_request
        await hass.async_block_till_done()

        assert initialstate == 1 - digest[mc.KEY_TOGGLEX][0][mc.KEY_ONOFF]

        # this call, should not be routed to mqtt since our device is
        # emulated in http
        hamqtt_mock.async_publish_mock.assert_not_called()


async def test_request_notification(
    request,
    hass: "HomeAssistant",
    hamqtt_mock: helpers.HAMQTTMocker,
):
    """
    Test service calls routed through a device
    """
    async with helpers.DeviceContext(request, hass, mc.TYPE_MSS310) as context:
        # let the device perform it's poll and come online
        await context.perform_coldstart()
        # when routing the call through a device the service data 'key' is not used
        await hass.services.async_call(
            mlc.DOMAIN,
            mlc.SERVICE_REQUEST,
            service_data={
                mlc.CONF_DEVICE_ID: context.device_id,
                mc.KEY_NAMESPACE: mn.Appliance_System_All.name,
                mlc.CONF_NOTIFYRESPONSE: True,
            },
            blocking=True,
        )
        # the device api will spawn a task to execute the call on async_http_request
        await hass.async_block_till_done()

        # we should check the notification has been created but
        # the test context patches (auto-fixture) the persistent_notification and
        # I'm right now lazy enough to start managing that

        # this call, should not be routed to mqtt since our device is
        # emulated in http
        hamqtt_mock.async_publish_mock.assert_not_called()
