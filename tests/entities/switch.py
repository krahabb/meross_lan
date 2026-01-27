from homeassistant.components import switch as haec
from homeassistant.helpers.entity import STATE_OFF, STATE_ON

from custom_components.meross_lan.devices import (
    garagedoor as gd,
    hub,
    mss,
    rollershutter as rs,
)
from custom_components.meross_lan.devices.thermostat.mtsthermostat import (
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
from custom_components.meross_lan.switch import (
    MLEmulatedSwitch,
    MLToggle,
    MLToggleX,
    PhysicalLockSwitch,
)

from tests.entities import EntityComponentTest


class EntityTest(EntityComponentTest):

    ENTITY_TYPE = haec.SwitchEntity

    # special care here since light and cover entity could manage the togglex
    # namespace
    DIGEST_ENTITIES = {
        mc.KEY_TOGGLEX: [MLToggleX],
    }
    NAMESPACES_ENTITIES = {
        mn.Appliance_GarageDoor_Config: [
            gd.MLGarageConfigSwitch,  # buzzerEnable
        ],
        mn.Appliance_GarageDoor_MultipleConfig: [
            gd.MLGarageDoorEnableSwitch,
            gd.MLGarageMultipleConfigSwitch,
        ],
        mn.Appliance_Config_OverTemp: [mss.OverTempEnableSwitch],
        mn.Appliance_Control_PhysicalLock: [PhysicalLockSwitch],
        mn_t.Appliance_Control_Thermostat_ModeC: [
            MLEmulatedSwitch,  # fan_hold_enable
        ],
        mn_t.Appliance_Control_Thermostat_Frost: [MtsConfigSwitch],
        mn_t.Appliance_Control_Thermostat_Sensor: [MtsExternalSensorSwitch],
        mn_t.Appliance_Control_Thermostat_Overheat: [MtsConfigSwitch],
        mn.Appliance_Control_Toggle: [MLToggle],
        mn.Appliance_RollerShutter_Adjust: [rs.MLRollerShutterAdjustSwitch],
    }
    HUB_SUBDEVICES_ENTITIES = {
        mc.TYPE_MTS100: [MLEmulatedSwitch],
        mc.TYPE_MTS100V3: [MLEmulatedSwitch],
        mc.TYPE_MTS150: [MLEmulatedSwitch, hub.HubBeep],
        mc.KEY_DOORWINDOW: [hub.HubBeep],
        mc.KEY_MST: [hub.MstSwitch],
        mc.KEY_WATERLEAK: [hub.HubBeep],
    }

    async def async_test_enabled_callback(self, entity: haec.SwitchEntity):
        await self.async_service_call_check(haec.SERVICE_TURN_ON, STATE_ON)
        await self.async_service_call_check(haec.SERVICE_TURN_OFF, STATE_OFF)

    async def async_test_disabled_callback(self, entity: haec.SwitchEntity):
        await entity.async_turn_on()
        assert entity.is_on
        await entity.async_turn_off()
        assert not entity.is_on
