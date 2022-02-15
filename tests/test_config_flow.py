"""Test integration_blueprint config flow."""
from unittest.mock import patch

from homeassistant import config_entries, data_entry_flow
from homeassistant.components import dhcp
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.meross_lan.const import (
    CONF_HOST,
    CONF_KEY,
    DOMAIN,
    CONF_DEVICE_ID,
    CONF_PAYLOAD
)

from .const import (
    MOCK_DEVICE_IP,
    MOCK_HUB_CONFIG,
    MOCK_DEVICE_CONFIG,
    MOCK_KEY
)


# This fixture bypasses the actual setup of the integration
# since we only want to test the config flow. We test the
# actual functionality of the integration in other test modules.
@pytest.fixture(autouse=True)
def bypass_setup_fixture():
    """Prevent setup."""
    with patch(
        "custom_components.meross_lan.async_setup",
        return_value=True,
    ), patch(
        "custom_components.meross_lan.async_setup_entry",
        return_value=True,
    ):
        yield

@pytest.fixture
def fixture_mqtt_is_loaded():
    with patch(
        "custom_components.meross_lan.config_flow.mqtt_is_loaded",
        return_value = True
    ):
        yield

@pytest.fixture
def fixture_mqtt_is_not_loaded():
    with patch(
        "custom_components.meross_lan.config_flow.mqtt_is_loaded",
        return_value = False
    ):
        yield

# Here we simulate a successful config flow from the backend.
# Note that we use the `bypass_get_data` fixture here because
# we want the config flow validation to succeed during the test.
async def test_user_config_flow_mqtt(hass, fixture_mqtt_is_loaded):

    #test initial user config-flow (MQTT Hub)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )

    # Check that the config flow shows the user form as the first step
    assert result["type"] == data_entry_flow.RESULT_TYPE_FORM
    assert result["step_id"] == "hub"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input=MOCK_HUB_CONFIG
    )

    # Check that the config flow is complete and a new entry is created with
    # the input data
    assert result["type"] == data_entry_flow.RESULT_TYPE_CREATE_ENTRY
    #assert result["title"] == "MQTT Hub"
    assert result["data"] == MOCK_HUB_CONFIG
    assert result["result"]

    # Now, a new configentry would add a device manually
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )

    # Check that the config flow shows the user form as the first step
    assert result["type"] == data_entry_flow.RESULT_TYPE_FORM
    assert result["step_id"] == "device"


async def test_user_config_flow_no_mqtt(hass, fixture_mqtt_is_not_loaded):
    #test user config-flow when no mqtt available

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )

    # Check that the config flow shows the user form as the first step
    assert result["type"] == data_entry_flow.RESULT_TYPE_FORM
    assert result["step_id"] == "device"


async def test_mqttdiscovery_config_flow(hass, fixture_mqtt_is_loaded):
    #test discovery config flow

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context = {"source": config_entries.SOURCE_DISCOVERY}, data = MOCK_DEVICE_CONFIG
    )

    assert result["type"] == data_entry_flow.RESULT_TYPE_FORM
    assert result["step_id"] == "finalize"


    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input={}
    )

    assert result["type"] == data_entry_flow.RESULT_TYPE_CREATE_ENTRY
    assert result["data"] == MOCK_DEVICE_CONFIG


async def test_dhcpdiscovery_config_flow(hass):
    #test discovery config flow

    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context = {"source": config_entries.SOURCE_DHCP},
        data = dhcp.DhcpServiceInfo(
            ip=MOCK_DEVICE_IP,
            macaddress='48E1E9000000',
            hostname='fakehost'
        )
    )

    assert result["type"] == data_entry_flow.RESULT_TYPE_FORM
    assert result["step_id"] == "device"

    """
    for this to work we'd need to mock a bit of aioclient
    since the flow is querying the device over HTTP
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        user_input={
            CONF_HOST: MOCK_DEVICE_IP,
            CONF_KEY: MOCK_KEY
        }
    )

    assert result["type"] == data_entry_flow.RESULT_TYPE_CREATE_ENTRY
    assert result["data"] == MOCK_DEVICE_CONFIG
    """

"""
# In this case, we want to simulate a failure during the config flow.
# We use the `error_on_get_data` mock instead of `bypass_get_data`
# (note the function parameters) to raise an Exception during
# validation of the input config.
async def test_failed_config_flow(hass, error_on_get_data):

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )

    assert result["type"] == data_entry_flow.RESULT_TYPE_FORM
    assert result["step_id"] == "user"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input=MOCK_CONFIG
    )

    assert result["type"] == data_entry_flow.RESULT_TYPE_FORM
    assert result["errors"] == {"base": "auth"}
"""

"""
# Our config flow also has an options flow, so we must test it as well.
async def test_options_flow(hass):

    # Create a new MockConfigEntry and add to HASS (we're bypassing config
    # flow entirely)
    entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CONFIG, entry_id="test")
    entry.add_to_hass(hass)

    # Initialize an options flow
    result = await hass.config_entries.options.async_init(entry.entry_id)

    # Verify that the first options step is a user form
    assert result["type"] == data_entry_flow.RESULT_TYPE_FORM
    assert result["step_id"] == "user"

    # Enter some fake data into the form
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        user_input={platform: platform != SENSOR for platform in PLATFORMS},
    )

    # Verify that the flow finishes
    assert result["type"] == data_entry_flow.RESULT_TYPE_CREATE_ENTRY
    assert result["title"] == "test_username"

    # Verify that the options were updated
    assert entry.options == {BINARY_SENSOR: True, SENSOR: False, SWITCH: True}
"""