"""Test meross_lan config entry setup"""

import asyncio
from typing import TYPE_CHECKING

from homeassistant.config_entries import ConfigEntryState

from custom_components.meross_lan import const as mlc
from custom_components.meross_lan.helpers.component_api import ComponentApi
from custom_components.meross_lan.merossclient.protocol import (
    namespaces as mn,
)

from tests import const as tc, helpers

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant


# We can pass fixtures as defined in conftest.py to tell pytest to use the fixture
# for a given test. We can also leverage fixtures and mocks that are available in
# Home Assistant using the pytest_homeassistant_custom_component plugin.
# Assertions allow you to verify that the return value of whatever is on the left
# side of the assertion matches with the right side.
async def test_mqtthub_entry(
    request, hass: "HomeAssistant", hamqtt_mock: helpers.HAMQTTMocker
):
    """Test mqtt hub entry setup and unload."""
    async with helpers.MQTTHubEntryMocker(request, hass):
        api = hass.data[mlc.DOMAIN]
        assert isinstance(api, ComponentApi)
        assert api._mqtt_connection and api._mqtt_connection.mqtt_is_subscribed

    # Unload the entry and verify that the data has not been removed
    # we actually never remove the ComponentApi...
    assert type(hass.data[mlc.DOMAIN]) is ComponentApi

    # try to fight subscribe/unsubscribe cooldowns
    await asyncio.sleep(1)


async def test_mqtthub_entry_notready(request, hass: "HomeAssistant"):
    """Test ConfigEntryNotReady when API raises an exception during entry setup"""
    async with helpers.MQTTHubEntryMocker(request, hass, auto_setup=False) as mqtthub:
        await mqtthub.async_setup()
        # In this case we are testing the condition where async_setup_entry raises
        # ConfigEntryNotReady since we don't have mqtt component in the test environment
        assert mqtthub.config_entry.state == ConfigEntryState.SETUP_RETRY
        mqtthub.assert_logs(
            1,
            message=(
                r"HAMQTTConnection\(############0:@0\): "
                r"HomeAssistantError\(mqtt_not_setup_cannot_subscribe\) "
                r"in async_connect"
            ),
        )


async def test_device_entry(request, hass: "HomeAssistant"):
    """
    Generic device setup testing:
    we'll try to configure and setup devices according to our
    diagnostic trace collected in emulator_traces
    The test just tries to setup the config entry and validate
    some common basic entities. Device behavior is emulated
    by communicating to MerossEmulator through the aioclient_mock
    i.e. we're testing something close to http connected devices
    """
    for emulator in helpers.build_emulators():
        async with helpers.DeviceContext(
            request, hass, emulator, auto_poll=True
        ) as context:

            descriptor = emulator.descriptor
            ability = descriptor.ability
            device = context.device

            # try to ensure some 'formal' consistency in ns configuration
            for handler in device.ns_handlers.values():
                assert (
                    handler.id in ability
                ), f"Namespace {handler.id} has no ability declared"
                assert (
                    (handler.id.payload_get is not mn.PayloadType.LIST_IDX_STRICT)
                    or handler.polling_request_payload  # if IDX_STRICT we need polling indexes in request
                    or handler.id is mn.Appliance_Config_DeviceCfg  # special case
                ), f"Incorrect config for {handler.id} namespace"


async def test_profile_entry(request, hass: "HomeAssistant"):
    """
    Test a Meross cloud profile entry
    """
    async with helpers.ProfileEntryMocker(request, hass) as profile_mock:
        assert profile_mock.api.profiles[tc.MOCK_PROFILE_ID] is not None
