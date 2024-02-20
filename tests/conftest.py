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
from unittest.mock import patch

import pytest
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from . import helpers

pytest_plugins = "pytest_homeassistant_custom_component"


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
    with (
        patch("homeassistant.components.persistent_notification.async_create"),
        patch("homeassistant.components.persistent_notification.async_dismiss"),
    ):
        yield


@pytest.fixture(name="disable_debug", autouse=True)
def disable_debug_fixture():
    """Disable development debug code so to test in a production env."""
    with (
        patch("custom_components.meross_lan.MEROSSDEBUG", None),
        patch("custom_components.meross_lan.meross_profile.MEROSSDEBUG", None),
        patch("custom_components.meross_lan.merossclient.MEROSSDEBUG", None),
        patch("custom_components.meross_lan.merossclient.httpclient.MEROSSDEBUG", None),
        patch("custom_components.meross_lan.merossclient.mqttclient.MEROSSDEBUG", None),
        patch("custom_components.meross_lan.merossclient.cloudapi.MEROSSDEBUG", None),
    ):
        yield


@pytest.fixture()
def disable_entity_registry_update():
    """This fixture comes at hand when we want to disable the 'automatic entity
    disable feature provided by our GarageDoor code. It would be too difficult
    in our tests to cover this scenario so we totally disable calling into
    the entity registry."""
    from homeassistant.helpers.entity_registry import EntityRegistry

    with patch.object(EntityRegistry, "async_update_entity"):
        yield


@pytest.fixture()
def cloudapi_mock(aioclient_mock: AiohttpClientMocker):
    with helpers.CloudApiMocker(aioclient_mock) as _cloudapi_mock:
        yield _cloudapi_mock


@pytest.fixture()
async def hamqtt_mock(mqtt_mock):
    async with helpers.HAMQTTMocker() as _hamqtt_mock:
        yield _hamqtt_mock


@pytest.fixture()
def merossmqtt_mock(hass):
    with helpers.MerossMQTTMocker(hass) as _merossmqtt_mock:
        yield _merossmqtt_mock


@pytest.fixture()
def time_mock(hass):
    with helpers.TimeMocker(hass) as _time_mock:
        yield _time_mock
