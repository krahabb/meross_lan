"""Test for meross cloud profiles"""

import typing
from unittest import mock

from homeassistant.helpers import device_registry
from pytest_homeassistant_custom_component.common import flush_store

from custom_components.meross_lan import MerossApi, const as mlc
from custom_components.meross_lan.meross_profile import MerossCloudProfile
from custom_components.meross_lan.merossclient import HostAddress, cloudapi, const as mc

from . import const as tc, helpers

if typing.TYPE_CHECKING:
    from homeassistant.core import HomeAssistant


async def test_meross_profile(
    request,
    hass: "HomeAssistant",
    hass_storage,
    aioclient_mock,
    cloudapi_mock: helpers.CloudApiMocker,
    merossmqtt_mock: helpers.MerossMQTTMocker,
    time_mock: helpers.TimeMocker,
):
    """
    Tests basic MerossCloudProfile (alone) behavior:
    - loading
    - starting (with cloud device_info list update)
    - discovery setup
    - saving
    """
    hass_storage.update(tc.MOCK_PROFILE_STORAGE)
    async with helpers.ProfileEntryMocker(request, hass) as profile_entry_mock:
        assert (profile := MerossApi.profiles.get(tc.MOCK_PROFILE_ID))
        # check we have refreshed our device list
        # the device discovery starts when we setup the entry and it might take
        # some while since we're queueing multiple requests (2).
        # Our profile starts with config as in tc.MOCK_PROFILE_STORAGE and
        # the cloud api is setup with data from MOCK_CLOUDAPI_DEVICE_DEVLIST.

        await time_mock.async_tick(5)
        await time_mock.async_tick(5)
        await hass.async_block_till_done()

        assert len(cloudapi_mock.api_calls) >= 2
        assert cloudapi_mock.api_calls[cloudapi.API_DEVICE_DEVLIST_PATH] == 1
        assert cloudapi_mock.api_calls[cloudapi.API_DEVICE_LATESTVERSION_PATH] == 1
        # check the cloud profile connected the mqtt server(s)
        # for discovery of devices. Our truth comes from
        # the cloudapi recovered device list
        expected_connections = set()
        for device_info in tc.MOCK_CLOUDAPI_DEVICE_DEVLIST.values():
            expected_connections.add(device_info[mc.KEY_DOMAIN])
            expected_connections.add(device_info[mc.KEY_RESERVEDDOMAIN])
        # check our profile built the expected number of connections
        mqttconnections = list(profile.mqttconnections.values())
        assert len(mqttconnections) == len(expected_connections)
        # and activated them (not less/no more)
        safe_start_calls = []
        for expected_connection in expected_connections:
            broker = HostAddress.build(expected_connection)
            connection_id = f"{broker.host}:{broker.port}"
            mqttconnection = profile.mqttconnections[connection_id]
            mqttconnections.remove(mqttconnection)
            safe_start_calls.append(mock.call(mqttconnection, broker))
        assert len(mqttconnections) == 0
        merossmqtt_mock.safe_start_mock.assert_has_calls(
            safe_start_calls,
            any_order=True,
        )
        await flush_store(profile._store)
        # check the store has been persisted with cloudapi fresh device list
        profile_storage_data = hass_storage[tc.MOCK_PROFILE_STORE_KEY]["data"]
        expected_storage_device_info_data = {
            device_info[mc.KEY_UUID]: device_info
            for device_info in tc.MOCK_CLOUDAPI_DEVICE_DEVLIST.values()
        }
        assert (
            profile_storage_data[MerossCloudProfile.KEY_DEVICE_INFO]
            == expected_storage_device_info_data
        )
        # check the update firmware versions was stored
        assert (
            profile_storage_data[MerossCloudProfile.KEY_LATEST_VERSION]
            == tc.MOCK_CLOUDAPI_DEVICE_LATESTVERSION
        )

        # check cleanup
        assert await profile_entry_mock.async_unload()
        assert MerossApi.profiles[tc.MOCK_PROFILE_ID] is None
        assert merossmqtt_mock.safe_stop_mock.call_count == len(safe_start_calls)


