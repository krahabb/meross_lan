"""Test meross_lan config entry setup"""
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import (
    STATE_OFF,
    STATE_ON,
    STATE_UNAVAILABLE,
)
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.meross_lan import MerossApi
from custom_components.meross_lan.const import DOMAIN
from custom_components.meross_lan.light import MLDNDLightEntity
from custom_components.meross_lan.merossclient import const as mc
from custom_components.meross_lan.sensor import RuntimeMixin
from emulator import generate_emulators

from .conftest import MQTTMock
from .const import EMULATOR_TRACES_PATH, MOCK_DEVICE_UUID, MOCK_HUB_CONFIG, MOCK_KEY
from .helpers import devicecontext


# We can pass fixtures as defined in conftest.py to tell pytest to use the fixture
# for a given test. We can also leverage fixtures and mocks that are available in
# Home Assistant using the pytest_homeassistant_custom_component plugin.
# Assertions allow you to verify that the return value of whatever is on the left
# side of the assertion matches with the right side.
async def test_mqtthub_entry(hass: HomeAssistant, mqtt_patch: MQTTMock):
    """Test mqtt hub entry setup and unload."""
    config_entry = MockConfigEntry(domain=DOMAIN, data=MOCK_HUB_CONFIG)
    config_entry.add_to_hass(hass)

    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    api = hass.data[DOMAIN]
    assert isinstance(api, MerossApi)

    assert api.mqtt_is_subscribed()

    # Unload the entry and verify that the data has not been removed
    # we actually never remove the MerossApi...
    assert await hass.config_entries.async_unload(config_entry.entry_id)
    assert type(hass.data[DOMAIN]) == MerossApi


async def test_mqtthub_entry_notready(hass: HomeAssistant):
    """ Test ConfigEntryNotReady when API raises an exception during entry setup"""
    config_entry = MockConfigEntry(domain=DOMAIN, data=MOCK_HUB_CONFIG)
    config_entry.add_to_hass(hass)
    # In this case we are testing the condition where async_setup_entry raises
    # ConfigEntryNotReady since we don't have mqtt component in the test environment
    await hass.config_entries.async_setup(config_entry.entry_id)
    assert config_entry.state == ConfigEntryState.SETUP_RETRY
    # with pytest.raises(ConfigEntryNotReady):
    #    assert await hass.config_entries.async_setup(config_entry.entry_id)


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
        EMULATOR_TRACES_PATH, MOCK_DEVICE_UUID, MOCK_KEY
    ):

        async with devicecontext(emulator, hass, aioclient_mock) as context:

            assert (device := context.device)
            device_ability = emulator.descriptor.ability

            entity_dnd = None
            if mc.NS_APPLIANCE_SYSTEM_DNDMODE in device_ability:
                entity_dnd = device.entity_dnd
                assert isinstance(entity_dnd, MLDNDLightEntity)
                dndstate = hass.states.get(entity_dnd.entity_id)
                assert dndstate and dndstate.state == STATE_UNAVAILABLE

            sensor_runtime = None
            if mc.NS_APPLIANCE_SYSTEM_RUNTIME in device_ability:
                assert isinstance(device, RuntimeMixin)
                sensor_runtime = device._sensor_runtime
                runtimestate = hass.states.get(sensor_runtime.entity_id)
                assert runtimestate and runtimestate.state == STATE_UNAVAILABLE

            await context.perform_coldstart()

            if entity_dnd is not None:
                dndstate = hass.states.get(entity_dnd.entity_id)
                assert dndstate and dndstate.state in (STATE_OFF, STATE_ON)

            if sensor_runtime is not None:
                runtimestate = hass.states.get(sensor_runtime.entity_id)
                assert runtimestate and runtimestate.state.isdigit()
