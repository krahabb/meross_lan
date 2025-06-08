from homeassistant.components import switch as haec
from homeassistant.helpers.entity import STATE_OFF, STATE_ON

from custom_components.meross_lan.devices.hub import GS559MuteToggle
from custom_components.meross_lan.devices.mss import OverTempEnableSwitch
from custom_components.meross_lan.devices.thermostat import MtsExternalSensorSwitch
from custom_components.meross_lan.merossclient.protocol import (
    const as mc,
    namespaces as mn,
)
from custom_components.meross_lan.switch import MLToggle, MLToggleX, PhysicalLockSwitch

from tests.entities import EntityComponentTest


class EntityTest(EntityComponentTest):

    ENTITY_TYPE = haec.SwitchEntity

    # special care here since light and cover entity could manage the togglex
    # namespace
    DIGEST_ENTITIES = {
        mc.KEY_TOGGLEX: [MLToggleX],
    }

    NAMESPACES_ENTITIES = {
        mn.Appliance_Config_OverTemp.name: [OverTempEnableSwitch],
        mn.Appliance_Control_PhysicalLock.name: [PhysicalLockSwitch],
        mn.Appliance_Control_Thermostat_Sensor.name: [MtsExternalSensorSwitch],
        mn.Appliance_Control_Toggle.name: [MLToggle],
    }

    HUB_SUBDEVICES_ENTITIES = {
        mc.KEY_SMOKEALARM: [GS559MuteToggle],  #  interConn switch
    }

    async def async_test_enabled_callback(self, entity: haec.SwitchEntity):
        await self.async_service_call_check(haec.SERVICE_TURN_ON, STATE_ON)
        await self.async_service_call_check(haec.SERVICE_TURN_OFF, STATE_OFF)

    async def async_test_disabled_callback(self, entity: haec.SwitchEntity):
        await entity.async_turn_on()
        assert entity.is_on
        await entity.async_turn_off()
        assert not entity.is_on
