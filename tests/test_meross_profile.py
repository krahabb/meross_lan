"""Test for meross cloud profiles"""
from unittest.mock import call

from homeassistant.helpers.aiohttp_client import async_get_clientsession
from pytest_homeassistant_custom_component.common import flush_store

from custom_components.meross_lan import MerossApi, const as mlc
from custom_components.meross_lan.meross_profile import MerossCloudProfile
from custom_components.meross_lan.merossclient import cloudapi, const as mc

from . import const as tc, helpers


async def test_cloudapi(hass, cloudapi_mock: helpers.CloudApiMocker):
    clientsession = async_get_clientsession(hass)

    result = await cloudapi.async_cloudapi_login(
        tc.MOCK_PROFILE_EMAIL, tc.MOCK_PROFILE_PASSWORD, clientsession
    )
    assert result[mc.KEY_USERID_] == tc.MOCK_PROFILE_ID
    assert result[mc.KEY_EMAIL] == tc.MOCK_PROFILE_EMAIL
    assert result[mc.KEY_KEY] == tc.MOCK_PROFILE_KEY
    assert result[mc.KEY_TOKEN] == tc.MOCK_PROFILE_TOKEN

    token = result[mc.KEY_TOKEN]
    result = await cloudapi.async_cloudapi_device_devlist(token, clientsession)
    assert result == tc.MOCK_PROFILE_CLOUDAPI_DEVLIST

    result = await cloudapi.async_cloudapi_hub_getsubdevices(
        token, tc.MOCK_PROFILE_MSH300_UUID, clientsession
    )
    assert (
        result == tc.MOCK_PROFILE_CLOUDAPI_SUBDEVICE_DICT[tc.MOCK_PROFILE_MSH300_UUID]
    )

    result = await cloudapi.async_cloudapi_logout(token, clientsession)


async def test_meross_profile(
    hass,
    hass_storage,
    aioclient_mock,
    cloudapi_mock: helpers.CloudApiMocker,
    merossmqtt_mock: helpers.MerossMQTTMocker,
):
    """
    Tests basic MerossCloudProfile (alone) behavior:
    - loading
    - starting (with cloud device_info list update)
    - discovery setup
    - saving
    """
    hass_storage.update(tc.MOCK_PROFILE_STORAGE)
    async with helpers.ProfileEntryMocker(hass) as profile_entry_mock:
        assert (profile := MerossApi.profiles.get(tc.MOCK_PROFILE_ID))
        # check we have refreshed our device list
        assert len(cloudapi_mock.api_calls) >= 1
        assert cloudapi_mock.api_calls[cloudapi.API_DEVICE_DEVLIST_PATH] == 1
        # check the cloud profile connected the mqtt server(s)
        # for discovery of devices. Our truth comes from
        # the cloudapi recovered device list
        expected_connections = set()
        for device_info in tc.MOCK_PROFILE_CLOUDAPI_DEVLIST:
            expected_connections.add(device_info[mc.KEY_DOMAIN])
            expected_connections.add(device_info[mc.KEY_RESERVEDDOMAIN])
        # check our profile built the expected number of connections
        mqttconnections = list(profile.mqttconnections.values())
        assert len(mqttconnections) == len(expected_connections)
        # and activated them (not less/no more)
        safe_connect_calls = []
        for expected_connection in expected_connections:
            host, port = cloudapi.parse_domain(expected_connection)
            connection_id = f"{tc.MOCK_PROFILE_ID}:{host}:{port}"
            mqttconnection = profile.mqttconnections[connection_id]
            mqttconnections.remove(mqttconnection)
            safe_connect_calls.append(call(mqttconnection, host, port))
        assert len(mqttconnections) == 0
        merossmqtt_mock.safe_connect_mock.assert_has_calls(
            safe_connect_calls,
            any_order=True,
        )
        # check the store has been persisted with cloudapi fresh device list
        await flush_store(profile._store)
        expected_storage_device_info_data = {
            device_info[mc.KEY_UUID]: device_info
            for device_info in tc.MOCK_PROFILE_CLOUDAPI_DEVLIST
        }
        profile_storage_data = hass_storage[tc.MOCK_PROFILE_STORE_KEY]["data"]
        assert (
            profile_storage_data[MerossCloudProfile.KEY_DEVICE_INFO]
            == expected_storage_device_info_data
        )
        # check cleanup
        assert await profile_entry_mock.async_unload()
        assert MerossApi.profiles[tc.MOCK_PROFILE_ID] is None
        assert merossmqtt_mock.safe_disconnect_mock.call_count == len(
            safe_connect_calls
        )


