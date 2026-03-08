from homeassistant import const as hac
from homeassistant.components import light as haec
from homeassistant.components.light import ColorMode, LightEntity, LightEntityFeature

from custom_components.meross_lan.devices.diffuser import DiffuserLight
from custom_components.meross_lan.light import (
    DNDLight,
    EffectLight,
    Light,
    LightBase,
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
        mc.KEY_LIGHT: [Light],
        mc.KEY_DIFFUSER: {mc.KEY_LIGHT: [DiffuserLight]},
    }
    NAMESPACES_ENTITIES = {
        mn.Appliance_Control_Light_Effect: [EffectLight],
        mn.Appliance_Control_Mp3: [Light],
        mn.Appliance_System_DNDMode: [DNDLight],
    }

    async def async_test_each_callback(
        self,
        entity: Light | DiffuserLight | DNDLight,
    ):
        await super().async_test_each_callback(entity)

        supported_color_modes = entity.supported_color_modes
        supported_features = entity.supported_features

        if isinstance(entity, DNDLight):
            # special light here with reduced set of features
            assert supported_color_modes == {ColorMode.ONOFF}, "supported_color_modes"
        else:
            ability = self.ability
            self._check_remove_togglex(entity)
            # check the other specialized implementations
            if mn.Appliance_Control_Diffuser_Light in ability:
                assert isinstance(entity, DiffuserLight)
                assert ColorMode.RGB in supported_color_modes, "supported_color_modes"
                assert LightEntityFeature.EFFECT in supported_features
                assert entity.effect_list == mc.DIFFUSER_LIGHT_MODE_LIST, "effect_list"
            if mn.Appliance_Control_Light in ability:
                assert isinstance(entity, Light)
                capacity = ability[mn.Appliance_Control_Light][mc.KEY_CAPACITY]
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
                if mn.Appliance_Control_Light_Effect in ability:
                    assert type(entity) is EffectLight
                    assert LightEntityFeature.EFFECT in supported_features
                    assert entity.effect_list, "effect_list"
                    # need to manually remove Light instance since it's also requested in digest
                    EntityComponentTest.expected_entity_types.remove(Light)
                if mn.Appliance_Control_Mp3 in ability:
                    assert LightEntityFeature.EFFECT in supported_features
                    assert (
                        entity.effect_list == mc.HP110A_LIGHT_EFFECT_LIST
                    ), "effect_list"
                    # need to manually remove Light instance since it's also requested in digest
                    EntityComponentTest.expected_entity_types.remove(Light)

    async def async_test_enabled_callback(
        self, entity: Light | DiffuserLight | DNDLight
    ):
        await self.async_service_call_check(haec.SERVICE_TURN_OFF, hac.STATE_OFF)
        await self.async_service_call_check(haec.SERVICE_TURN_ON, hac.STATE_ON)

        if entity.entitykey == DNDLight.ENTITY_KEY:
            return
        assert isinstance(entity, LightBase)
        supported_color_modes = entity.supported_color_modes

        check_brightness = ColorMode.BRIGHTNESS in supported_color_modes

        if ColorMode.RGB in supported_color_modes:
            check_brightness = True
            rgb_tuple = (255, 0, 0)
            rgb_meross = rgb_to_native(rgb_tuple)
            state = await self.async_service_call_check(
                haec.SERVICE_TURN_ON,
                hac.STATE_ON,
                {haec.ATTR_RGB_COLOR: rgb_tuple},
            )
            assert (
                state.attributes[haec.ATTR_RGB_COLOR] == native_to_rgb(rgb_meross)
                and entity.ns_payload[mc.KEY_RGB] == rgb_meross
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
                    and entity.ns_payload[mc.KEY_TEMPERATURE] == temperature
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
                    and entity.ns_payload[mc.KEY_LUMINANCE] == luminance
                ), "brightness_to_native"

    async def async_test_disabled_callback(
        self,
        entity: Light | DiffuserLight | DNDLight,
    ):
        await entity.async_turn_on()
        assert entity.is_on
        await entity.async_turn_off()
        assert not entity.is_on
