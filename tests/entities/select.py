from homeassistant.components.select import (
    ATTR_OPTION,
    DOMAIN,
    SERVICE_SELECT_OPTION,
    SelectEntity,
)

from custom_components.meross_lan.merossclient import const as mc
from custom_components.meross_lan.select import MLSpray, MtsTrackedSensor

from tests.entities import EntityComponentTest, EntityTestContext


class EntityTest(EntityComponentTest):

    ENTITY_TYPE = SelectEntity

    DIGEST_ENTITIES = {
        mc.KEY_THERMOSTAT: {MtsTrackedSensor},
        mc.KEY_SPRAY: {MLSpray},
        mc.KEY_DIFFUSER: {MLSpray},
    }

    HUB_SUBDEVICES_ENTITIES = {
        mc.TYPE_MTS100: {MtsTrackedSensor},
        mc.TYPE_MTS150: {MtsTrackedSensor},
    }

    async def async_test_each_callback(
        self, context: EntityTestContext, entity: SelectEntity
    ):
        pass

    async def async_test_enabled_callback(
        self, context: EntityTestContext, entity: SelectEntity, entity_id: str
    ):
        hass = context.hass
        call_service = hass.services.async_call
        states = hass.states

        for option in entity.options:
            await call_service(
                DOMAIN,
                SERVICE_SELECT_OPTION,
                service_data={
                    ATTR_OPTION: option,
                    "entity_id": entity_id,
                },
                blocking=True,
            )
            state = states.get(entity_id)
            assert state and state.state == option

    async def async_test_disabled_callback(
        self, context: EntityTestContext, entity: SelectEntity
    ):
        for option in entity.options:
            await entity.async_select_option(option)
            assert entity.state == option


"""
DIGEST_TO_ENTITY_CLASS = {
    mc.KEY_HUB: (MtsTrackedSensor, 0),
    mc.KEY_THERMOSTAT: (MtsTrackedSensor, 0),
    mc.KEY_SPRAY: (MLSpray, 3),
    mc.KEY_DIFFUSER: (MLSpray, 3),
}


async def test_select_entities(hass: HomeAssistant, aioclient_mock):
    call_service = hass.services.async_call

    for emulator in generate_emulators(
        tc.EMULATOR_TRACES_PATH, tc.MOCK_DEVICE_UUID, tc.MOCK_KEY
    ):
        descriptor = emulator.descriptor
        ability = descriptor.ability
        digest = descriptor.digest

        for key, def_tuple in DIGEST_TO_ENTITY_CLASS.items():
            if key in digest:
                classtype = def_tuple[0]
                options_count = def_tuple[1]
                break
        else:
            continue

        async with helpers.DeviceContext(hass, emulator, aioclient_mock) as context:
            device = await context.perform_coldstart()
            entities = device.managed_entities(DOMAIN)
            for entity in entities:
                assert isinstance(entity, SelectEntity)
                entity_id = entity.entity_id

                options = entity.options
                assert isinstance(entity, classtype)
                assert len(options) >= options_count

                state = hass.states.get(entity_id)
                if state:
                    assert entity._hass_connected
                    if not entity.available:
                        # skip entities which are not available in emulator (warning though)
                        assert state.state == STATE_UNAVAILABLE
                        continue

                    for option in options:
                        await call_service(
                            DOMAIN,
                            SERVICE_SELECT_OPTION,
                            service_data={
                                ATTR_OPTION: option,
                                "entity_id": entity_id,
                            },
                            blocking=True,
                        )
                        state = hass.states.get(entity_id)
                        assert state and state.state == option

                else:
                    # entity not loaded in HA so we just test
                    # the Meross internal interface
                    assert not entity._hass_connected
                    if not entity.available:
                        continue

                    for option in options:
                        await entity.async_select_option(option)
                        assert entity.state == option
"""
