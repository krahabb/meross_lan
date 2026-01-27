from homeassistant.components import number as haec

from custom_components.meross_lan.devices import (
    garagedoor as gd,
    hub,
    ms600,
    rollershutter as rs,
)
from custom_components.meross_lan.devices.hub.mts100 import Mts100Climate
from custom_components.meross_lan.devices.thermostat.mts200 import Mts200Climate
from custom_components.meross_lan.devices.thermostat.mts300 import Mts300Climate
from custom_components.meross_lan.devices.thermostat.mts960 import Mts960Climate
from custom_components.meross_lan.devices.thermostat.mtsthermostat import (
    MLScreenBrightnessNumber,
    MtsClimate,
    MtsCommonTemperatureExtNumber,
    MtsDeadZoneNumber,
    MtsFrostNumber,
    MtsOverheatNumber,
    MtsThermostatClimate,
    mn_t,
)
from custom_components.meross_lan.helpers.entity import MLEntity
from custom_components.meross_lan.merossclient.protocol import (
    const as mc,
    namespaces as mn,
)
from custom_components.meross_lan.number import MLConfigNumber, MLNumber
from custom_components.meross_lan.switch import MLEmulatedSwitch

from tests.entities import EntityComponentTest


def _climate_number_entities(climate_class: type[MtsClimate]) -> list[type[MLEntity]]:
    # TODO: redefine EntityComponentTest base container types to allow Sequence instead of list for defining
    # entity types
    return [climate_class.AdjustNumber] + [climate_class.SetPointNumber] * 3  # type: ignore


_MTS100_ENTITES = _climate_number_entities(Mts100Climate)


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
        mn.Appliance_GarageDoor_Config: [
            gd.MLGarageConfigNumber,
            gd.MLGarageConfigNumber,  # doorOpenDuration
            gd.MLGarageConfigNumber,  # doorCloseDuration
            gd.MLGarageConfigSwitch,  # buzzerEnble
        ],
        mn.Appliance_GarageDoor_MultipleConfig: [
            gd.MLGarageMultipleConfigNumber,
            gd.MLGarageMultipleConfigNumber,
        ],
        mn.Appliance_RollerShutter_Config: [rs.MLRollerShutterConfigNumber] * 2,
        mn.Appliance_Control_Presence_Config: [
            ms600.PresenceConfigNoBodyTime,
            ms600.PresenceConfigDistance,
        ]
        + [ms600.PresenceConfigMthX] * 3,
        mn.Appliance_Control_Screen_Brightness: [MLScreenBrightnessNumber] * 2,
        mn_t.Appliance_Control_Thermostat_DeadZone: [MtsDeadZoneNumber],
        mn_t.Appliance_Control_Thermostat_Frost: [MtsFrostNumber],
        mn_t.Appliance_Control_Thermostat_HoldAction: [MLConfigNumber],
        mn_t.Appliance_Control_Thermostat_ModeC: [
            Mts300Climate.AdjustNumber,
            MLConfigNumber,  # humidity_calibration
            MLConfigNumber,  # fan_hold_time
        ],
        mn_t.Appliance_Control_Thermostat_Overheat: [MtsOverheatNumber],
    }
    HUB_SUBDEVICES_ENTITIES = {
        mc.TYPE_MS100: [
            hub.MS100Sensor.AdjustTemperatureNumber,
            hub.MS100Sensor.AdjustHumidityNumber,
        ],
        mc.TYPE_MTS100: _MTS100_ENTITES,
        mc.TYPE_MTS100V3: _MTS100_ENTITES,
        mc.TYPE_MTS150: _MTS100_ENTITES,
        mc.KEY_MST: [hub.MstSwitch.WateringDurationNumber],
    }

    async def async_test_each_callback(self, entity: MLNumber):
        if type(entity) is gd.MLGarageEmulatedConfigNumber:
            EntityComponentTest.expected_entity_types.remove(gd.MLGarageConfigNumber)

        if isinstance(entity, MtsCommonTemperatureExtNumber):
            # rich temperatures are set to 'unavailable' when
            # the corresponding function is 'off'. We'll so use
            # the associated switch to turn it on in case.
            switch = entity.switch
            ison = switch.is_on
            assert ison is entity.available  # either both True or False
            if not ison:
                await switch.async_turn_on()
        elif entity.entitykey == "fan_hold_time":
            # This entity too (mts300) might be unavailable if
            # the device is configured to disable 'fan hold'.
            # Again we can control this function through a dedicated switch.
            device = self.device_context.device
            switch = device.entities[f"{entity.channel}_fan_hold_enable"]
            assert type(switch) is MLEmulatedSwitch
            # Here we cannot check for availability consistence
            # since at start it is a bit messed up.
            if not switch.is_on:
                await switch.async_turn_on()
        await super().async_test_each_callback(entity)

    async def async_test_enabled_callback(self, entity: MLNumber):
        is_config_number = isinstance(entity, MLConfigNumber)
        states = self.hass_states
        time_mocker = self.device_context.time_mock
        await self.async_service_call(
            haec.SERVICE_SET_VALUE, {haec.ATTR_VALUE: entity.max_value}
        )
        if is_config_number:
            await time_mocker.async_tick(entity.DEBOUNCE_DELAY)
        assert (state := states.get(self.entity_id))
        assert float(state.state) == entity.max_value, "max_value"
        await self.async_service_call(
            haec.SERVICE_SET_VALUE, {haec.ATTR_VALUE: entity.min_value}
        )
        if is_config_number:
            await time_mocker.async_tick(entity.DEBOUNCE_DELAY)
        assert (state := states.get(self.entity_id))
        assert float(state.state) == entity.min_value, "min_value"

    async def async_test_disabled_callback(self, entity: MLNumber):
        is_config_number = isinstance(entity, MLConfigNumber)
        time_mocker = self.device_context.time_mock
        await entity.async_set_native_value(entity.native_max_value)
        if is_config_number:
            await time_mocker.async_tick(entity.DEBOUNCE_DELAY)
        assert entity.state == entity.max_value
        await entity.async_set_native_value(entity.native_min_value)
        if is_config_number:
            await time_mocker.async_tick(entity.DEBOUNCE_DELAY)
        assert entity.state == entity.min_value
