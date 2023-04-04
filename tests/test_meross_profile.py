"""Test for meross cloud profiles"""
import datetime as dt
from unittest.mock import call

from homeassistant.helpers import device_registry
from pytest_homeassistant_custom_component.common import flush_store

from custom_components.meross_lan import const as mlc
from custom_components.meross_lan.meross_profile import MerossCloudProfile
from custom_components.meross_lan.merossclient import const as mc

from . import const as tc, helpers


async def test_meross_profile(
    hass,
    hass_storage,
    aioclient_mock,
    cloudapi_mock: helpers.CloudApiMocker,
    merossmqtt_mock: helpers.MerossMQTTMocker,
):
    """
    Tests basic MerossCloudProfile behavior:
    - loading
    - starting (with cloud device_info list update)
    - discovery setup
    - saving
    """
    hass_storage.update(tc.MOCK_PROFILE_STORAGE)

    async with helpers.devicecontext(
        helpers.build_emulator_for_profile(tc.MOCK_PROFILE_ID, model=mc.TYPE_MSS310),
        hass,
        aioclient_mock,
        config_data={
            mlc.CONF_PROFILE_ID: tc.MOCK_PROFILE_ID,
            mlc.CONF_PROTOCOL: mlc.CONF_PROTOCOL_AUTO,
        },
    ) as context:

        await context.async_load_config_entry()

        assert (api := context.api)
        assert (device := context.device)
        assert (profile := api.profiles.get(tc.MOCK_PROFILE_ID))

        await context.perform_coldstart()
        # check the cloud profile connected the mqtt server
        # in device startup
        mqttconnections = list(profile.mqttconnections.values())
        merossmqtt_mock.safe_connect_mock.assert_has_calls(
            [
                call(mqttconnections[0], tc.MOCK_PROFILE_MSS310_DOMAIN, 443),
            ],
            any_order=True,
        )
        # check the device registry has the device name from the cloud (stored)
        assert (
            device_registry_entry := device_registry.async_get(hass).async_get_device(
                **device.deviceentry_id
            )
        ) and device_registry_entry.name == tc.MOCK_PROFILE_MSS310_DEVNAME_STORED

        # now the profile should query the cloudapi and get an updated device_info list
        await context.async_tick(
            dt.timedelta(seconds=mlc.PARAM_CLOUDPROFILE_DELAYED_SETUP_TIMEOUT)
        )
        mqttconnections = list(profile.mqttconnections.values())
        merossmqtt_mock.safe_connect_mock.assert_has_calls(
            [
                call(mqttconnections[0], tc.MOCK_PROFILE_MSS310_DOMAIN, 443),
                call(mqttconnections[1], tc.MOCK_PROFILE_MSH300_DOMAIN, 443),
            ],
            any_order=True,
        )
        assert (
            device_registry_entry := device_registry.async_get(hass).async_get_device(
                **device.deviceentry_id
            )
        ) and device_registry_entry.name == tc.MOCK_PROFILE_MSS310_DEVNAME
        assert cloudapi_mock.devlist_calls == 1

        await flush_store(profile._store)

        expected_storage_device_info_data = {
            device_info[mc.KEY_UUID]: device_info
            for device_info in tc.MOCK_PROFILE_DEVLIST
        }
        profile_storage_data = hass_storage[tc.MOCK_PROFILE_STORE_KEY]["data"]
        assert (
            profile_storage_data[MerossCloudProfile.KEY_DEVICE_INFO]
            == expected_storage_device_info_data
        )

        await context.async_unload_config_entry()

        await profile.async_shutdown()

        merossmqtt_mock.safe_disconnect_mock.assert_has_calls(
            [call(mqttconnections[0]), call(mqttconnections[1])], any_order=True
        )

        assert tc.MOCK_PROFILE_ID not in api.profiles
