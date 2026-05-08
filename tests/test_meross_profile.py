"""Test for meross cloud profiles"""

from typing import TYPE_CHECKING
from types import SimpleNamespace
from unittest import mock

from homeassistant.helpers import device_registry as dr
import paho.mqtt.client as paho_mqtt
from pytest_homeassistant_custom_component.common import flush_store

from custom_components.meross_lan import const as mlc
from custom_components.meross_lan.helpers.meross_profile import MerossProfile
from custom_components.meross_lan.merossclient import HostAddress, cloudapi
from custom_components.meross_lan.merossclient.client import Transport
from custom_components.meross_lan.merossclient.protocol import (
    const as mc,
    namespaces as mn,
)
from custom_components.meross_lan.merossclient.protocol.message import MerossMessage

from . import const as tc, helpers

if TYPE_CHECKING:
    from typing import Any

    from homeassistant.core import HomeAssistant


def test_get_latest_version_prefers_hardware_train():
    class ProfileStub:
        KEY_LATEST_VERSION_HISTORY = MerossProfile.KEY_LATEST_VERSION_HISTORY

    profile = ProfileStub()
    descriptor = SimpleNamespace(
        type="mss110",
        subType="us",
        fw_version="4.2.14",
        hw_version="4.0.0",
    )
    profile._data = {
        MerossProfile.KEY_LATEST_VERSION_HISTORY: {
            "mss110:us": [
                {"2026-05-07T00:00:00+00:00": {mc.KEY_VERSION: "7.3.46"}},
                {"2026-05-07T00:01:00+00:00": {mc.KEY_VERSION: "4.2.14"}},
            ]
        }
    }

    assert (
        MerossProfile.get_latest_version(profile, descriptor)[mc.KEY_VERSION]
        == "4.2.14"
    )

    profile._data[MerossProfile.KEY_LATEST_VERSION_HISTORY]["mss110:us"] = [
        {"2026-05-07T00:00:00+00:00": {mc.KEY_VERSION: "4.2.14"}},
        {"2026-05-07T00:01:00+00:00": {mc.KEY_VERSION: "7.3.46"}},
    ]
    assert (
        MerossProfile.get_latest_version(profile, descriptor)[mc.KEY_VERSION]
        == "4.2.14"
    )

    profile._data[MerossProfile.KEY_LATEST_VERSION_HISTORY]["mss110:us"][-1] = {
        "2026-05-07T00:01:00+00:00": {mc.KEY_VERSION: "4.2.15"}
    }
    assert (
        MerossProfile.get_latest_version(profile, descriptor)[mc.KEY_VERSION]
        == "4.2.15"
    )

    profile._data[MerossProfile.KEY_LATEST_VERSION_HISTORY]["mss110:us"] = [
        {"2026-05-07T00:00:00+00:00": {mc.KEY_VERSION: "7.3.46"}},
    ]
    descriptor.fw_version = "4.2.14"
    assert MerossProfile.get_latest_version(profile, descriptor) is None

    descriptor.fw_version = "3.2.14"
    assert (
        MerossProfile.get_latest_version(profile, descriptor)[mc.KEY_VERSION]
        == "7.3.46"
    )

    profile._data[MerossProfile.KEY_LATEST_VERSION_HISTORY]["mss110:us"] = [
        {"2026-05-07T00:00:00+00:00": {mc.KEY_VERSION: None}},
        {"2026-05-07T00:01:00+00:00": {mc.KEY_VERSION: "4.2.15"}},
    ]
    descriptor.fw_version = "4.2.14"
    assert (
        MerossProfile.get_latest_version(profile, descriptor)[mc.KEY_VERSION]
        == "4.2.15"
    )


