from homeassistant.components.switch import (
    DOMAIN,
    SERVICE_TURN_OFF,
    SERVICE_TURN_ON,
    SwitchEntity,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import (
    STATE_OFF,
    STATE_ON,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
)
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.meross_lan.merossclient import const as mc
from custom_components.meross_lan.switch import MLSwitch
from emulator import generate_emulators

from tests import const as tc, helpers


async def test_switch_entities(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
):
    call_service = hass.services.async_call

    for emulator in generate_emulators(
        tc.EMULATOR_TRACES_PATH, tc.MOCK_DEVICE_UUID, tc.MOCK_KEY
    ):
        descriptor = emulator.descriptor
        ability = descriptor.ability
        digest = descriptor.digest

        async with helpers.DeviceContext(hass, emulator, aioclient_mock) as context:
            device = await context.perform_coldstart()
            entities = device.managed_entities(DOMAIN)
            for entity in entities:
                entity_id = entity.entity_id
                state = hass.states.get(entity_id)
                if state:
                    assert entity._hass_connected
                    if not entity.available:
                        # skip entities which are not available in emulator (warning though)
                        assert state.state == STATE_UNAVAILABLE
                        continue

                    await call_service(
                        DOMAIN,
                        SERVICE_TURN_ON,
                        service_data={
                            "entity_id": entity_id,
                        },
                        blocking=True,
                    )
                    state = hass.states.get(entity_id)
                    assert state and state.state == STATE_ON
                    await call_service(
                        DOMAIN,
                        SERVICE_TURN_OFF,
                        service_data={
                            "entity_id": entity_id,
                        },
                        blocking=True,
                    )
                    state = hass.states.get(entity_id)
                    assert state and state.state == STATE_OFF

                else:
                    # entity not loaded in HA so we just test
                    # the Meross internal interface
                    assert isinstance(entity, MLSwitch)
                    assert not entity._hass_connected
                    if not entity.available:
                        continue

                    await entity.async_turn_on()
                    assert entity.is_on
                    await entity.async_turn_off()
                    assert not entity.is_on
