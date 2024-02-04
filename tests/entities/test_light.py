from homeassistant import const as hac
from homeassistant.components.light import (
    ATTR_BRIGHTNESS,
    ATTR_COLOR_TEMP,
    ATTR_RGB_COLOR,
    DOMAIN,
    SERVICE_TURN_OFF,
    SERVICE_TURN_ON,
    ColorMode,
    LightEntity,
    LightEntityFeature,
)
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.meross_lan.devices.mod100 import MLDiffuserLight
from custom_components.meross_lan.light import (
    MLDNDLightEntity,
    MLLight,
    MLLightBase,
    _int_to_rgb,
    _rgb_to_int,
)
from custom_components.meross_lan.merossclient import const as mc
from emulator import generate_emulators

from tests import const as tc, helpers


async def test_light_entities(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker):

    call_service = hass.services.async_call

    for emulator in generate_emulators(
        tc.EMULATOR_TRACES_PATH, tc.MOCK_DEVICE_UUID, tc.MOCK_KEY
    ):
        descriptor = emulator.descriptor
        ability = descriptor.ability
        digest = descriptor.digest

        if not ((mc.KEY_LIGHT in digest) or (mc.KEY_DIFFUSER in digest)):
            # this way we're testing MLDNDLightEntity only
            # in devices carrying other light entities but that's
            # enough since DND is already tested somehow
            # and, from the Light entity point of view, it's enough to test just once
            continue

        async with helpers.DeviceContext(hass, emulator, aioclient_mock) as context:
            device = await context.perform_coldstart()
            entities = device.managed_entities(DOMAIN)
            assert entities
            for entity in entities:

                entity_id = entity.entity_id
                assert isinstance(entity, LightEntity)
                supported_color_modes = entity.supported_color_modes
                supported_features = entity.supported_features
                assert supported_color_modes

                if isinstance(entity, MLDNDLightEntity):
                    # special light here with reduced set of features
                    assert entity is device.entity_dnd
                    assert supported_color_modes == {ColorMode.ONOFF}
                else:
                    # check the other specialized implementations
                    if mc.NS_APPLIANCE_CONTROL_DIFFUSER_LIGHT in ability:
                        assert isinstance(entity, MLDiffuserLight)
                        assert supported_color_modes == {ColorMode.RGB}
                        assert supported_features == LightEntityFeature.EFFECT

                    if mc.NS_APPLIANCE_CONTROL_LIGHT in ability:
                        assert isinstance(entity, MLLight)
                        capacity = ability[mc.NS_APPLIANCE_CONTROL_LIGHT][mc.KEY_CAPACITY]
                        if capacity & mc.LIGHT_CAPACITY_RGB:
                            assert ColorMode.RGB in supported_color_modes
                        if capacity & mc.LIGHT_CAPACITY_TEMPERATURE:
                            assert ColorMode.COLOR_TEMP in supported_color_modes

                    if mc.NS_APPLIANCE_CONTROL_LIGHT_EFFECT in ability:
                        assert supported_features == LightEntityFeature.EFFECT

                if not entity.available:
                    # skip entities which are not available in emulator (warning though)
                    continue

                if entity._hass_connected:

                    await call_service(
                        DOMAIN,
                        SERVICE_TURN_OFF,
                        service_data={
                            "entity_id": entity_id,
                        },
                        blocking=True,
                    )
                    assert (state := hass.states.get(entity_id))
                    assert state.state == hac.STATE_OFF
                    await call_service(
                        DOMAIN,
                        SERVICE_TURN_ON,
                        service_data={
                            "entity_id": entity_id,
                        },
                        blocking=True,
                    )
                    assert (state := hass.states.get(entity_id))
                    assert state.state == hac.STATE_ON

                    if entity is device.entity_dnd:
                        continue
                    assert isinstance(entity, MLLightBase)

                    if ColorMode.BRIGHTNESS in supported_color_modes:
                        await call_service(
                            DOMAIN,
                            SERVICE_TURN_ON,
                            service_data={
                                ATTR_BRIGHTNESS: 1,
                                "entity_id": entity_id,
                            },
                            blocking=True,
                        )
                        assert (state := hass.states.get(entity_id))
                        assert (
                            state.state == hac.STATE_ON
                            and state.attributes[ATTR_BRIGHTNESS] == (255 // 100)
                            and entity._light[mc.KEY_LUMINANCE] == 1
                        )
                        await call_service(
                            DOMAIN,
                            SERVICE_TURN_ON,
                            service_data={
                                ATTR_BRIGHTNESS: 255,
                                "entity_id": entity_id,
                            },
                            blocking=True,
                        )
                        assert (state := hass.states.get(entity_id))
                        assert (
                            state.state == hac.STATE_ON
                            and state.attributes[ATTR_BRIGHTNESS] == 255
                            and entity._light[mc.KEY_LUMINANCE] == 100
                        )

                    if ColorMode.RGB in supported_color_modes:
                        rgb_tuple = (255, 0, 0)
                        rgb_meross = _rgb_to_int(rgb_tuple)
                        await call_service(
                            DOMAIN,
                            SERVICE_TURN_ON,
                            service_data={
                                ATTR_RGB_COLOR: rgb_tuple,
                                "entity_id": entity_id,
                            },
                            blocking=True,
                        )
                        assert (state := hass.states.get(entity_id))
                        assert (
                            state.state == hac.STATE_ON
                            and state.attributes[ATTR_RGB_COLOR]
                            == _int_to_rgb(rgb_meross)
                            and entity._light[mc.KEY_RGB] == rgb_meross
                        )

                    if ColorMode.COLOR_TEMP in supported_color_modes:
                        MIREDS_TO_MEROSS_TEMP = {
                            entity.min_mireds: 100,
                            entity.max_mireds: 1,
                        }
                        for temp_mired, temp_meross in MIREDS_TO_MEROSS_TEMP.items():
                            await call_service(
                                DOMAIN,
                                SERVICE_TURN_ON,
                                service_data={
                                    ATTR_COLOR_TEMP: temp_mired,
                                    "entity_id": entity_id,
                                },
                                blocking=True,
                            )
                            assert (state := hass.states.get(entity_id))
                            assert (
                                state.state == hac.STATE_ON
                                and state.attributes[ATTR_COLOR_TEMP] == temp_mired
                                and entity._light[mc.KEY_TEMPERATURE] == temp_meross
                            )

