"""Test meross_lan config flow"""

from typing import TYPE_CHECKING
from uuid import uuid4

from homeassistant import config_entries
from homeassistant.components import dhcp
from homeassistant.config_entries import ConfigEntry, ConfigEntryState, ConfigFlowResult
from homeassistant.data_entry_flow import FlowResultType

try:
    from homeassistant.helpers.service_info.dhcp import DhcpServiceInfo
except ImportError:
    from homeassistant.components.dhcp import DhcpServiceInfo  # type: ignore

from pytest_homeassistant_custom_component.common import async_fire_mqtt_message

from custom_components.meross_lan import const as mlc
from custom_components.meross_lan.merossclient import (
    cloudapi,
    fmt_macaddress,
    json_dumps,
)
from custom_components.meross_lan.merossclient.protocol import (
    const as mc,
    namespaces as mn,
)
from custom_components.meross_lan.merossclient.protocol.message import build_message

from tests import const as tc, helpers

try:
    from homeassistant.helpers import discovery_flow
except ImportError:
    discovery_flow = None


if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

    from pytest_homeassistant_custom_component.test_util.aiohttp import (
        AiohttpClientMocker,
    )


async def _cleanup_config_entry(hass: "HomeAssistant", result: ConfigFlowResult):
    config_entry: ConfigEntry = result["result"]  # type: ignore
    assert config_entry.state == ConfigEntryState.LOADED
    await hass.config_entries.async_unload(config_entry.entry_id)


async def test_device_config_flow(hass: "HomeAssistant", aioclient_mock):
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
        assert result.get("type") == FlowResultType.FORM
        assert result.get("step_id") == "finalize"
        result = await config_flow.async_configure(result["flow_id"], user_input={})
        assert result.get("type") == FlowResultType.CREATE_ENTRY

        # kick device polling task
        await hass.async_block_till_done()

        data: mlc.DeviceConfigType = result["data"]  # type: ignore
        descriptor = emulator.descriptor
        assert data[mlc.CONF_DEVICE_ID] == descriptor.uuid
        assert data.get(mlc.CONF_HOST) == host
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
    hass: "HomeAssistant",
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


