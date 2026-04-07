from homeassistant.components import number as haec

from custom_components.meross_lan import number, siren, switch
from custom_components.meross_lan.devices import (
    garagedoor as gd,
    ms600,
    rollershutter as rs,
)
from custom_components.meross_lan.devices.hub import ms, mst
from custom_components.meross_lan.devices.hub.mts import mts100v3
from custom_components.meross_lan.devices.thermostat import (
    MtsClimate,
    MtsCommonTemperatureExtNumber,
    MtsDeadZoneNumber,
    MtsFrostNumber,
    MtsOverheatNumber,
    ScreenBrightnessNumber,
    mn_t,
)
from custom_components.meross_lan.devices.thermostat.mts200 import Mts200Climate
from custom_components.meross_lan.devices.thermostat.mts300 import Mts300Climate
from custom_components.meross_lan.devices.thermostat.mts960 import Mts960Climate
from custom_components.meross_lan.helpers.entity import Entity
from custom_components.meross_lan.merossclient.protocol import (
    const as mc,
    namespaces as mn,
)

from tests.entities import EntityComponentTest


def _climate_number_entities(climate_class: type[MtsClimate]) -> list[type[Entity]]:
    # TODO: redefine EntityComponentTest base container types to allow Sequence instead of list for defining
    # entity types
    return [climate_class.AdjustNumber] + [climate_class.SetPointNumber] * 3  # type: ignore


_MTS100_ENTITES = _climate_number_entities(mts100v3)


class EntityTest(EntityComponentTest):

    ENTITY_TYPE = haec.NumberEntity

    DIGEST_ENTITIES = {
        mc.KEY_THERMOSTAT: {
            mc.KEY_MODE: _climate_number_entities(Mts200Climate),
            mc.KEY_MODEB: [
                Mts960Climate.AdjustNumber,
                Mts960Climate.TimerConfigNumber,
                Mts960Climate.TimerConfigNumber,
                Mts960Climate.TimerConfigNumber,
            ],
        },
    }
    NAMESPACES_ENTITIES = {
        mn.Appliance_Config_Alarm: [siren.Siren.VolumeNumber],
        mn.Appliance_GarageDoor_Config: [
            gd.GarageConfigNumber,
            gd.GarageConfigNumber,  # doorOpenDuration
            gd.GarageConfigNumber,  # doorCloseDuration
        ],
        mn.Appliance_GarageDoor_MultipleConfig: [
            gd.GarageConfigNumber,
            gd.GarageConfigNumber,
        ],
        mn.Appliance_RollerShutter_Config: [rs.NumberParser] * 2,
        mn.Appliance_Control_Presence_Config: [
            ms600.PresenceConfigNumber,
            ms600.PresenceConfigNumber,
        ]
        + [ms600.PresenceConfigMthX] * 3,  # type: ignore
        mn.Appliance_Control_Screen_Brightness: [ScreenBrightnessNumber] * 2,
        mn_t.Appliance_Control_Thermostat_DeadZone: [MtsDeadZoneNumber],
        mn_t.Appliance_Control_Thermostat_Frost: [MtsFrostNumber],
        mn_t.Appliance_Control_Thermostat_HoldAction: [number.NumberParser],
        mn_t.Appliance_Control_Thermostat_ModeC: [
            Mts300Climate.AdjustNumber,
            Mts300Climate.AdjustNumber.AdjustHumidityNumber,  # humidity_calibration
            number.NumberParser,  # fan_hold_time
        ],
        mn_t.Appliance_Control_Thermostat_Overheat: [MtsOverheatNumber],
    }
    HUB_SUBDEVICES_ENTITIES = {
        mc.TYPE_MS100: [
            ms.ms100.AdjustTemperatureNumber,
            ms.ms100.AdjustHumidityNumber,
        ],
        mc.TYPE_MTS100: _MTS100_ENTITES,
        mc.TYPE_MTS100V3: _MTS100_ENTITES,
        mc.TYPE_MTS150: _MTS100_ENTITES,
        mc.KEY_MST: [mst.mst100.WateringDurationNumber],
    }

    async def async_test_each_callback(self, entity: number.NumberEntity):
        if type(entity) is gd.EmulatedNumber and type(entity.parent) is gd.GarageDoor:
            EntityComponentTest.expected_entity_types.remove(gd.GarageConfigNumber)

        if isinstance(entity, MtsCommonTemperatureExtNumber):
            # rich temperatures are set to 'unavailable' when
            # the corresponding function is 'off'. We'll so use
            # the associated switch to turn it on in case.
            _switch = entity.switch
            ison = _switch.is_on
            assert ison is entity.available  # either both True or False
            if not ison:
                await _switch.async_turn_on()
        elif entity.entity_key == "fan_hold_time":
            # This entity too (mts300) might be unavailable if
            # the device is configured to disable 'fan hold'.
            # Again we can control this function through a dedicated switch.
            device = self.device_context.device
            _switch = device.entities[
                entity.id.replace("fan_hold_time", "fan_hold_enable")
            ]
            assert type(_switch) is switch.EmulatedSwitch
            # Here we cannot check for availability consistence
            # since at start it is a bit messed up.
            if not _switch.is_on:
                await _switch.async_turn_on()
        await super().async_test_each_callback(entity)

    async def async_test_enabled_callback(self, entity: number.NumberEntity):
        is_config_number = isinstance(entity, number.NumberParser)
        get_hass_state = EntityComponentTest.get_hass_state
        time_mocker = self.device_context.time_mock
        await self.async_service_call(
            haec.SERVICE_SET_VALUE, {haec.ATTR_VALUE: entity.max_value}
        )
        if is_config_number:
            await time_mocker.async_tick(entity.DEBOUNCE_DELAY)
        assert (state := get_hass_state(self.entity_id))
        assert float(state.state) == entity.max_value, "max_value"
        await self.async_service_call(
            haec.SERVICE_SET_VALUE, {haec.ATTR_VALUE: entity.min_value}
        )
        if is_config_number:
            await time_mocker.async_tick(entity.DEBOUNCE_DELAY)
        assert (state := get_hass_state(self.entity_id))
        assert float(state.state) == entity.min_value, "min_value"

    async def async_test_disabled_callback(self, entity: number.NumberEntity):
        is_config_number = isinstance(entity, number.NumberParser)
        time_mocker = self.device_context.time_mock
        await entity.async_set_native_value(entity.native_max_value)
        if is_config_number:
            await time_mocker.async_tick(entity.DEBOUNCE_DELAY)
        assert entity.state == entity.max_value
        await entity.async_set_native_value(entity.native_min_value)
        if is_config_number:
            await time_mocker.async_tick(entity.DEBOUNCE_DELAY)
        assert entity.state == entity.min_value