async def test_meross_profile_cloudapi_offline(
    hass,
    hass_storage,
    aioclient_mock,
    cloudapi_mock: helpers.CloudApiMocker,
    merossmqtt_mock: helpers.MerossMQTTMocker,
):
    """
    Tests basic MerossCloudProfile (alone) behavior:
    - loading
    - starting (with cloud api offline)
    - discovery setup
    """
    cloudapi_mock.online = False
    hass_storage.update(tc.MOCK_PROFILE_STORAGE)
    async with helpers.ProfileEntryMocker(hass) as profile_entry_mock:
        assert (profile := MerossApi.profiles.get(tc.MOCK_PROFILE_ID))
        # check we have tried to refresh our device list
        assert len(cloudapi_mock.api_calls) == 1
        assert cloudapi_mock.api_calls[cloudapi.API_DEVICE_DEVLIST_PATH] == 1
        # check the cloud profile connected the mqtt server(s)
        # for discovery of devices. Since the device list was not refreshed
        # we check against our stored list of devices
        expected_connections = set()
        for device_info in tc.MOCK_PROFILE_STORE_DEVICEINFO_DICT.values():
            expected_connections.add(device_info[mc.KEY_DOMAIN])
            expected_connections.add(device_info[mc.KEY_RESERVEDDOMAIN])
        # check our profile built the expected number of connections
        mqttconnections = list(profile.mqttconnections.values())
        assert len(mqttconnections) == len(expected_connections)
        # and activated them (not less/no more)
        safe_connect_calls = []
        for expected_connection in expected_connections:
            host, port = cloudapi.parse_domain(expected_connection)
            connection_id = f"{tc.MOCK_PROFILE_ID}:{host}:{port}"
            mqttconnection = profile.mqttconnections[connection_id]
            mqttconnections.remove(mqttconnection)
            safe_connect_calls.append(call(mqttconnection, host, port))
        assert len(mqttconnections) == 0
        merossmqtt_mock.safe_connect_mock.assert_has_calls(
            safe_connect_calls,
            any_order=True,
        )
        # check cleanup
        assert await profile_entry_mock.async_unload()
        assert MerossApi.profiles[tc.MOCK_PROFILE_ID] is None
        assert merossmqtt_mock.safe_disconnect_mock.call_count == len(
            safe_connect_calls
        )


async def test_meross_profile_with_device(
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

    async with helpers.DeviceContext(
        hass,
        helpers.build_emulator_for_profile(tc.MOCK_PROFILE_ID, model=mc.TYPE_MSS310),
        aioclient_mock,
        config_data={
            mlc.CONF_PROTOCOL: mlc.CONF_PROTOCOL_AUTO,
        },
    ) as devicecontext, helpers.ProfileEntryMocker(
        hass, auto_setup=False
    ) as profile_entry_mock:
        # the loading order of the config entries might
        # have side-effects because of device<->profile binding
        # beware: we cannot selectively load config_entries here
        # since component initialization load them all
        await devicecontext.async_load_config_entry()

        assert (api := devicecontext.api)
        assert (device := devicecontext.device)
        assert (profile := api.profiles.get(tc.MOCK_PROFILE_ID))

        assert device._cloud_profile is profile
        assert device._mqtt_connection in profile.mqttconnections.values()
        assert device._mqtt_connected is device._mqtt_connection

        await devicecontext.perform_coldstart()

        """
        # check the device registry has the device name from the cloud (stored)
        assert (
            device_registry_entry := device_registry.async_get(hass).async_get_device(
                **device.deviceentry_id
            )
        ) and device_registry_entry.name == tc.MOCK_PROFILE_MSS310_DEVNAME_STORED
        # now the profile should query the cloudapi and get an updated device_info list
        await devicecontext.async_tick(
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
        """
        # remove the cloud profile
        assert await profile_entry_mock.async_unload()
        assert api.profiles[tc.MOCK_PROFILE_ID] is None
        assert device._cloud_profile is None
        assert device._mqtt_connection is None
        assert device._mqtt_connected is None
