from homeassistant.components import switch as haec
from homeassistant.helpers.entity import STATE_OFF, STATE_ON

from custom_components.meross_lan.devices.mss import OverTempEnableSwitch
from custom_components.meross_lan.devices.thermostat import MtsExternalSensorSwitch
from custom_components.meross_lan.meross_entity import MerossToggle
from custom_components.meross_lan.merossclient import const as mc
from custom_components.meross_lan.switch import MLSwitch, PhysicalLockSwitch

from tests.entities import EntityComponentTest


class EntityTest(EntityComponentTest):

    ENTITY_TYPE = haec.SwitchEntity

    # special care here since light and cover entity could manage the togglex
    # namespace
    DIGEST_ENTITIES = {
        mc.KEY_TOGGLEX: [MLSwitch],
    }

    NAMESPACES_ENTITIES = {
        mc.NS_APPLIANCE_CONFIG_OVERTEMP: [OverTempEnableSwitch],
        mc.NS_APPLIANCE_CONTROL_PHYSICALLOCK: [PhysicalLockSwitch],
        mc.NS_APPLIANCE_CONTROL_THERMOSTAT_SENSOR: [MtsExternalSensorSwitch],
        mc.NS_APPLIANCE_CONTROL_TOGGLE: [MLSwitch],
    }

    async def async_test_each_callback(self, entity: haec.SwitchEntity):
        pass

    async def async_test_enabled_callback(self, entity: haec.SwitchEntity):
        await self.async_service_call_check(haec.SERVICE_TURN_ON, STATE_ON)
        await self.async_service_call_check(haec.SERVICE_TURN_OFF, STATE_OFF)

    async def async_test_disabled_callback(self, entity: haec.SwitchEntity):
        await entity.async_turn_on()
        assert entity.is_on
        await entity.async_turn_off()
        assert not entity.is_on
