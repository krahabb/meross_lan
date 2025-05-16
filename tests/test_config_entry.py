"""Test meross_lan config entry setup"""

import asyncio
import typing

from homeassistant import const as hac
from homeassistant.config_entries import ConfigEntryState
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.meross_lan import MerossApi, const as mlc
from custom_components.meross_lan.light import MLDNDLightEntity
from custom_components.meross_lan.merossclient import const as mc, namespaces as mn
from emulator import generate_emulators

from tests import const as tc, helpers

if typing.TYPE_CHECKING:
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
        assert isinstance(api, MerossApi)
        assert api._mqtt_connection and api._mqtt_connection.mqtt_is_subscribed

    # Unload the entry and verify that the data has not been removed
    # we actually never remove the MerossApi...
    assert type(hass.data[mlc.DOMAIN]) is MerossApi

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
                r"HomeAssistantError\(Cannot subscribe to topic '/appliance/\+/publish', "
                r"make sure MQTT is set up correctly\) in async_mqtt_subscribe"
            ),
        )


async def test_device_entry(
    request, hass: "HomeAssistant", aioclient_mock: AiohttpClientMocker
):
    """
    Generic device setup testing:
    we'll try to configure and setup devices according to our
    diagnostic trace collected in emulator_traces
    The test just tries to setup the config entry and validate
    some common basic entities. Device behavior is emulated
    by communicating to MerossEmulator through the aioclient_mock
    i.e. we're testing something close to http connected devices
    """
    for emulator in generate_emulators(
        tc.EMULATOR_TRACES_PATH, key=tc.MOCK_KEY, uuid=tc.MOCK_DEVICE_UUID
    ):
        async with helpers.DeviceContext(
            request, hass, emulator, aioclient_mock
        ) as context:
            assert await context.async_setup()

            descriptor = emulator.descriptor
            ability = descriptor.ability
            device = context.device

            entity_dnd = None
            if mn.Appliance_System_DNDMode.name in ability:
                entity_dnd = device.entities[mlc.DND_ID]
                assert isinstance(entity_dnd, MLDNDLightEntity)
                state = hass.states.get(entity_dnd.entity_id)
                assert state and state.state == hac.STATE_UNAVAILABLE

            sensor_signal_strength = None
            if mn.Appliance_System_Runtime.name in ability:
                sensor_signal_strength = device.entities[mlc.SIGNALSTRENGTH_ID]
                state = hass.states.get(sensor_signal_strength.entity_id)
                assert state and state.state == hac.STATE_UNAVAILABLE

            await context.perform_coldstart()

            # try to ensure some 'formal' consistency in ns configuration
            for namespace_handler in device.namespace_handlers.values():
                ns = namespace_handler.ns
                assert (
                    (ns.request_payload_type is not mn.RequestPayloadType.LIST_C)
                    or ns.is_sensor
                    or namespace_handler.polling_request_channels
                    or descriptor.type.startswith(mc.TYPE_EM06)  # brutal exception
                ), f"Incorrect config for {ns.name} namespace"

            if entity_dnd:
                state = hass.states.get(entity_dnd.entity_id)
                assert state and state.state in (hac.STATE_OFF, hac.STATE_ON)

            if sensor_signal_strength:
                state = hass.states.get(sensor_signal_strength.entity_id)
                assert state and state.state.isdigit()


async def test_profile_entry(
    request,
    hass: "HomeAssistant",
    cloudapi_mock: helpers.CloudApiMocker,
    merossmqtt_mock: helpers.MerossMQTTMocker,
):
    """
    Test a Meross cloud profile entry
    """
    async with helpers.ProfileEntryMocker(request, hass):
        assert MerossApi.profiles[tc.MOCK_PROFILE_ID] is not None