async def test_meross_profile(
    request,
    hass: "HomeAssistant",
    hass_storage: dict[str, "Any"],
    cloudapi_mock: helpers.CloudApiMocker,
    merossmqtt_mock: helpers.MerossMQTTMocker,
):
    """
    Tests basic MerossProfile (alone) behavior:
    - loading
    - starting (with cloud device_info list update)
    - discovery setup
    - saving
    """
    hass_storage.update(tc.MOCK_PROFILE_STORAGE)
    async with helpers.ProfileEntryMocker(request, hass) as context:
        assert (profile := context.api.profiles.get(tc.MOCK_PROFILE_ID))
        # check we have refreshed our device list
        # the device discovery starts when we setup the entry and it might take
        # some while since we're queueing multiple requests (2).
        # Our profile starts with config as in tc.MOCK_PROFILE_STORAGE and
        # the cloud api is setup with data from MOCK_CLOUDAPI_DEVICE_DEVLIST.

        await context.time_mock.async_tick(mlc.PARAM_CLOUDPROFILE_DELAYED_SETUP_TIMEOUT)
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
            mqttconnection = profile.mqttconnections[f"{broker.host}:{broker.port}"]
            mqttconnections.remove(mqttconnection)
            safe_start_calls.append(mock.call(mqttconnection))
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
            profile_storage_data[MerossProfile.KEY_DEVICE_INFO]
            == expected_storage_device_info_data
        )
        # check the update firmware versions was stored
        assert (
            profile_storage_data[MerossProfile.KEY_LATEST_VERSION]
            == tc.MOCK_CLOUDAPI_DEVICE_LATESTVERSION
        )

        # check cleanup
        assert await context.async_unload()
        assert context.api.profiles[tc.MOCK_PROFILE_ID] is None
        assert merossmqtt_mock.safe_stop_mock.call_count == len(safe_start_calls)


async def test_meross_profile_cloudapi_offline(
    request,
    hass: "HomeAssistant",
    hass_storage: dict[str, "Any"],
    cloudapi_mock: helpers.CloudApiMocker,
    merossmqtt_mock: helpers.MerossMQTTMocker,
):
    """
    Tests basic MerossProfile (alone) behavior:
    - loading
    - starting (with cloud api offline)
    - discovery setup
    """
    cloudapi_mock.online = False
    hass_storage.update(tc.MOCK_PROFILE_STORAGE)
    async with helpers.ProfileEntryMocker(request, hass) as context:
        assert (profile := context.api.profiles.get(tc.MOCK_PROFILE_ID))
        await context.time_mock.async_tick(mlc.PARAM_CLOUDPROFILE_DELAYED_SETUP_TIMEOUT)

        # check we have tried to refresh our devicelist/latestversion
        assert len(cloudapi_mock.api_calls) == 2
        assert cloudapi_mock.api_calls[cloudapi.API_DEVICE_DEVLIST_PATH] == 1
        assert cloudapi_mock.api_calls[cloudapi.API_DEVICE_LATESTVERSION_PATH] == 1
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
            mqttconnection = profile.mqttconnections[f"{broker.host}:{broker.port}"]
            mqttconnections.remove(mqttconnection)
            safe_start_calls.append(mock.call(mqttconnection))
        assert len(mqttconnections) == 0
        merossmqtt_mock.safe_start_mock.assert_has_calls(
            safe_start_calls,
            any_order=True,
        )
        # check cleanup
        assert await context.async_unload()
        assert context.api.profiles[tc.MOCK_PROFILE_ID] is None
        assert merossmqtt_mock.safe_stop_mock.call_count == len(safe_start_calls)