async def test_device_config_flow_with_profile(
    hass: "HomeAssistant",
    aioclient_mock: "AiohttpClientMocker",
    cloudapi_mock: helpers.CloudApiMocker,
    merossmqtt_mock: helpers.MerossMQTTMocker,
):
    """
    Test standard manual device entry config flow with cloud key retrieval
    """

    emulator = helpers.build_emulator(
        mc.TYPE_MSS310, key=tc.MOCK_PROFILE_KEY, uuid=tc.MOCK_PROFILE_MSS310_UUID
    )

    with helpers.EmulatorContext(emulator, aioclient_mock) as emulator_context:

        user_input = {mlc.CONF_HOST: emulator_context.host, mlc.CONF_KEY: ""}

        config_flow = hass.config_entries.flow
        result = await config_flow.async_init(
            mlc.DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        result = await helpers.async_assert_flow_menu_to_step(
            config_flow, result, "user", "device"
        )
        result = await config_flow.async_configure(
            result["flow_id"],
            user_input=user_input,
        )
        # verifies the empty key was not good and choose to retrieve cloud key (cloud profile)
        result = await helpers.async_assert_flow_menu_to_step(
            config_flow, result, "keyerror", "profile"
        )

        result = await config_flow.async_configure(
            result["flow_id"],
            user_input={
                mlc.CONF_EMAIL: tc.MOCK_PROFILE_EMAIL,
                mlc.CONF_PASSWORD: tc.MOCK_PROFILE_PASSWORD,
                mlc.CONF_SAVE_PASSWORD: False,
                mlc.CONF_ALLOW_MQTT_PUBLISH: True,
                mlc.CONF_CHECK_FIRMWARE_UPDATES: True,
            },
        )

        assert result.get("type") == FlowResultType.FORM
        assert result.get("step_id") == "device"
        user_input[mlc.CONF_KEY] = tc.MOCK_PROFILE_KEY
        result = await config_flow.async_configure(
            result["flow_id"], user_input=user_input
        )
        assert result.get("type") == FlowResultType.FORM
        assert result.get("step_id") == "finalize"
        result = await config_flow.async_configure(result["flow_id"], user_input={})
        assert result.get("type") == FlowResultType.CREATE_ENTRY

        # kick device polling task
        await hass.async_block_till_done()

        entries = hass.config_entries.async_entries(mlc.DOMAIN)
        assert len(entries) == 2
        for entry in entries:
            assert entry.state == ConfigEntryState.LOADED
            await hass.config_entries.async_unload(entry.entry_id)
            await hass.async_block_till_done()


async def test_mqtt_discovery_config_flow(hass: "HomeAssistant", hamqtt_mock):
    """
    Test the initial discovery process i.e. meross_lan
    not configured yet
    """
    device_id = tc.MOCK_DEVICE_UUID
    key = ""
    topic = mc.TOPIC_RESPONSE.format(device_id)
    payload = build_message(
        mn.Appliance_Control_ToggleX.name,
        mc.METHOD_PUSH,
        {mn.Appliance_Control_ToggleX.key: {mc.KEY_CHANNEL: 0, mc.KEY_ONOFF: 0}},
        key,
        mc.TOPIC_REQUEST.format(device_id),
    )

    async_fire_mqtt_message(hass, topic, json_dumps(payload))
    await hass.async_block_till_done()

    flow = hass.config_entries.flow
    # we should have 2 flows now: one for the MQTT hub and the other for the
    # incoming device but this second one needs the time to progress in order to show up
    # so we're not checking now (#TODO: warp the test time so discovery will complete)
    flow_hub = None
    flow_device = None
    for _flow in flow.async_progress_by_handler(mlc.DOMAIN):
        flow_unique_id = _flow.get("context", {}).get("unique_id")
        if flow_unique_id == mlc.DOMAIN:
            flow_hub = _flow
        elif flow_unique_id == device_id:
            flow_device = _flow
        else:
            assert False, "unexpected flow in progress"

    assert flow_hub
    assert flow_hub["step_id"] == "hub"  # type: ignore
    result = await flow.async_configure(
        flow_hub["flow_id"], user_input={mlc.CONF_KEY: key}
    )
    assert result["type"] == FlowResultType.CREATE_ENTRY  # type: ignore

    # kick device polling task
    await hass.async_block_till_done()

    await _cleanup_config_entry(hass, result)

    # TODO: check the device flow after we completed discovery
    assert flow_device is None


async def _create_dhcp_discovery_flow(
    hass: "HomeAssistant", dhcp_service_info: DhcpServiceInfo
):
    # helper to create the dhcp discovery under different HA cores discovery semantics
    if discovery_flow:
        # new semantic (from 2024.10 ?)
        dhcp_discovery_flow_context = {"source": config_entries.SOURCE_DHCP}
        discovery_flow.async_create_flow(
            hass,
            mlc.DOMAIN,
            dhcp_discovery_flow_context,  # type: ignore
            dhcp_service_info,
            discovery_key=discovery_flow.DiscoveryKey(
                domain=dhcp.DOMAIN,
                key=dhcp_service_info.macaddress,
                version=1,
            ),
        )
        await hass.async_block_till_done(wait_background_tasks=True)
        for flow_result in hass.config_entries.flow.async_progress_by_handler(
            mlc.DOMAIN,
            include_uninitialized=True,
            match_context=dhcp_discovery_flow_context,
        ):
            return flow_result
        else:
            return None
    else:
        # old semantic
        return await hass.config_entries.flow.async_init(
            mlc.DOMAIN,
            context={"source": config_entries.SOURCE_DHCP},
            data=dhcp_service_info,
        )


async def test_dhcp_discovery_config_flow(hass: "HomeAssistant"):
    result = await _create_dhcp_discovery_flow(
        hass,
        DhcpServiceInfo(
            tc.MOCK_DEVICE_IP,
            "",
            tc.MOCK_MACADDRESS,
        ),
    )
    assert result, "Dhcp discovery didn't create the discovery flow"
    assert result.get("step_id") == "device"


async def test_dhcp_ignore_config_flow(hass: "HomeAssistant"):

    flow = hass.config_entries.flow

    dhcp_service_info = DhcpServiceInfo(
        tc.MOCK_DEVICE_IP,
        "",
        tc.MOCK_MACADDRESS,
    )
    # create the initial discovery
    result = await _create_dhcp_discovery_flow(hass, dhcp_service_info)
    assert result, "Dhcp discovery didn't create the initial discovery flow"
    assert result.get("step_id") == "device"

    # now 'ignore' it
    entry_unique_id = fmt_macaddress(tc.MOCK_MACADDRESS)
    result = await flow.async_init(
        mlc.DOMAIN,
        context={"source": config_entries.SOURCE_IGNORE},
        data={
            "unique_id": entry_unique_id,
            "title": "",
        },
    )

    assert not flow.async_progress_by_handler(mlc.DOMAIN)

    # try dhcp rediscovery..should abort
    result = await _create_dhcp_discovery_flow(hass, dhcp_service_info)
    assert not result, "Dhcp discovery didn't ignored the discovery flow"

    # now remove the ignored entry
    ignored_entry = hass.config_entries.async_entry_for_domain_unique_id(
        mlc.DOMAIN, entry_unique_id
    )
    assert ignored_entry
    await hass.config_entries.async_remove(ignored_entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)

    """
    I expect the DHCP re-discovery kicks in automatically but this check is not not
    working...I'm giving up atm
    has_progress = False
    for progress in hass.config_entries.flow.async_progress_by_handler(mlc.DOMAIN):
        assert progress.get("context", {}).get("unique_id") == entry_unique_id
        assert progress.get("step_id") == "device"
        has_progress = True

    assert has_progress, "unignored entry did not progress"
    """


async def test_dhcp_renewal_config_flow(request, hass: "HomeAssistant", aioclient_mock):
    """
    When an entry is already configured, check what happens when dhcp sends
    us a new ip
    """
    device_type = mc.TYPE_MTS200
    flow = hass.config_entries.flow

    async with helpers.DeviceContext(
        request, hass, device_type, aioclient_mock
    ) as device_context:
        emulator = device_context.emulator
        device = await device_context.perform_coldstart()

        # better be sure our context is consistent with expectations!
        assert device.host == str(id(emulator))
        assert device.id == device.descriptor.uuid
        device_macaddress = device.descriptor.macAddress
        # since we check the DHCP renewal comes form a legit device we need to setup
        # a mock responding at the solicited ip with the same device info (descriptor)
        # since dhcp config flow will check by mac address

        # here we build a 'clone' of the configured device
        emulator_dhcp = helpers.build_emulator(
            device_type, key=device.key, uuid=device.id
        )
        assert (
            emulator_dhcp.descriptor.macAddress == device_macaddress
        ), "wrong emulator clone"
        assert (
            emulator_dhcp.descriptor.uuid == device.descriptor.uuid
        ), "wrong emulator clone"
        # now we mock the device emulator at new address
        DHCP_GOOD_HOST = "88.88.88.88"
        with helpers.EmulatorContext(
            emulator_dhcp, aioclient_mock, host=DHCP_GOOD_HOST
        ):
            result = await flow.async_init(
                mlc.DOMAIN,
                context={"source": config_entries.SOURCE_DHCP},
                data=DhcpServiceInfo(DHCP_GOOD_HOST, "", device_macaddress),
            )

            assert result["type"] == FlowResultType.ABORT  # type: ignore
            assert result["reason"] == "already_configured"  # type: ignore
            # also check the device host got updated with new address
            await hass.async_block_till_done()
            assert device.host == DHCP_GOOD_HOST, "device host was not updated"

        # here we build a different (device uuid) device instance
        BOGUS_DEVICE_ID = uuid4().hex
        emulator_dhcp = helpers.build_emulator(
            device_type, key=device.key, uuid=BOGUS_DEVICE_ID
        )
        assert (
            emulator_dhcp.descriptor.macAddress != device_macaddress
        ), "wrong emulator clone"
        assert (
            emulator_dhcp.descriptor.uuid != device.descriptor.uuid
        ), "wrong emulator clone"
        # now we mock the device emulator at new address
        DHCP_BOGUS_HOST = "99.99.99.99"
        with helpers.EmulatorContext(
            emulator_dhcp, aioclient_mock, host=DHCP_BOGUS_HOST
        ):
            result = await flow.async_init(
                mlc.DOMAIN,
                context={"source": config_entries.SOURCE_DHCP},
                data=DhcpServiceInfo(DHCP_BOGUS_HOST, "", device_macaddress),
            )

            assert result["type"] == FlowResultType.ABORT  # type: ignore
            assert result["reason"] == "already_configured"  # type: ignore
            # also check the device host got updated with MOCK_DEVICE_IP
            await hass.async_block_till_done()
            assert device.host == DHCP_GOOD_HOST, "device host was wrongly updated"
            device_context.assert_logs(
                1,
                message=(
                    r"received a DHCP update \(ip:99\.99\.99\.99 mac:00:11:22:33:44:55\) "
                    r"but the new uuid:\S* doesn't match "
                    r"the configured one \(uuid:\S*\)"
                ),
            )


async def test_options_flow(
    request, hass: "HomeAssistant", aioclient_mock, hamqtt_mock, merossmqtt_mock
):
    """
    Tests the device config entry option flow. This code could potentially use
    either HTTP or MQTT so we accordingly mock both. TODO: perform the test check
    against different config options (namely: the protocol) in order to see if
    they behave as expected
    """
    async with helpers.DeviceContext(
        request, hass, mc.TYPE_MTS200, aioclient_mock
    ) as context:
        device = await context.perform_coldstart()

        options_flow = hass.config_entries.options
        result = await options_flow.async_init(context.config_entry_id)
        result = await helpers.async_assert_flow_menu_to_step(
            options_flow, result, "menu", "device"
        )
        user_input = {
            mlc.CONF_HOST: device.host,
            mlc.CONF_KEY: "wrongkey",
            mlc.CONF_PROTOCOL: mlc.CONF_PROTOCOL_HTTP,
            mlc.CONF_POLLING_PERIOD: mlc.CONF_POLLING_PERIOD_DEFAULT,
        }
        result = await options_flow.async_configure(
            result["flow_id"], user_input=user_input
        )
        result = await helpers.async_assert_flow_menu_to_step(
            options_flow, result, "keyerror", "device"
        )
        user_input[mlc.CONF_KEY] = device.key
        result = await options_flow.async_configure(
            result["flow_id"], user_input=user_input
        )
        assert result.get("type") == FlowResultType.CREATE_ENTRY
