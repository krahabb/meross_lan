"""Test meross_lan config entry setup"""

from homeassistant.config_entries import ConfigEntryState
from homeassistant import const as hac
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.meross_lan import MerossApi, const as mlc
from custom_components.meross_lan.light import MLDNDLightEntity
from custom_components.meross_lan.switch import MLSwitch
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
        assert mqtthub_entry_mocker.config_entry.state == ConfigEntryState.SETUP_RETRY


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
            assert await context.async_setup()

            device = context.device
            device_ability = emulator.descriptor.ability

            switches = device.managed_entities(MLSwitch.PLATFORM)
            for switch in switches:
                assert isinstance(switch, MLSwitch)
                state = hass.states.get(switch.entity_id)
                assert state and state.state == hac.STATE_UNAVAILABLE

            entity_dnd = None
            if mc.NS_APPLIANCE_SYSTEM_DNDMODE in device_ability:
                entity_dnd = device.entity_dnd
                assert isinstance(entity_dnd, MLDNDLightEntity)
                state = hass.states.get(entity_dnd.entity_id)
                assert state and state.state == hac.STATE_UNAVAILABLE

            sensor_signal_strength = None
            if mc.NS_APPLIANCE_SYSTEM_RUNTIME in device_ability:
                sensor_signal_strength = device.sensor_signal_strength
                state = hass.states.get(sensor_signal_strength.entity_id)
                assert state and state.state == hac.STATE_UNAVAILABLE

            await context.perform_coldstart()

            call_service = hass.services.async_call
            for switch in switches:
                if not switch.available:
                    # skip entities which are not avialble in emulator (warning though)
                    continue
                if switch._hass_connected:
                    await call_service(
                        MLSwitch.PLATFORM,
                        hac.SERVICE_TURN_ON,
                        service_data={
                            "entity_id": switch.entity_id,
                        },
                        blocking=True,
                    )
                    state = hass.states.get(switch.entity_id)
                    assert state and state.state == hac.STATE_ON
                    await call_service(
                        MLSwitch.PLATFORM,
                        hac.SERVICE_TURN_OFF,
                        service_data={
                            "entity_id": switch.entity_id,
                        },
                        blocking=True,
                    )
                    state = hass.states.get(switch.entity_id)
                    assert state and state.state == hac.STATE_OFF
                else:
                    # entity not loaded in HA so we just test
                    # the Meross internal interface
                    assert isinstance(switch, MLSwitch)
                    await switch.async_turn_on()
                    assert switch.is_on
                    await switch.async_turn_off()
                    assert not switch.is_on

            if entity_dnd:
                state = hass.states.get(entity_dnd.entity_id)
                assert state and state.state in (hac.STATE_OFF, hac.STATE_ON)

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
