"""Test the merossclient module (low level device/cloud api)"""

from homeassistant.helpers.aiohttp_client import async_get_clientsession

from custom_components.meross_lan.merossclient import (
    cloudapi,
    const as mc,
    namespaces as mn,
)

from . import const as tc, helpers


def test_merossclient_module():
    """
    Test utilities defined in merossclient package/module
    """
    for mc_symbol in dir(mc):
        if mc_symbol.startswith("NS_"):
            namespace = mn.NAMESPACES[getattr(mc, mc_symbol)]
            _is_hub_namespace = namespace.is_hub
            if mc_symbol.startswith("NS_APPLIANCE_HUB_"):
                assert _is_hub_namespace
            else:
                assert not _is_hub_namespace

            _is_thermostat_namespace = namespace.is_thermostat
            if mc_symbol.startswith("NS_APPLIANCE_CONTROL_THERMOSTAT_"):
                assert _is_thermostat_namespace
            else:
                assert not _is_thermostat_namespace


async def test_cloudapi(hass, cloudapi_mock: helpers.CloudApiMocker):
    cloudapiclient = cloudapi.CloudApiClient(session=async_get_clientsession(hass))
    credentials = await cloudapiclient.async_signin(
        tc.MOCK_PROFILE_EMAIL, tc.MOCK_PROFILE_PASSWORD
    )
    assert credentials == tc.MOCK_PROFILE_CREDENTIALS_SIGNIN

    result = await cloudapiclient.async_device_devlist()
    assert result == tc.MOCK_CLOUDAPI_DEVICE_DEVLIST

    result = await cloudapiclient.async_device_latestversion()
    assert result == tc.MOCK_CLOUDAPI_DEVICE_LATESTVERSION

    result = await cloudapiclient.async_hub_getsubdevices(tc.MOCK_PROFILE_MSH300_UUID)
    assert result == tc.MOCK_CLOUDAPI_HUB_GETSUBDEVICES[tc.MOCK_PROFILE_MSH300_UUID]

    await cloudapiclient.async_logout()
