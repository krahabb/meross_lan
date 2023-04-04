"""Test for meross_lan.request service calls"""
import json
from unittest.mock import ANY

from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.meross_lan import const as mlc
from custom_components.meross_lan.merossclient import const as mc

from tests import const as tc, helpers


async def test_request_on_mqtt(hass: HomeAssistant, hamqtt_mock: helpers.HAMQTTMocker):
    """
    Test service call routed through mqtt without being forwarded to
    MerossDevice. This happens when we want to send request to
    devices not registered in HA
    """
    config_entry = MockConfigEntry(domain=mlc.DOMAIN, data=tc.MOCK_HUB_CONFIG)
    config_entry.add_to_hass(hass)

    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    await hass.services.async_call(
        mlc.DOMAIN,
        mlc.SERVICE_REQUEST,
        service_data={
            mlc.CONF_DEVICE_ID: tc.MOCK_DEVICE_UUID,
            mc.KEY_NAMESPACE: mc.NS_APPLIANCE_SYSTEM_ALL,
            mc.KEY_METHOD: mc.METHOD_GET,
        },
        blocking=True,
    )
    # this call, with no devices registered in configuration
    # will just try to publish on mqtt so we'll check the mock
    hamqtt_mock.mqtt_async_publish.assert_called_once_with(
        hass, mc.TOPIC_REQUEST.format(tc.MOCK_DEVICE_UUID), ANY
    )

    assert await hass.config_entries.async_unload(config_entry.entry_id)


async def test_request_on_device(
    hass: HomeAssistant, hamqtt_mock: helpers.HAMQTTMocker, aioclient_mock
):
    """
    Test service calls routed through a device
    """
    async with helpers.devicecontext(mc.TYPE_MSS310, hass, aioclient_mock) as context:

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
                mc.KEY_NAMESPACE: mc.NS_APPLIANCE_CONTROL_TOGGLEX,
                mc.KEY_METHOD: mc.METHOD_SET,
                mc.KEY_PAYLOAD: json.dumps(
                    {
                        mc.KEY_TOGGLEX: {
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
        hamqtt_mock.mqtt_async_publish.assert_not_called()


async def test_request_notification(
    hass: HomeAssistant, hamqtt_mock: helpers.HAMQTTMocker, aioclient_mock
):
    """
    Test service calls routed through a device
    """
    async with helpers.devicecontext(mc.TYPE_MSS310, hass, aioclient_mock) as context:

        # let the device perform it's poll and come online
        await context.perform_coldstart()

        # when routing the call through a device the service data 'key' is not used
        await hass.services.async_call(
            mlc.DOMAIN,
            mlc.SERVICE_REQUEST,
            service_data={
                mlc.CONF_DEVICE_ID: context.device_id,
                mc.KEY_NAMESPACE: mc.NS_APPLIANCE_SYSTEM_ALL,
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
        hamqtt_mock.mqtt_async_publish.assert_not_called()
