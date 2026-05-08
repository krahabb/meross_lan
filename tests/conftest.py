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
from pytest_homeassistant_custom_component.test_util.aiohttp import (
    AiohttpClientMocker,
    mock_aiohttp_client,
)

from . import helpers

pytest_plugins = "pytest_homeassistant_custom_component"


# Test initialization must ensure custom_components are enabled
# but we can't autouse a simple fixture for that since the recorder
# need to be initialized first
@pytest.fixture(autouse=True)
def auto_enable(request: pytest.FixtureRequest, disable_debug):
    """
    Special initialization fixture managing recorder mocking.
    For some tests we need a working recorder but recorder_mock
    needs to be init before hass.
    """

    if "hass" in request.fixturenames:
        request.getfixturevalue("capsys")
        request.getfixturevalue("caplog")
        has_recorder = "recorder_mock" in request.fixturenames
        if has_recorder:
            request.getfixturevalue("recorder_mock")
        hass = request.getfixturevalue("hass")
        hass.data.pop("custom_components")
    yield


# This fixture is used to prevent HomeAssistant from attempting to create and dismiss persistent
# notifications. These calls would fail without this fixture since the persistent_notification
# integration is never loaded during a test.
@pytest.fixture(autouse=True)
def skip_notifications():
    """Skip notification calls."""
    with (
        patch("homeassistant.components.persistent_notification.async_create"),
        patch("homeassistant.components.persistent_notification.async_dismiss"),
    ):
        yield


@pytest.fixture()
def disable_debug():
    """Disable development debug code so to test in a production env."""
    with (
        patch("custom_components.meross_lan.merossclient.MEROSSDEBUG", None),
        patch("custom_components.meross_lan.merossclient.cloudapi.MEROSSDEBUG", None),
        patch(
            "custom_components.meross_lan.merossclient.client.http.MEROSSDEBUG",
            None,
        ),
        patch(
            "custom_components.meross_lan.merossclient.client.mqtt.MEROSSDEBUG",
            None,
        ),
    ):
        yield


@pytest.fixture()
def disable_entity_registry_update():
    """This fixture comes at hand when we want to disable the 'automatic entity
    disable feature provided by our GarageDoor code. It would be too difficult
    in our tests to cover this scenario so we totally disable calling into
    the entity registry."""

    from custom_components.meross_lan.devices.garagedoor import (
        GarageEnableSwitch,
    )

    saved = GarageEnableSwitch._check_channel_enable
    GarageEnableSwitch._check_channel_enable = lambda self: None
    yield
    GarageEnableSwitch._check_channel_enable = saved


@pytest.fixture(autouse=True, scope="function")
def log_exception(request: "pytest.FixtureRequest", capsys: "pytest.CaptureFixture"):
    """Intercepts any code managed exception sent to logging."""

    with helpers.LoggableMocker() as loggable_mock:
        yield loggable_mock
        calls = loggable_mock._mock.mock_calls
        if calls:
            with capsys.disabled():
                print(f"\n{request.node.name}: Loggable.log_exception calls:")
                print(*calls, sep="\n")


@pytest.fixture(autouse=True, scope="session")
def config_entry_manager():
    """Intercepts any code managed exception sent to logging."""

    with patch(
        "custom_components.meross_lan.helpers.manager.ConfigEntryManager",
        new=helpers.ConfigEntryMocker.ManagerMock,
    ):
        yield


@pytest.fixture(autouse=True, scope="session")
def config_entry_device():
    """Allows a 'global' mock of Device class. Mock layout is defined in helpers.DeviceContext.ManagerMock."""

    import unittest.mock

    with patch(
        "custom_components.meross_lan.helpers.device.Device",
        new=helpers.DeviceContext.ManagerMock,
    ):
        yield


@pytest.fixture()
def aioclient_mock(hass):
    """This fixture overrides the test library 'aioclient_mock' in order to
    (also) add patching for the dedicated aiohttp.ClientSession in our http client
    because the default HA api is not able to provide the needed features."""

    with mock_aiohttp_client() as aioclient_mocker:

        from custom_components.meross_lan.merossclient.client.http import (
            HttpClient,
        )

        def create_session():
            if not HttpClient._SESSION:
                HttpClient._SESSION = aioclient_mocker.create_session(hass.loop)
            return HttpClient._SESSION

        with patch(
            "custom_components.meross_lan.merossclient.client.http.HttpClient._get_or_create_client_session",
            side_effect=create_session,
        ):
            yield aioclient_mocker


@pytest.fixture()
def cloudapi_mock(aioclient_mock: AiohttpClientMocker):
    with helpers.CloudApiMocker(aioclient_mock) as _cloudapi_mock:
        yield _cloudapi_mock


@pytest.fixture()
async def hamqtt_mock(hass, mqtt_mock):
    async with helpers.HAMQTTMocker(hass) as _hamqtt_mock:
        yield _hamqtt_mock


@pytest.fixture()
def merossmqtt_mock(hass):
    with helpers.MerossMQTTMocker(hass) as _merossmqtt_mock:
        yield _merossmqtt_mock


@pytest.fixture()
def time_mock(hass):
    with helpers.TimeMocker(hass) as _time_mock:
        yield _time_mock
