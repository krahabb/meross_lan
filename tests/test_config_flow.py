"""Test meross_lan config flow"""

from homeassistant import config_entries, data_entry_flow
from homeassistant.core import HomeAssistant

from custom_components.meross_lan.const import (
    CONF_DEVICE_ID,
    CONF_HOST,
    CONF_KEY,
    CONF_PAYLOAD,
    DOMAIN,
)
from custom_components.meross_lan.merossclient import const as mc

from .helpers import emulator_mock


async def test_user_config_flow(hass: HomeAssistant, aioclient_mock):

    with emulator_mock('mts200', aioclient_mock) as emulator:
        #test user config-flow when no mqtt available
        device_id = emulator.descriptor.uuid

        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={'source': config_entries.SOURCE_USER}
        )

        # Check that the config flow shows the user form as the first step
        assert result['type'] == data_entry_flow.RESULT_TYPE_FORM # type: ignore
        assert result['step_id'] == 'device' # type: ignore

        # we'll use the configuration of the emulator to reach it
        # through the aioclient_mock
        result = await hass.config_entries.flow.async_configure(
            result['flow_id'], user_input={
                CONF_HOST: device_id,
                CONF_KEY: emulator.key
            }
        )

        assert result['type'] == data_entry_flow.RESULT_TYPE_FORM # type: ignore
        assert result['step_id'] == 'finalize' # type: ignore

        result = await hass.config_entries.flow.async_configure(
            result['flow_id'], user_input={}
        )

        assert result['type'] == data_entry_flow.RESULT_TYPE_CREATE_ENTRY # type: ignore

        data = result['data'] # type: ignore
        assert data[CONF_DEVICE_ID] == device_id
        assert data[CONF_HOST] == device_id
        assert data[CONF_KEY] == emulator.key
        assert data[CONF_PAYLOAD][mc.KEY_ALL] == emulator.descriptor.all
        assert data[CONF_PAYLOAD][mc.KEY_ABILITY] == emulator.descriptor.ability