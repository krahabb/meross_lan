from homeassistant.components import climate as haec
from homeassistant.components.climate import ClimateEntity, HVACMode

from custom_components.meross_lan.climate import MtsClimate
from custom_components.meross_lan.devices.mts100 import Mts100Climate
from custom_components.meross_lan.devices.mts200 import Mts200Climate
from custom_components.meross_lan.devices.mts960 import Mts960Climate
from custom_components.meross_lan.merossclient import const as mc, namespaces as mn

from tests.entities import EntityComponentTest

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
        mc.MTS200_MODE_MANUAL,
    },
}


class EntityTest(EntityComponentTest):

    ENTITY_TYPE = ClimateEntity

    DIGEST_ENTITIES = {
        mc.KEY_THERMOSTAT: {
            mc.KEY_MODE: [Mts200Climate],
            mc.KEY_MODEB: [Mts960Climate],
        },
    }

    HUB_SUBDEVICES_ENTITIES = {
        mc.TYPE_MTS100: [Mts100Climate],
        mc.TYPE_MTS100V3: [Mts100Climate],
        mc.TYPE_MTS150: [Mts100Climate],
    }

    async def async_test_each_callback(self, entity: MtsClimate):
        await super().async_test_each_callback(entity)
        entity_hvac_modes = set(entity.hvac_modes)
        expected_hvac_modes = HVAC_MODES[entity.__class__]
        assert expected_hvac_modes.issubset(entity_hvac_modes)
        if mn.Appliance_Control_Thermostat_SummerMode.name in self.ability:
            assert HVACMode.COOL in entity_hvac_modes

        if entity.__class__ in PRESET_MODES:
            entity_preset_modes = set(entity.preset_modes)
            expected_preset_modes = {
                entity.MTS_MODE_TO_PRESET_MAP[mts_mode]
                for mts_mode in PRESET_MODES[entity.__class__]
            }
            assert expected_preset_modes == entity_preset_modes

    async def async_test_enabled_callback(self, entity: MtsClimate):
        if isinstance(entity, Mts960Climate):
            # TODO: restore testing once mts960 is done
            return

        for hvac_mode in entity.hvac_modes:
            await self.async_service_call_check(
                haec.SERVICE_SET_HVAC_MODE, hvac_mode, {haec.ATTR_HVAC_MODE: hvac_mode}
            )

        for preset_mode in entity.preset_modes:
            state = await self.async_service_call(
                haec.SERVICE_SET_PRESET_MODE, {haec.ATTR_PRESET_MODE: preset_mode}
            )
            assert (
                state.attributes[haec.ATTR_PRESET_MODE] == preset_mode
            ), f"preset_mode: {preset_mode}"

        state = await self.async_service_call(
            haec.SERVICE_SET_TEMPERATURE, {haec.ATTR_TEMPERATURE: entity.min_temp}
        )
        assert (
            state.attributes[haec.ATTR_TEMPERATURE] == entity.min_temp
        ), "set_temperature: min_temp"
        state = await self.async_service_call(
            haec.SERVICE_SET_TEMPERATURE, {haec.ATTR_TEMPERATURE: entity.max_temp}
        )
        assert (
            state.attributes[haec.ATTR_TEMPERATURE] == entity.max_temp
        ), "set_temperature: max_temp"
        await self.async_service_call_check(haec.SERVICE_TURN_OFF, HVACMode.OFF)

    async def async_test_disabled_callback(self, entity: ClimateEntity):
        pass
