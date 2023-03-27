"""Test meross_lan config flow"""
import json

from homeassistant import config_entries
from homeassistant.components.dhcp import DhcpServiceInfo
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import async_fire_mqtt_message

from custom_components.meross_lan.const import (
    CONF_DEVICE_ID,
    CONF_HOST,
    CONF_KEY,
    CONF_PAYLOAD,
    DOMAIN,
)
from custom_components.meross_lan.merossclient import build_payload, const as mc

from .const import MOCK_DEVICE_IP, MOCK_MACADDRESS
from .helpers import build_emulator, devicecontext, emulator_mock


async def test_user_config_flow(hass: HomeAssistant, aioclient_mock):
    """
    Test standard manual entry config flow
    """
    with emulator_mock(mc.TYPE_MTS200, aioclient_mock) as emulator:

        device_id = emulator.descriptor.uuid

        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )

        # Check that the config flow shows the user form as the first step
        assert result["type"] == FlowResultType.FORM  # type: ignore
        assert result["step_id"] == "device"  # type: ignore

        # we'll use the configuration of the emulator to reach it
        # through the aioclient_mock
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], user_input={CONF_HOST: device_id, CONF_KEY: emulator.key}
        )

        assert result["type"] == FlowResultType.FORM  # type: ignore
        assert result["step_id"] == "finalize"  # type: ignore

        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], user_input={}
        )

        assert result["type"] == FlowResultType.CREATE_ENTRY  # type: ignore

        data = result["data"]  # type: ignore
        assert data[CONF_DEVICE_ID] == device_id
        assert data[CONF_HOST] == device_id
        assert data[CONF_KEY] == emulator.key
        assert data[CONF_PAYLOAD][mc.KEY_ALL] == emulator.descriptor.all
        assert data[CONF_PAYLOAD][mc.KEY_ABILITY] == emulator.descriptor.ability


async def test_mqtt_discovery_config_flow(hass: HomeAssistant, mqtt_patch):
    """
    Test the initial discovery process i.e. meross_lan
    not configured yet
    """
    emulator = build_emulator(mc.TYPE_MSS310)
    emulator.key = ""  # patch the key so the default hub key will work
    device_id = emulator.descriptor.uuid
    topic = mc.TOPIC_RESPONSE.format(device_id)
    payload = build_payload(
        mc.NS_APPLIANCE_CONTROL_TOGGLEX,
        mc.METHOD_PUSH,
        {mc.KEY_TOGGLEX: {mc.KEY_CHANNEL: 0, mc.KEY_ONOFF: 0}},
        emulator.key,
        mc.TOPIC_REQUEST.format(device_id),
    )

    async_fire_mqtt_message(hass, topic, json.dumps(payload))
    await hass.async_block_till_done()

    # we should have 2 flows now: one for the MQTT hub and the other for the
    # incoming device
    for flow in hass.config_entries.flow.async_progress_by_handler(DOMAIN):
        flow_context = flow.get("context", {})
        if flow_context.get("unique_id") == DOMAIN:
            assert flow["step_id"] == "hub"  # type: ignore
            result = await hass.config_entries.flow.async_configure(
                flow["flow_id"], user_input={CONF_KEY: emulator.key}
            )
            assert result["type"] == FlowResultType.CREATE_ENTRY  # type: ignore
        elif flow_context.get("unique_id") == device_id:
            pass
        else:
            assert False, "unexpected flow in progress"

    await hass.async_block_till_done()


async def test_dhcp_discovery_config_flow(hass: HomeAssistant):

    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_DHCP},
        data=DhcpServiceInfo(MOCK_DEVICE_IP, "", MOCK_MACADDRESS),
    )

    assert result["type"] == FlowResultType.FORM  # type: ignore
    assert result["step_id"] == "device"  # type: ignore


async def test_dhcp_renewal_config_flow(hass: HomeAssistant, aioclient_mock):
    """
    When an entry is already configured, check what happens when dhcp sends
    us a new ip
    """
    async with devicecontext(mc.TYPE_MTS200, hass, aioclient_mock) as context:
        await context.perform_coldstart()

        assert (device := context.device)

        # better be sure our context is consistent with expectations!
        assert device.host == device.device_id

        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": config_entries.SOURCE_DHCP},
            data=DhcpServiceInfo(MOCK_DEVICE_IP, "", device.descriptor.macAddress),
        )

        assert result["type"] == FlowResultType.ABORT  # type: ignore
        assert result["reason"] == "already_configured"  # type: ignore
        # also check the device host got updated with MOCK_DEVICE_IP
        assert device.host == MOCK_DEVICE_IP


async def test_options_flow(hass, aioclient_mock):

    async with devicecontext(mc.TYPE_MTS200, hass, aioclient_mock) as context:
        await context.perform_coldstart()

        assert (device := context.device)

        result = await hass.config_entries.options.async_init(device.entry_id)

        assert result["type"] == FlowResultType.FORM
        assert result["step_id"] == "device"

        result = await hass.config_entries.options.async_configure(
            result["flow_id"],
            user_input={CONF_HOST: device.host, CONF_KEY: "wrongkey"},
        )

        assert result["type"] == FlowResultType.MENU
        assert result["step_id"] == "keyerror"

        result = await hass.config_entries.options.async_configure(
            result["flow_id"],
            user_input={"next_step_id": "device"},
        )

        assert result["type"] == FlowResultType.FORM
        assert result["step_id"] == "device"

        result = await hass.config_entries.options.async_configure(
            result["flow_id"],
            user_input={CONF_HOST: device.host, CONF_KEY: device.key},
        )

        assert result["type"] == FlowResultType.CREATE_ENTRY
