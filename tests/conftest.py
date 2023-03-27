"""Global fixtures for integration_blueprint integration."""
# Fixtures allow you to replace functions with a Mock object. You can perform
# many options via the Mock to reflect a particular behavior from the original
# function that you want to see without going through the function's actual logic.
# Fixtures can either be passed into tests as parameters, or if autouse=True, they
# will automatically be used across all tests.
#
# Fixtures that are defined in conftest.py are available across all tests. You can also
# define fixtures within a particular test file to scope them locally.
#
# pytest_homeassistant_custom_component provides some fixtures that are provided by
# Home Assistant core. You can find those fixture definitions here:
# https://github.com/MatthewFlamm/pytest-homeassistant-custom-component/blob/master/pytest_homeassistant_custom_component/common.py
#
# See here for more info: https://docs.pytest.org/en/latest/fixture.html (note that
# pytest includes fixtures OOB which you can use as defined on this page)
from typing import Any, Callable, Coroutine
from unittest.mock import MagicMock, Mock, patch

import pytest

pytest_plugins = "pytest_homeassistant_custom_component"

MqttMockPahoClient = MagicMock
"""MagicMock for `paho.mqtt.client.Client`"""
MqttMockHAClient = MagicMock
"""MagicMock for `homeassistant.components.mqtt.MQTT`."""
MqttMockHAClientGenerator = Callable[..., Coroutine[Any, Any, MqttMockHAClient]]

# Test initialization must ensure custom_components are enabled
# but we can't autouse a simple fixture for that since the recorder
# need to be initialized first
@pytest.fixture(autouse=True)
def auto_enable(request: pytest.FixtureRequest):
    if "recorder_mock" in request.fixturenames:
        request.getfixturevalue("recorder_mock")
    hass = request.getfixturevalue("hass")
    hass.data.pop("custom_components")
    yield


# This fixture is used to prevent HomeAssistant from attempting to create and dismiss persistent
# notifications. These calls would fail without this fixture since the persistent_notification
# integration is never loaded during a test.
@pytest.fixture(name="skip_notifications", autouse=True)
def skip_notifications_fixture():
    """Skip notification calls."""
    with patch("homeassistant.components.persistent_notification.async_create"), patch(
        "homeassistant.components.persistent_notification.async_dismiss"
    ):
        yield


@pytest.fixture(name="disable_debug", autouse=True)
def disable_debug_fixture():
    """Skip notification calls."""
    with patch("custom_components.meross_lan.MEROSSDEBUG", return_value=False), patch(
        "custom_components.meross_lan.meross_profile.MEROSSDEBUG", return_value=None
    ), patch(
        "custom_components.meross_lan.merossclient.MEROSSDEBUG", return_value=None
    ), patch(
        "custom_components.meross_lan.merossclient.httpclient.MEROSSDEBUG",
        return_value=None,
    ), patch(
        "custom_components.meross_lan.merossclient.cloudapi.MEROSSDEBUG",
        return_value=None,
    ):
        yield


class MQTTMock:
    mqtt_client: MqttMockHAClient
    mqtt_async_publish: Mock

    def async_publish(self, hass, topic: str, payload: str, *args, **kwargs):
        pass


@pytest.fixture()
async def mqtt_patch(mqtt_mock_entry_no_yaml_config: MqttMockHAClientGenerator):

    with patch("homeassistant.components.mqtt.async_publish") as mqtt_async_publish:
        context = MQTTMock()
        context.mqtt_client = await mqtt_mock_entry_no_yaml_config()
        context.mqtt_async_publish = mqtt_async_publish
        mqtt_async_publish.side_effect = context.async_publish
        yield context