async def test_meross_profile_cloudapi_offline(
    request,
    hass: "HomeAssistant",
    hass_storage,
    aioclient_mock,
    cloudapi_mock: helpers.CloudApiMocker,
    merossmqtt_mock: helpers.MerossMQTTMocker,
    time_mock: helpers.TimeMocker,
):
    """
    Tests basic MerossCloudProfile (alone) behavior:
    - loading
    - starting (with cloud api offline)
    - discovery setup
    """
    cloudapi_mock.online = False
    hass_storage.update(tc.MOCK_PROFILE_STORAGE)
    async with helpers.ProfileEntryMocker(request, hass) as profile_entry_mock:
        assert (profile := MerossApi.profiles.get(tc.MOCK_PROFILE_ID))
        time_mock.tick(mlc.PARAM_CLOUDPROFILE_DELAYED_SETUP_TIMEOUT)
        time_mock.tick(mlc.PARAM_CLOUDPROFILE_DELAYED_SETUP_TIMEOUT)
        await hass.async_block_till_done()

        # check we have tried to refresh our device list
        assert len(cloudapi_mock.api_calls) == 1
        assert cloudapi_mock.api_calls[cloudapi.API_DEVICE_DEVLIST_PATH] == 1
        # check the cloud profile connected the mqtt server(s)
        # for discovery of devices. Since the device list was not refreshed
        # we check against our stored list of devices
        expected_connections = set()
        if mc.KEY_MQTTDOMAIN in profile.config:
            expected_connections.add(profile.config.get(mc.KEY_MQTTDOMAIN))
        """
        # update 2023-12-08: on entry setup we're not automatically querying
        # the stored device list
        for device_info in tc.MOCK_PROFILE_STORE_DEVICEINFO_DICT.values():
            expected_connections.add(device_info[mc.KEY_DOMAIN])
            expected_connections.add(device_info[mc.KEY_RESERVEDDOMAIN])
        """
        # check our profile built the expected number of connections
        mqttconnections = list(profile.mqttconnections.values())
        assert len(mqttconnections) == len(expected_connections)
        # and activated them (not less/no more)
        safe_start_calls = []
        for expected_connection in expected_connections:
            broker = HostAddress.build(expected_connection)
            mqttconnection = profile.mqttconnections[str(broker)]
            mqttconnections.remove(mqttconnection)
            safe_start_calls.append(mock.call(mqttconnection, broker))
        assert len(mqttconnections) == 0
        merossmqtt_mock.safe_start_mock.assert_has_calls(
            safe_start_calls,
            any_order=True,
        )
        # check cleanup
        assert await profile_entry_mock.async_unload()
        assert MerossApi.profiles[tc.MOCK_PROFILE_ID] is None
        assert merossmqtt_mock.safe_stop_mock.call_count == len(safe_start_calls)


async def test_meross_profile_with_device(
    request,
    hass: "HomeAssistant",
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

    async with (
        helpers.DeviceContext(
            request,
            hass,
            helpers.build_emulator_for_profile(
                tc.MOCK_PROFILE_CONFIG, model=mc.TYPE_MSS310
            ),
            aioclient_mock,
            data={
                mlc.CONF_PROTOCOL: mlc.CONF_PROTOCOL_AUTO,
            },
        ) as devicecontext,
        helpers.ProfileEntryMocker(
            request, hass, auto_setup=False
        ) as profile_entry_mock,
    ):
        # the loading order of the config entries might
        # have side-effects because of device<->profile binding
        # beware: we cannot selectively load config_entries here
        # since component initialization load them all
        assert await devicecontext.async_setup()

        assert (api := devicecontext.api)
        assert (device := devicecontext.device)
        assert (profile := api.profiles.get(tc.MOCK_PROFILE_ID))

        assert device._profile is profile
        assert device._mqtt_connection in profile.mqttconnections.values()
        """
        The cloud MQTT connection is (or might be) done in an executor
        so we cannot reliably validate this condition. Later on it should
        be connected for sure
        assert device._mqtt_connected is device._mqtt_connection
        """

        device = await devicecontext.perform_coldstart()

        # check the device registry has the device name from the cloud (stored)
        assert (
            device_registry_entry := device_registry.async_get(hass).async_get_device(
                **device.deviceentry_id
            )
        ) and device_registry_entry.name == tc.MOCK_PROFILE_MSS310_DEVNAME_STORED
        # now the profile should query the cloudapi and get an updated device_info list
        await devicecontext.time.async_tick(
            mlc.PARAM_CLOUDPROFILE_DELAYED_SETUP_TIMEOUT
        )
        mqttconnections = list(profile.mqttconnections.values())
        merossmqtt_mock.safe_start_mock.assert_has_calls(
            [
                mock.call(
                    mqttconnections[0], HostAddress(tc.MOCK_PROFILE_MSS310_DOMAIN, 443)
                ),
                mock.call(
                    mqttconnections[1], HostAddress(tc.MOCK_PROFILE_MSH300_DOMAIN, 443)
                ),
            ],
            any_order=True,
        )
        # check the device name was updated from cloudapi query
        assert (
            device_registry_entry := device_registry.async_get(hass).async_get_device(
                **device.deviceentry_id
            )
        ) and device_registry_entry.name == tc.MOCK_PROFILE_MSS310_DEVNAME
        assert cloudapi_mock.api_calls[cloudapi.API_DEVICE_DEVLIST_PATH] == 1

        # now check if a new fw is correctly managed in update entity
        assert device.update_firmware is None
        tc.MOCK_CLOUDAPI_DEVICE_LATESTVERSION[0][mc.KEY_VERSION] = "2.1.5"
        await devicecontext.time.async_tick(
            mlc.PARAM_CLOUDPROFILE_QUERY_LATESTVERSION_TIMEOUT
        )
        update_firmware = device.update_firmware
        assert update_firmware
        update_firmware_state = hass.states.get(update_firmware.entity_id)
        assert update_firmware_state and update_firmware_state.state == "on"

        # this condition needs testing after the mqtt client schedule_connect
        # executor code has been done. No effort to reliably assert that
        # but at this point in time it should have run
        assert device._mqtt_connected is device._mqtt_connection
        # TODO: check the protocol switching?

        # remove the cloud profile
        assert await profile_entry_mock.async_unload()
        assert api.profiles[tc.MOCK_PROFILE_ID] is None
        assert device._profile is None
        assert device._mqtt_connection is None
        assert device._mqtt_connected is None
