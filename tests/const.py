"""Constants for integration_blueprint tests."""
from custom_components.meross_lan.const import (
    CONF_DEVICE_ID, CONF_KEY, CONF_PAYLOAD
)

# Mock config data to be used across multiple tests
MOCK_HUB_CONFIG = {
    CONF_KEY: "test_key"
    }
MOCK_DEVICE_CONFIG = {
    CONF_DEVICE_ID: "9109182170548290880048b1a9522933",
    CONF_KEY: "test_key",
    CONF_PAYLOAD: {}
    }
