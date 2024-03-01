"""Test meross_lan config flow"""

from typing import Final
from uuid import uuid4

from homeassistant import config_entries
from homeassistant.components.dhcp import DhcpServiceInfo
from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResult, FlowResultType
from pytest_homeassistant_custom_component.common import async_fire_mqtt_message

from custom_components.meross_lan import const as mlc
from custom_components.meross_lan.merossclient import (
    build_message,
    cloudapi,
    const as mc,
    json_dumps,
)

from tests import const as tc, helpers


async def _cleanup_config_entry(hass: HomeAssistant, result: FlowResult):
    config_entry: ConfigEntry = result["result"]  # type: ignore
    assert config_entry.state == ConfigEntryState.LOADED
    await hass.config_entries.async_unload(config_entry.entry_id)


async def test_device_config_flow(hass: HomeAssistant, aioclient_mock):
    """
    Test standard manual device entry config flow
    """
    with helpers.EmulatorContext(mc.TYPE_MTS200, aioclient_mock) as emulator_context:
        emulator = emulator_context.emulator
        host = emulator_context.host

        config_flow = hass.config_entries.flow
        result = await config_flow.async_init(
            mlc.DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        result = await helpers.async_assert_flow_menu_to_step(
            config_flow, result, "user", "device"
        )
        # we'll use the configuration of the emulator to reach it
        # through the aioclient_mock
        result = await config_flow.async_configure(
            result["flow_id"],
            user_input={mlc.CONF_HOST: host, mlc.CONF_KEY: emulator.key},
        )
        assert result["type"] == FlowResultType.FORM  # type: ignore
        assert result["step_id"] == "finalize"  # type: ignore
        result = await config_flow.async_configure(result["flow_id"], user_input={})
        assert result["type"] == FlowResultType.CREATE_ENTRY  # type: ignore

        data: mlc.DeviceConfigType = result["data"]  # type: ignore
        descriptor = emulator.descriptor
        assert data[mlc.CONF_DEVICE_ID] == descriptor.uuid
        assert data[mlc.CONF_HOST] == host
        assert data[mlc.CONF_KEY] == emulator.key
        # since the emulator updates it's own state (namely the timestamp)
        # on every request we have to be careful in comparing configuration
        payload = data[mlc.CONF_PAYLOAD]
        payload_all = payload[mc.KEY_ALL]
        payload_time = payload_all[mc.KEY_SYSTEM][mc.KEY_TIME]
        if payload_time[mc.KEY_TIMESTAMP] == descriptor.time[mc.KEY_TIMESTAMP] - 1:
            # we just have to patch when the emulator timestamp ticked around a second
            payload_time[mc.KEY_TIMESTAMP] = descriptor.time[mc.KEY_TIMESTAMP]
        assert payload_all == descriptor.all
        assert payload[mc.KEY_ABILITY] == descriptor.ability

        # now cleanup the entry
        await _cleanup_config_entry(hass, result)


async def test_profile_config_flow(
    hass: HomeAssistant,
    cloudapi_mock: helpers.CloudApiMocker,
    merossmqtt_mock: helpers.MerossMQTTMocker,
):
    """
    Test cloud profile entry config flow
    """
    user_input = {
        mlc.CONF_EMAIL: tc.MOCK_PROFILE_EMAIL,
        mlc.CONF_PASSWORD: tc.MOCK_PROFILE_PASSWORD,
        mlc.CONF_SAVE_PASSWORD: False,
        mlc.CONF_ALLOW_MQTT_PUBLISH: True,
        mlc.CONF_CHECK_FIRMWARE_UPDATES: True,
    }

    config_flow = hass.config_entries.flow

    result = await config_flow.async_init(
        mlc.DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await helpers.async_assert_flow_menu_to_step(
        config_flow, result, "user", "profile"
    )
    # enter wrong profile username/password
    result = await config_flow.async_configure(
        result["flow_id"],
        user_input=user_input
        | {
            mlc.CONF_PASSWORD: "",
        },
    )
    assert cloudapi_mock.api_calls[cloudapi.API_AUTH_SIGNIN_PATH] == 1
    assert result["type"] == FlowResultType.FORM  # type: ignore
    assert result["step_id"] == "profile"  # type: ignore
    # put the cloud offline
    cloudapi_mock.online = False
    result = await config_flow.async_configure(
        result["flow_id"],
        user_input=user_input,
    )
    assert cloudapi_mock.api_calls[cloudapi.API_AUTH_SIGNIN_PATH] == 2
    assert result["type"] == FlowResultType.FORM  # type: ignore
    assert result["step_id"] == "profile"  # type: ignore
    # online the cloud and finish setup
    cloudapi_mock.online = True
    result = await config_flow.async_configure(
        result["flow_id"],
        user_input=user_input,
    )
    assert cloudapi_mock.api_calls[cloudapi.API_AUTH_SIGNIN_PATH] == 3
    assert result["type"] == FlowResultType.CREATE_ENTRY  # type: ignore

    profile_config: mlc.ProfileConfigType = result["data"]  # type: ignore
    # these are the defaults as set by the config_flow
    profile_config_expected = dict(tc.MOCK_PROFILE_CONFIG)
    profile_config_expected.pop(mlc.CONF_OBFUSCATE)
    assert profile_config == profile_config_expected

    # now cleanup the entry
    await _cleanup_config_entry(hass, result)


async def test_mqtt_discovery_config_flow(hass: HomeAssistant, hamqtt_mock):
    """
    Test the initial discovery process i.e. meross_lan
    not configured yet
    """
    emulator = helpers.build_emulator(mc.TYPE_MSS310)
    emulator.key = ""  # patch the key so the default hub key will work
    device_id = emulator.descriptor.uuid
    topic = mc.TOPIC_RESPONSE.format(device_id)
    payload = build_message(
        mc.NS_APPLIANCE_CONTROL_TOGGLEX,
        mc.METHOD_PUSH,
        {mc.KEY_TOGGLEX: {mc.KEY_CHANNEL: 0, mc.KEY_ONOFF: 0}},
        emulator.key,
        mc.TOPIC_REQUEST.format(device_id),
    )

    async_fire_mqtt_message(hass, topic, json_dumps(payload))
    await hass.async_block_till_done()

    # we should have 2 flows now: one for the MQTT hub and the other for the
    # incoming device but this second one needs the time to progress in order to show up
    # so we're not checking now (#TODO: warp the test time so discovery will complete)
    flow_hub = None
    flow_device = None
    for flow in hass.config_entries.flow.async_progress_by_handler(mlc.DOMAIN):
        flow_unique_id = flow.get("context", {}).get("unique_id")
        if flow_unique_id == mlc.DOMAIN:
            flow_hub = flow
        elif flow_unique_id == device_id:
            flow_device = flow
        else:
            assert False, "unexpected flow in progress"

    assert flow_hub
    assert flow_hub["step_id"] == "hub"  # type: ignore
    result = await hass.config_entries.flow.async_configure(
        flow_hub["flow_id"], user_input={mlc.CONF_KEY: emulator.key}
    )
    assert result["type"] == FlowResultType.CREATE_ENTRY  # type: ignore
    await _cleanup_config_entry(hass, result)

    # TODO: check the device flow after we completed discovery
    assert flow_device is None

    await hass.async_block_till_done()


async def test_dhcp_discovery_config_flow(hass: HomeAssistant):
    result = await hass.config_entries.flow.async_init(
        mlc.DOMAIN,
        context={"source": config_entries.SOURCE_DHCP},
        data=DhcpServiceInfo(tc.MOCK_DEVICE_IP, "", tc.MOCK_MACADDRESS),
    )

    assert result["type"] == FlowResultType.FORM  # type: ignore
    assert result["step_id"] == "device"  # type: ignore


async def test_dhcp_renewal_config_flow(hass: HomeAssistant, aioclient_mock):
    """
    When an entry is already configured, check what happens when dhcp sends
    us a new ip
    """
    device_type: Final = mc.TYPE_MTS200
    async with helpers.DeviceContext(
        hass, device_type, aioclient_mock
    ) as device_context:
        emulator = device_context.emulator
        device = await device_context.perform_coldstart()

        # better be sure our context is consistent with expectations!
        assert device.host == str(id(emulator))
        assert device.id == device.descriptor.uuid

        # since we check the DHCP renewal comes form a legit device we need to setup
        # a mock responding at the solicited ip with the same device info (descriptor)
        # since dhcp config flow will check by mac address

        # here we build a 'clone' of the configured device
        emulator_dhcp = helpers.build_emulator(
            device_type, device_id=device.id, key=device.key
        )
        assert (
            emulator_dhcp.descriptor.macAddress == device.descriptor.macAddress
        ), "wrong emulator clone"
        assert (
            emulator_dhcp.descriptor.uuid == device.descriptor.uuid
        ), "wrong emulator clone"
        # now we mock the device emulator at new address
        DHCP_GOOD_HOST: Final = "88.88.88.88"
        with helpers.EmulatorContext(
            emulator_dhcp, aioclient_mock, host=DHCP_GOOD_HOST
        ):
            result = await hass.config_entries.flow.async_init(
                mlc.DOMAIN,
                context={"source": config_entries.SOURCE_DHCP},
                data=DhcpServiceInfo(DHCP_GOOD_HOST, "", device.descriptor.macAddress),
            )

            assert result["type"] == FlowResultType.ABORT  # type: ignore
            assert result["reason"] == "already_configured"  # type: ignore
            # also check the device host got updated with new address
            assert device.host == DHCP_GOOD_HOST, "device host was not updated"

        # here we build a different (device uuid) device instance
        BOGUS_DEVICE_ID: Final = uuid4().hex
        emulator_dhcp = helpers.build_emulator(
            device_type, device_id=BOGUS_DEVICE_ID, key=device.key
        )
        assert (
            emulator_dhcp.descriptor.macAddress != device.descriptor.macAddress
        ), "wrong emulator clone"
        assert (
            emulator_dhcp.descriptor.uuid != device.descriptor.uuid
        ), "wrong emulator clone"
        # now we mock the device emulator at new address
        DHCP_BOGUS_HOST: Final = "99.99.99.99"
        with helpers.EmulatorContext(
            emulator_dhcp, aioclient_mock, host=DHCP_BOGUS_HOST
        ):
            result = await hass.config_entries.flow.async_init(
                mlc.DOMAIN,
                context={"source": config_entries.SOURCE_DHCP},
                data=DhcpServiceInfo(DHCP_BOGUS_HOST, "", device.descriptor.macAddress),
            )

            assert result["type"] == FlowResultType.ABORT  # type: ignore
            assert result["reason"] == "already_configured"  # type: ignore
            # also check the device host got updated with MOCK_DEVICE_IP
            assert device.host == DHCP_GOOD_HOST, "device host was wrongly updated"


async def test_options_flow(
    hass: HomeAssistant, aioclient_mock, hamqtt_mock, merossmqtt_mock
):
    """
    Tests the device config entry option flow. This code could potentially use
    either HTTP or MQTT so we accordingly mock both. TODO: perform the test check
    against different config options (namely: the protocol) in order to see if
    they behave as expected
    """
    async with helpers.DeviceContext(hass, mc.TYPE_MTS200, aioclient_mock) as context:
        device = await context.perform_coldstart()

        options_flow = hass.config_entries.options
        result = await options_flow.async_init(device.config_entry_id)
        result = await helpers.async_assert_flow_menu_to_step(
            options_flow, result, "menu", "device"
        )
        result = await options_flow.async_configure(
            result["flow_id"],
            user_input={
                mlc.CONF_HOST: device.host,
                mlc.CONF_KEY: "wrongkey",
                mlc.CONF_PROTOCOL: mlc.CONF_PROTOCOL_HTTP,
            },
        )
        result = await helpers.async_assert_flow_menu_to_step(
            options_flow, result, "keyerror", "device"
        )
        result = await options_flow.async_configure(
            result["flow_id"],
            user_input={
                mlc.CONF_HOST: device.host,
                mlc.CONF_KEY: device.key,
                mlc.CONF_PROTOCOL: mlc.CONF_PROTOCOL_HTTP,
            },
        )
        assert result["type"] == FlowResultType.CREATE_ENTRY
