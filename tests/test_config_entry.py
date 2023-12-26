"""Test meross_lan config entry setup"""
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import STATE_OFF, STATE_ON, STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.meross_lan import MerossApi, const as mlc
from custom_components.meross_lan.light import MLDNDLightEntity
from custom_components.meross_lan.merossclient import const as mc
from emulator import generate_emulators

from tests import const as tc, helpers


# We can pass fixtures as defined in conftest.py to tell pytest to use the fixture
# for a given test. We can also leverage fixtures and mocks that are available in
# Home Assistant using the pytest_homeassistant_custom_component plugin.
# Assertions allow you to verify that the return value of whatever is on the left
# side of the assertion matches with the right side.
async def test_mqtthub_entry(hass: HomeAssistant, hamqtt_mock: helpers.HAMQTTMocker):
    """Test mqtt hub entry setup and unload."""
    async with helpers.MQTTHubEntryMocker(hass):
        api = hass.data[mlc.DOMAIN]
        assert isinstance(api, MerossApi)
        assert api._mqtt_connection and api._mqtt_connection.mqtt_is_subscribed

    # Unload the entry and verify that the data has not been removed
    # we actually never remove the MerossApi...
    assert type(hass.data[mlc.DOMAIN]) is MerossApi


async def test_mqtthub_entry_notready(hass: HomeAssistant):
    """Test ConfigEntryNotReady when API raises an exception during entry setup"""
    async with helpers.MQTTHubEntryMocker(
        hass, auto_setup=False
    ) as mqtthub_entry_mocker:
        await mqtthub_entry_mocker.async_setup()
        # In this case we are testing the condition where async_setup_entry raises
        # ConfigEntryNotReady since we don't have mqtt component in the test environment
        assert mqtthub_entry_mocker.state == ConfigEntryState.SETUP_RETRY


async def test_device_entry(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker):
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
        tc.EMULATOR_TRACES_PATH, tc.MOCK_DEVICE_UUID, tc.MOCK_KEY
    ):
        async with helpers.DeviceContext(hass, emulator, aioclient_mock) as context:
            await context.async_load_config_entry()

            assert (device := context.device)
            device_ability = emulator.descriptor.ability

            entity_dnd = None
            if mc.NS_APPLIANCE_SYSTEM_DNDMODE in device_ability:
                entity_dnd = device.entity_dnd
                assert isinstance(entity_dnd, MLDNDLightEntity)
                state = hass.states.get(entity_dnd.entity_id)
                assert state and state.state == STATE_UNAVAILABLE

            sensor_signal_strength = None
            if mc.NS_APPLIANCE_SYSTEM_RUNTIME in device_ability:
                sensor_signal_strength = device.sensor_signal_strength
                state = hass.states.get(sensor_signal_strength.entity_id)
                assert state and state.state == STATE_UNAVAILABLE

            await context.perform_coldstart()

            if entity_dnd:
                state = hass.states.get(entity_dnd.entity_id)
                assert state and state.state in (STATE_OFF, STATE_ON)

            if sensor_signal_strength:
                state = hass.states.get(sensor_signal_strength.entity_id)
                assert state and state.state.isdigit()


async def test_profile_entry(
    hass: HomeAssistant,
    cloudapi_mock: helpers.CloudApiMocker,
    merossmqtt_mock: helpers.MerossMQTTMocker,
):
    """
    Test a Meross cloud profile entry
    """
    async with helpers.ProfileEntryMocker(hass):
        assert MerossApi.profiles[tc.MOCK_PROFILE_ID] is not None