async def test_meross_profile_with_device(
    request,
    hass: "HomeAssistant",
    hass_storage: dict[str, "Any"],
    cloudapi_mock: helpers.CloudApiMocker,
    merossmqtt_mock: helpers.MerossMQTTMocker,
):
    """
    Tests basic MerossProfile behavior:
    - loading
    - starting (with cloud device_info list update)
    - discovery setup
    - saving
    """
    hass_storage.update(tc.MOCK_PROFILE_STORAGE)

    async with (
        helpers.ProfileEntryMocker(request, hass, auto_setup=True) as profile_context,
        helpers.DeviceContext(
            request,
            hass,
            helpers.build_emulator_for_profile(
                tc.MOCK_PROFILE_CONFIG, model=mc.TYPE_MSS310
            ),
            data={
                mlc.CONF_PROTOCOL: Transport.AUTO.value,
            },
            auto_poll=True,
        ) as device_context,
    ):
        # the loading order of the config entries might
        # have side-effects because of device<->profile binding
        # beware: we cannot selectively load config_entries here
        # since component initialization load them all

        assert (api := device_context.api)
        assert (device := device_context.device)
        assert (profile := api.profiles.get(tc.MOCK_PROFILE_ID))

        assert device.profile is profile
        assert (
            device.mqtt and device.mqtt.connection
        ) in profile.mqttconnections.values()

        # The cloud MQTT connection is (or might be) done in an executor
        # so we cannot reliably validate this condition. Later on it should
        # be connected for sure
        # assert device._mqtt_connected is device._mqtt_connection

        # check the device registry has the device name from the cloud (stored)
        assert (
            device_entry := dr.async_get(hass).async_get_device(
                identifiers={(mlc.DOMAIN, device_context.device_id)}
            )
        ) and device_entry.name == tc.MOCK_PROFILE_MSS310_DEVNAME_STORED
        # now the profile should query the cloudapi and get an updated device_info list
        await device_context.time_mock.async_tick(
            mlc.PARAM_CLOUDPROFILE_DELAYED_SETUP_TIMEOUT
        )
        mqttconnections = list(profile.mqttconnections.values())
        assert mqttconnections[0].id == HostAddress(tc.MOCK_PROFILE_MSS310_DOMAIN, 443)
        assert mqttconnections[1].id == HostAddress(tc.MOCK_PROFILE_MSH300_DOMAIN, 443)
        merossmqtt_mock.safe_start_mock.assert_has_calls(
            [mock.call(mqttconnections[0]), mock.call(mqttconnections[1])],
            any_order=True,
        )
        # check the device name was updated from cloudapi query
        assert (
            device_entry := dr.async_get(hass).async_get_device(
                identifiers={(mlc.DOMAIN, device_context.device_id)}
            )
        ) and device_entry.name == tc.MOCK_PROFILE_MSS310_DEVNAME
        assert cloudapi_mock.api_calls[cloudapi.API_DEVICE_DEVLIST_PATH] == 1

        # now check if a new fw is correctly managed in update entity
        assert device.update_firmware is None
        latest_version = tc.MOCK_CLOUDAPI_DEVICE_LATESTVERSION[0]
        latest_version[mc.KEY_TYPE] = device.descriptor.type
        latest_version[mc.KEY_SUBTYPE] = device.descriptor.subType
        latest_version[mc.KEY_VERSION] = "2.1.5"
        await device_context.time_mock.async_tick(
            mlc.PARAM_CLOUDPROFILE_QUERY_DEVICELIST_TIMEOUT + 1
        )
        update_firmware = device.update_firmware
        assert update_firmware
        update_firmware_state = device_context.get_hass_state(update_firmware.entity_id)
        assert update_firmware_state and update_firmware_state.state == "on"

        # this conditions needs testing after the mqtt client schedule_connect
        # executor code has been done. No effort to reliably assert that
        # but at this point in time it should have run

        # check correct transports/clients state (mock config allows publishing)
        assert device.http and device.http.is_connected
        assert (
            device.mqtt
            and device.mqtt.connection.is_connected
            and not device.mqtt.is_connected
        )
        assert device.mqtt and device.mqtt.connection.can_publish
        assert len(device._clients_connected) == 1
        assert not device.mqtt_active

        # simulate async PUSH message from the device mqtt connection
        mqttconnection = device.mqtt.connection
        message = MerossMessage.build(
            mn.Appliance_Control_ToggleX,
            mc.METHOD_PUSH,
            {mn.Appliance_Control_ToggleX.key: [{"channel": 0, "onoff": 1}]},
            device.key,
            from_=mc.TOPIC_RESPONSE.format(device.id),
        )
        mqtt_message = paho_mqtt.MQTTMessage()
        mqtt_message.payload = message.json.encode()
        mqttconnection.on_message(mqtt_message)
        assert device.mqtt and device.mqtt.is_connected
        assert len(device._clients_connected) == 2
        assert device.mqtt_active

        # both clients should be connected

        # remove the cloud profile
        assert await profile_context.async_unload()
        assert api.profiles[tc.MOCK_PROFILE_ID] is None
        assert device.profile is None
        assert device.mqtt is None
        assert len(device._clients_connected) == 1
        assert not device.mqtt_active
