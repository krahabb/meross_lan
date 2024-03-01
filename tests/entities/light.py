from homeassistant import const as hac
from homeassistant.components import light as haec
from homeassistant.components.light import ColorMode, LightEntity, LightEntityFeature

from custom_components.meross_lan import const as mlc
from custom_components.meross_lan.devices.diffuser import MLDiffuserLight
from custom_components.meross_lan.light import (
    MLDNDLightEntity,
    MLLight,
    MLLightBase,
    _int_to_rgb,
    _rgb_to_int,
)
from custom_components.meross_lan.merossclient import const as mc
from custom_components.meross_lan.switch import MLSwitch

from tests.entities import EntityComponentTest


class EntityTest(EntityComponentTest):

    ENTITY_TYPE = LightEntity

    DIGEST_ENTITIES = {
        mc.KEY_LIGHT: [MLLight],
        mc.KEY_DIFFUSER: {mc.KEY_LIGHT: [MLDiffuserLight]},
    }
    NAMESPACES_ENTITIES = {
        mc.NS_APPLIANCE_SYSTEM_DNDMODE: [MLDNDLightEntity],
    }

    async def async_test_each_callback(
        self,
        entity: MLLight | MLDiffuserLight | MLDNDLightEntity,
    ):
        supported_color_modes = entity.supported_color_modes
        supported_features = entity.supported_features

        if isinstance(entity, MLDNDLightEntity):
            # special light here with reduced set of features
            assert supported_color_modes == {ColorMode.ONOFF}, "supported_color_modes"
        else:
            ability = self.ability
            if mc.NS_APPLIANCE_CONTROL_TOGGLEX in ability:
                if MLSwitch in EntityComponentTest.expected_entity_types:
                    EntityComponentTest.expected_entity_types.remove(MLSwitch)
            # check the other specialized implementations
            if mc.NS_APPLIANCE_CONTROL_DIFFUSER_LIGHT in ability:
                assert isinstance(entity, MLDiffuserLight)
                assert supported_color_modes == {ColorMode.RGB}, "supported_color_modes"
                assert supported_features == LightEntityFeature.EFFECT
                assert entity.effect_list == list(mc.DIFFUSER_LIGHT_EFFECT_MAP.values()), "effect_list"
            if mc.NS_APPLIANCE_CONTROL_LIGHT in ability:
                assert isinstance(entity, MLLight)
                capacity = ability[mc.NS_APPLIANCE_CONTROL_LIGHT][mc.KEY_CAPACITY]
                if capacity & mc.LIGHT_CAPACITY_RGB:
                    assert ColorMode.RGB in supported_color_modes, "supported_color_modes"
                if capacity & mc.LIGHT_CAPACITY_TEMPERATURE:
                    assert ColorMode.COLOR_TEMP in supported_color_modes, "supported_color_modes"
                if capacity & mc.LIGHT_CAPACITY_EFFECT:
                    assert supported_features == LightEntityFeature.EFFECT
                    assert entity.effect_list, "effect_list"
            if mc.NS_APPLIANCE_CONTROL_LIGHT_EFFECT in ability:
                assert supported_features == LightEntityFeature.EFFECT
                assert entity.effect_list, "effect_list"
            if mc.NS_APPLIANCE_CONTROL_MP3 in ability:
                assert isinstance(entity, MLLight)
                assert supported_features == LightEntityFeature.EFFECT
                assert entity.effect_list == list(mc.HP110A_LIGHT_EFFECT_MAP.values()), "effect_list"

    async def async_test_enabled_callback(
        self, entity: MLLight | MLDiffuserLight | MLDNDLightEntity
    ):
        await self.async_service_call_check(haec.SERVICE_TURN_OFF, hac.STATE_OFF)
        await self.async_service_call_check(haec.SERVICE_TURN_ON, hac.STATE_ON)

        if entity.entitykey == mlc.DND_ID:
            return
        assert isinstance(entity, MLLightBase)
        supported_color_modes = entity.supported_color_modes

        if ColorMode.BRIGHTNESS in supported_color_modes:
            state = await self.async_service_call_check(
                haec.SERVICE_TURN_ON, hac.STATE_ON, {haec.ATTR_BRIGHTNESS: 1}
            )
            assert (
                state.attributes[haec.ATTR_BRIGHTNESS] == (255 // 100)
                and entity._light[mc.KEY_LUMINANCE] == 1
            )
            state = await self.async_service_call_check(
                haec.SERVICE_TURN_ON, hac.STATE_ON, {haec.ATTR_BRIGHTNESS: 255}
            )
            assert (
                state.attributes[haec.ATTR_BRIGHTNESS] == 255
                and entity._light[mc.KEY_LUMINANCE] == 100
            )

        if ColorMode.RGB in supported_color_modes:
            rgb_tuple = (255, 0, 0)
            rgb_meross = _rgb_to_int(rgb_tuple)
            state = await self.async_service_call_check(
                haec.SERVICE_TURN_ON, hac.STATE_ON, {haec.ATTR_RGB_COLOR: rgb_tuple}
            )
            assert (
                state.attributes[haec.ATTR_RGB_COLOR] == _int_to_rgb(rgb_meross)
                and entity._light[mc.KEY_RGB] == rgb_meross
            )

        if ColorMode.COLOR_TEMP in supported_color_modes:
            MIREDS_TO_MEROSS_TEMP = {
                entity.min_mireds: 100,
                entity.max_mireds: 1,
            }
            for temp_mired, temp_meross in MIREDS_TO_MEROSS_TEMP.items():
                state = await self.async_service_call_check(
                    haec.SERVICE_TURN_ON,
                    hac.STATE_ON,
                    {haec.ATTR_COLOR_TEMP: temp_mired},
                )
                assert (
                    state.attributes[haec.ATTR_COLOR_TEMP] == temp_mired
                    and entity._light[mc.KEY_TEMPERATURE] == temp_meross
                )

    async def async_test_disabled_callback(
        self,
        entity: MLLight | MLDiffuserLight | MLDNDLightEntity,
    ):
        await entity.async_turn_on()
        assert entity.is_on
        await entity.async_turn_off()
        assert not entity.is_on
