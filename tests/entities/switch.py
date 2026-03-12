from homeassistant import const as hac
from homeassistant.components import switch as haec

from custom_components.meross_lan import siren, switch
from custom_components.meross_lan.devices import (
    garagedoor as gd,
    hub,
    mss,
    rollershutter as rs,
)
from custom_components.meross_lan.devices.thermostat import (
    MtsConfigSwitch,
    MtsExternalSensorSwitch,
)
from custom_components.meross_lan.merossclient.protocol import (
    const as mc,
    namespaces as mn,
)
from custom_components.meross_lan.merossclient.protocol.namespaces import (
    thermostat as mn_t,
)

from tests.entities import EntityComponentTest


class EntityTest(EntityComponentTest):

    ENTITY_TYPE = haec.SwitchEntity

    # special care here since light and cover entity could manage the togglex
    # namespace
    DIGEST_ENTITIES = {
        mc.KEY_TOGGLEX: [switch.Togglex],
    }
    NAMESPACES_ENTITIES = {
        mn.Appliance_Config_Alarm: [siren.Siren.EnableSwitch],
        mn.Appliance_GarageDoor_Config: [
            gd.GarageConfigSwitch,  # buzzerEnable
        ],
        mn.Appliance_GarageDoor_MultipleConfig: [
            gd.GarageEnableSwitch,
            gd.GarageConfigSwitch,
        ],
        mn.Appliance_Config_OverTemp: [mss.OverTempEnableSwitch],
        mn.Appliance_Control_PhysicalLock: [switch.PhysicalLockSwitch],
        mn_t.Appliance_Control_Thermostat_ModeC: [
            switch.EmulatedSwitch,  # fan_hold_enable
        ],
        mn_t.Appliance_Control_Thermostat_Frost: [MtsConfigSwitch],
        mn_t.Appliance_Control_Thermostat_Sensor: [MtsExternalSensorSwitch],
        mn_t.Appliance_Control_Thermostat_Overheat: [MtsConfigSwitch],
        mn.Appliance_Control_Toggle: [switch.Toggle],
        mn.Appliance_RollerShutter_Adjust: [rs.RollerShutterAdjustSwitch],
    }
    HUB_SUBDEVICES_ENTITIES = {
        mc.TYPE_MTS100: [switch.EmulatedSwitch],
        mc.TYPE_MTS100V3: [switch.EmulatedSwitch],
        mc.TYPE_MTS150: [switch.EmulatedSwitch, hub.HubBeep],
        mc.KEY_DOORWINDOW: [hub.HubBeep],
        mc.KEY_MST: [hub.MstSwitch],
        mc.KEY_WATERLEAK: [hub.HubBeep],
    }

    async def async_test_enabled_callback(self, entity):
        await self.async_service_call_check(haec.SERVICE_TURN_ON, hac.STATE_ON)
        await self.async_service_call_check(haec.SERVICE_TURN_OFF, hac.STATE_OFF)

    async def async_test_disabled_callback(self, entity: haec.SwitchEntity):
        await entity.async_turn_on()
        assert entity.is_on
        await entity.async_turn_off()
        assert not entity.is_on
