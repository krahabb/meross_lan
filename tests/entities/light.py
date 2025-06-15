from homeassistant import const as hac
from homeassistant.components import light as haec
from homeassistant.components.light import ColorMode, LightEntity, LightEntityFeature

from custom_components.meross_lan import const as mlc
from custom_components.meross_lan.devices.diffuser import MLDiffuserLight
from custom_components.meross_lan.light import (
    MLDNDLightEntity,
    MLLight,
    MLLightBase,
    MLLightEffect,
    MLLightMp3,
    native_to_rgb,
    rgb_to_native,
)
from custom_components.meross_lan.merossclient.protocol import (
    const as mc,
    namespaces as mn,
)

from tests.entities import EntityComponentTest


class EntityTest(EntityComponentTest):

    ENTITY_TYPE = LightEntity

    DIGEST_ENTITIES = {
        mc.KEY_LIGHT: [MLLight],
        mc.KEY_DIFFUSER: {mc.KEY_LIGHT: [MLDiffuserLight]},
    }
    NAMESPACES_ENTITIES = {
        mn.Appliance_Control_Light_Effect.name: [MLLightEffect],
        mn.Appliance_Control_Mp3.name: [MLLightMp3],
        mn.Appliance_System_DNDMode.name: [MLDNDLightEntity],
    }

    async def async_test_each_callback(
        self,
        entity: MLLight | MLDiffuserLight | MLDNDLightEntity,
    ):
        await super().async_test_each_callback(entity)

        supported_color_modes = entity.supported_color_modes
        supported_features = entity.supported_features

        if isinstance(entity, MLDNDLightEntity):
            # special light here with reduced set of features
            assert supported_color_modes == {ColorMode.ONOFF}, "supported_color_modes"
        else:
            ability = self.ability
            self._check_remove_togglex(entity)
            # check the other specialized implementations
            if mn.Appliance_Control_Diffuser_Light.name in ability:
                assert isinstance(entity, MLDiffuserLight)
                assert ColorMode.RGB in supported_color_modes, "supported_color_modes"
                assert LightEntityFeature.EFFECT in supported_features
                assert entity.effect_list == mc.DIFFUSER_LIGHT_MODE_LIST, "effect_list"
            if mn.Appliance_Control_Light.name in ability:
                assert isinstance(entity, MLLight)
                # need to manually remove MLLight since actual is rather polymorphic
                # and the general code in _async_test_entities cannot handle this case
                if type(entity) is not MLLight:
                    EntityComponentTest.expected_entity_types.remove(MLLight)
                capacity = ability[mn.Appliance_Control_Light.name][mc.KEY_CAPACITY]
                if capacity & mc.LIGHT_CAPACITY_RGB:
                    assert (
                        ColorMode.RGB in supported_color_modes
                    ), "supported_color_modes"
                if capacity & mc.LIGHT_CAPACITY_TEMPERATURE:
                    assert (
                        ColorMode.COLOR_TEMP in supported_color_modes
                    ), "supported_color_modes"
                if capacity & mc.LIGHT_CAPACITY_EFFECT:
                    assert LightEntityFeature.EFFECT in supported_features
                    assert entity.effect_list, "effect_list"
                if mn.Appliance_Control_Light_Effect.name in ability:
                    assert type(entity) is MLLightEffect
                    assert LightEntityFeature.EFFECT in supported_features
                    assert entity.effect_list, "effect_list"
                if mn.Appliance_Control_Mp3.name in ability:
                    assert type(entity) is MLLightMp3
                    assert LightEntityFeature.EFFECT in supported_features
                    assert (
                        entity.effect_list == mc.HP110A_LIGHT_EFFECT_LIST
                    ), "effect_list"

    async def async_test_enabled_callback(
        self, entity: MLLight | MLDiffuserLight | MLDNDLightEntity
    ):
        await self.async_service_call_check(haec.SERVICE_TURN_OFF, hac.STATE_OFF)
        await self.async_service_call_check(haec.SERVICE_TURN_ON, hac.STATE_ON)

        if entity.entitykey == mlc.DND_ID:
            return
        assert isinstance(entity, MLLightBase)
        supported_color_modes = entity.supported_color_modes

        check_brightness = False
        if ColorMode.BRIGHTNESS in supported_color_modes:
            check_brightness = True

        if ColorMode.RGB in supported_color_modes:
            check_brightness = True
            rgb_tuple = (255, 0, 0)
            rgb_meross = rgb_to_native(rgb_tuple)
            state = await self.async_service_call_check(
                haec.SERVICE_TURN_ON, hac.STATE_ON, {haec.ATTR_RGB_COLOR: rgb_tuple}
            )
            assert (
                state.attributes[haec.ATTR_RGB_COLOR] == native_to_rgb(rgb_meross)
                and entity._light[mc.KEY_RGB] == rgb_meross
            ), "rgb_to_native"

        if ColorMode.COLOR_TEMP in supported_color_modes:
            check_brightness = True
            KELVIN_TO_TEMPERATURE = {
                entity.min_color_temp_kelvin: 1,
                entity.max_color_temp_kelvin: 100,
            }
            for kelvin, temperature in KELVIN_TO_TEMPERATURE.items():
                state = await self.async_service_call_check(
                    haec.SERVICE_TURN_ON,
                    hac.STATE_ON,
                    {haec.ATTR_COLOR_TEMP_KELVIN: kelvin},
                )
                assert (
                    state.attributes[haec.ATTR_COLOR_TEMP_KELVIN] == kelvin
                    and entity._light[mc.KEY_TEMPERATURE] == temperature
                ), "kelvin_to_native"

        if check_brightness:
            BRIGHTNESS_TO_LUMINANCE = {
                3: 1,
                255: 100,
            }
            for brightness, luminance in BRIGHTNESS_TO_LUMINANCE.items():
                state = await self.async_service_call_check(
                    haec.SERVICE_TURN_ON,
                    hac.STATE_ON,
                    {haec.ATTR_BRIGHTNESS: brightness},
                )
                assert (
                    state.attributes[haec.ATTR_BRIGHTNESS] == brightness
                    and entity._light[mc.KEY_LUMINANCE] == luminance
                ), "brightness_to_native"

    async def async_test_disabled_callback(
        self,
        entity: MLLight | MLDiffuserLight | MLDNDLightEntity,
    ):
        await entity.async_turn_on()
        assert entity.is_on
        await entity.async_turn_off()
        assert not entity.is_on
