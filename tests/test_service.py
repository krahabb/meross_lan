"""Test for meross_lan.request service calls"""

from unittest.mock import ANY

from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.meross_lan.const import CONF_DEVICE_ID, DOMAIN, SERVICE_REQUEST
from custom_components.meross_lan.merossclient import const as mc

from tests.conftest import MQTTMock
from tests.const import MOCK_DEVICE_UUID, MOCK_HUB_CONFIG


async def test_request(hass: HomeAssistant, mqtt_available: MQTTMock):

    config_entry = MockConfigEntry(domain=DOMAIN, data=MOCK_HUB_CONFIG)
    config_entry.add_to_hass(hass)

    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    await hass.services.async_call(
        DOMAIN,
        SERVICE_REQUEST,
        service_data={
            CONF_DEVICE_ID: MOCK_DEVICE_UUID,
            mc.KEY_NAMESPACE: mc.NS_APPLIANCE_SYSTEM_ALL,
            mc.KEY_METHOD: mc.METHOD_GET,
        },
        blocking=True,
    )
    # this call, with no devices registered in configuration
    # will just try to publish on mqtt so we'll check the mock
    mqtt_available.mqtt_publish.assert_called_once_with(
        hass, mc.TOPIC_REQUEST.format(MOCK_DEVICE_UUID), ANY)

