"""Test meross_lan config entry setup"""
from typing import Any
from unittest.mock import ANY

from freezegun.api import freeze_time
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)
from pytest_homeassistant_custom_component.test_util.aiohttp import (
    AiohttpClientMocker,
    AiohttpClientMockResponse,
)

from custom_components.meross_lan import MerossApi
from custom_components.meross_lan.const import (
    CONF_DEVICE_ID,
    CONF_HOST,
    CONF_KEY,
    CONF_PAYLOAD,
    CONF_POLLING_PERIOD,
    DOMAIN,
)
from custom_components.meross_lan.emulator.emulator import MerossEmulator
from custom_components.meross_lan.merossclient import const as mc
from custom_components.meross_lan.sensor import RuntimeMixin

from .conftest import MQTTMock
from .const import MOCK_DEVICE_IP, MOCK_HUB_CONFIG, MOCK_POLLING_PERIOD
from .helpers import generate_emulators


# We can pass fixtures as defined in conftest.py to tell pytest to use the fixture
# for a given test. We can also leverage fixtures and mocks that are available in
# Home Assistant using the pytest_homeassistant_custom_component plugin.
# Assertions allow you to verify that the return value of whatever is on the left
# side of the assertion matches with the right side.
async def test_mqtthub_entry(hass: HomeAssistant, mqtt_available: MQTTMock):
    """Test entry setup and unload."""
    # Create a mock entry so we don't have to go through config flow
    config_entry = MockConfigEntry(domain=DOMAIN, data=MOCK_HUB_CONFIG)
    config_entry.add_to_hass(hass)

    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    assert type(hass.data[DOMAIN]) == MerossApi
    mqtt_available.async_subscribe.assert_called_once_with(
        hass, mc.TOPIC_DISCOVERY, ANY)

    # Unload the entry and verify that the data has not been removed
    # we actually never remove the MerossApi...
    assert await hass.config_entries.async_unload(config_entry.entry_id)
    assert type(hass.data[DOMAIN]) == MerossApi


async def test_mqtthub_entry_notready(hass: HomeAssistant):
    # Test ConfigEntryNotReady when API raises an exception during entry setup.
    config_entry = MockConfigEntry(domain=DOMAIN, data=MOCK_HUB_CONFIG)
    config_entry.add_to_hass(hass)
    # In this case we are testing the condition where async_setup_entry raises
    # ConfigEntryNotReady since we don't have mqtt component in the test environment
    await hass.config_entries.async_setup(config_entry.entry_id)
    assert config_entry.state == ConfigEntryState.SETUP_RETRY
    # with pytest.raises(ConfigEntryNotReady):
    #    assert await hass.config_entries.async_setup(config_entry.entry_id)


async def test_device_entry(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, mqtt_available
):
    """
    Generic device setup testing:
    we'll try to configure and setup devices according to our
    diagnostic trace collected in meross_lan/traces/emulator
    The test just tries to setup the config entry and validate
    some common basic entities
    """
    emulator: MerossEmulator
    frozen_time: Any

    async def _handle_http_request(method, url, data):
        # simulate time passes else the device will fall offline
        frozen_time.tick(delta=0.1)
        response = emulator.handle(data)
        frozen_time.tick(delta=0.1)
        return AiohttpClientMockResponse(method, url, json=response)

    aioclient_mock.post(
        f"http://{MOCK_DEVICE_IP}/config",
        side_effect=_handle_http_request,
    )

    for emulator in generate_emulators():

        device_uuid = emulator.descriptor.uuid
        device_ability = emulator.descriptor.ability
        entry_data = {
            CONF_DEVICE_ID: device_uuid,
            CONF_HOST: MOCK_DEVICE_IP,
            CONF_KEY: emulator.key,
            CONF_PAYLOAD: {
                mc.KEY_ALL: emulator.descriptor.all,
                mc.KEY_ABILITY: device_ability,
            },
            CONF_POLLING_PERIOD: MOCK_POLLING_PERIOD,
        }
        config_entry = MockConfigEntry(
            domain=DOMAIN,
            data=entry_data,
            unique_id=emulator.descriptor.uuid,
            version=1,
        )
        config_entry.add_to_hass(hass)

        with freeze_time() as frozen_time:

            assert await hass.config_entries.async_setup(config_entry.entry_id)
            await hass.async_block_till_done()

            api: MerossApi = hass.data[DOMAIN]
            device = api.devices[device_uuid]

            dndstate = None
            if mc.NS_APPLIANCE_SYSTEM_DNDMODE in device_ability:
                dndstate = hass.states.get(device.entity_dnd.entity_id)  # type: ignore
                assert dndstate
                assert dndstate.state == "unavailable"

            runtimestate = None
            if mc.NS_APPLIANCE_SYSTEM_RUNTIME in device_ability:
                assert isinstance(device, RuntimeMixin)
                runtimestate = hass.states.get(device._sensor_runtime.entity_id)
                assert runtimestate
                assert runtimestate.state == "unavailable"

            # trigger the dalayed calls so the http client starts polling
            async_fire_time_changed(hass)
            await hass.async_block_till_done()

            # check status after initial polling
            assert device.online
            frozen_time.tick(delta=MOCK_POLLING_PERIOD)
            async_fire_time_changed(hass)
            await hass.async_block_till_done()

            if dndstate is not None:
                dndstate = hass.states.get(device.entity_dnd.entity_id)  # type: ignore
                assert dndstate.state in ("on", "off")  # type: ignore

            if runtimestate is not None:
                runtimestate = hass.states.get(device._sensor_runtime.entity_id)  # type: ignore
                assert runtimestate.state.isdigit()  # type: ignore

            assert await hass.config_entries.async_unload(config_entry.entry_id)
            await hass.async_block_till_done()

            assert device_uuid not in api.devices
