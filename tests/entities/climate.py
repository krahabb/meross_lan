from homeassistant.components.climate import (
    ATTR_HVAC_MODE,
    ATTR_PRESET_MODE,
    DOMAIN,
    SERVICE_SET_HVAC_MODE,
    SERVICE_SET_PRESET_MODE,
    SERVICE_TURN_OFF,
    ClimateEntity,
    ClimateEntityFeature,
    HVACAction,
    HVACMode,
)

from custom_components.meross_lan.climate import MtsClimate
from custom_components.meross_lan.devices.mts100 import Mts100Climate
from custom_components.meross_lan.devices.mts200 import Mts200Climate
from custom_components.meross_lan.devices.mts960 import Mts960Climate
from custom_components.meross_lan.merossclient import const as mc

from tests.entities import EntityComponentTest, EntityTestContext

HVAC_MODES: dict[type[MtsClimate], set[HVACMode]] = {
    Mts100Climate: {HVACMode.OFF, HVACMode.HEAT},
    Mts200Climate: {HVACMode.OFF, HVACMode.HEAT},
    Mts960Climate: {HVACMode.OFF, HVACMode.HEAT, HVACMode.COOL, HVACMode.AUTO},
}

PRESET_MODES: dict[type[MtsClimate], set] = {
    Mts100Climate: {
        mc.MTS100_MODE_CUSTOM,
        mc.MTS100_MODE_HEAT,
        mc.MTS100_MODE_COOL,
        mc.MTS100_MODE_ECO,
        mc.MTS100_MODE_AUTO,
    },
    Mts200Climate: {
        mc.MTS200_MODE_HEAT,
        mc.MTS200_MODE_COOL,
        mc.MTS200_MODE_ECO,
        mc.MTS200_MODE_AUTO,
        mc.MTS200_MODE_CUSTOM,
    },
    Mts960Climate: {
        mc.MTS960_MODE_HEAT,
        mc.MTS960_MODE_COOL,
        mc.MTS960_MODE_CYCLE,
        mc.MTS960_MODE_COUNTDOWN_ON,
        mc.MTS960_MODE_COUNTDOWN_OFF,
        mc.MTS960_MODE_SCHEDULE_HEAT,
        mc.MTS960_MODE_SCHEDULE_COOL,
    },
}


class EntityTest(EntityComponentTest):

    ENTITY_TYPE = ClimateEntity

    DIGEST_ENTITIES = {
        # mc.KEY_THERMOSTAT: {MLGarage},
    }

    NAMESPACES_ENTITIES = {}

    async def async_test_each_callback(
        self, context: EntityTestContext, entity: MtsClimate
    ):
        entity_hvac_modes = set(entity.hvac_modes)
        expected_hvac_modes = HVAC_MODES[entity.__class__]
        assert expected_hvac_modes.issubset(entity_hvac_modes)
        if mc.NS_APPLIANCE_CONTROL_THERMOSTAT_SUMMERMODE in context.ability:
            assert HVACMode.COOL in entity_hvac_modes

        entity_preset_modes = set(entity.preset_modes)
        expected_preset_modes = {
            entity.MTS_MODE_TO_PRESET_MAP[mts_mode]
            for mts_mode in PRESET_MODES[entity.__class__]
        }
        assert expected_preset_modes == entity_preset_modes

    async def async_test_enabled_callback(
        self, context: EntityTestContext, entity: MtsClimate, entity_id: str
    ):
        hass = context.hass
        call_service = hass.services.async_call
        states = hass.states
        for hvac_mode in entity.hvac_modes:
            await call_service(
                DOMAIN,
                SERVICE_SET_HVAC_MODE,
                service_data={
                    ATTR_HVAC_MODE: hvac_mode,
                    "entity_id": entity_id,
                },
                blocking=True,
            )
            assert (state := states.get(entity_id))
            assert state.state == hvac_mode

        for preset_mode in entity.preset_modes:
            await call_service(
                DOMAIN,
                SERVICE_SET_PRESET_MODE,
                service_data={
                    ATTR_PRESET_MODE: preset_mode,
                    "entity_id": entity_id,
                },
                blocking=True,
            )
            assert (state := states.get(entity_id))
            assert state.attributes[ATTR_PRESET_MODE] == preset_mode

        await call_service(
            DOMAIN,
            SERVICE_TURN_OFF,
            service_data={
                "entity_id": entity_id,
            },
            blocking=True,
        )
        assert (state := states.get(entity_id))
        assert state.state == HVACMode.OFF

    async def async_test_disabled_callback(
        self, context: EntityTestContext, entity: ClimateEntity
    ):
        pass

"""
async def test_climate_entities(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
):
    call_service = hass.services.async_call

    for emulator in generate_emulators(
        tc.EMULATOR_TRACES_PATH, tc.MOCK_DEVICE_UUID, tc.MOCK_KEY
    ):
        descriptor = emulator.descriptor
        ability = descriptor.ability
        digest = descriptor.digest

        if not ((mc.KEY_THERMOSTAT in digest) or (mc.KEY_HUB in digest)):
            continue

        async with helpers.DeviceContext(hass, emulator, aioclient_mock) as context:
            device = await context.perform_coldstart()
            entities = device.managed_entities(DOMAIN)
            if mc.KEY_THERMOSTAT in digest:
                assert entities
            for entity in entities:

                assert isinstance(entity, MtsClimate)

                entity_hvac_modes = set(entity.hvac_modes)
                expected_hvac_modes = HVAC_MODES[entity.__class__]
                assert expected_hvac_modes.issubset(entity_hvac_modes)
                if mc.NS_APPLIANCE_CONTROL_THERMOSTAT_SUMMERMODE in ability:
                    assert HVACMode.COOL in entity_hvac_modes

                entity_preset_modes = set(entity.preset_modes)
                expected_preset_modes = {
                    entity.MTS_MODE_TO_PRESET_MAP[mts_mode]
                    for mts_mode in PRESET_MODES[entity.__class__]
                }
                assert expected_preset_modes == entity_preset_modes

                if not entity.available:
                    # skip entities which are not available in emulator (warning though)
                    continue

                if entity._hass_connected:
                    entity_id = entity.entity_id

                    for hvac_mode in entity_hvac_modes:
                        await call_service(
                            DOMAIN,
                            SERVICE_SET_HVAC_MODE,
                            service_data={
                                ATTR_HVAC_MODE: hvac_mode,
                                "entity_id": entity_id,
                            },
                            blocking=True,
                        )
                        assert (state := hass.states.get(entity_id))
                        assert state.state == hvac_mode

                    for preset_mode in entity_preset_modes:
                        await call_service(
                            DOMAIN,
                            SERVICE_SET_PRESET_MODE,
                            service_data={
                                ATTR_PRESET_MODE: preset_mode,
                                "entity_id": entity_id,
                            },
                            blocking=True,
                        )
                        assert (state := hass.states.get(entity_id))
                        assert state.attributes[ATTR_PRESET_MODE] == preset_mode

                    await call_service(
                        DOMAIN,
                        SERVICE_TURN_OFF,
                        service_data={
                            "entity_id": entity_id,
                        },
                        blocking=True,
                    )
                    state = hass.states.get(entity_id)
                    assert state and state.state == HVACMode.OFF
"""