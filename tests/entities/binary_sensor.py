from homeassistant.components import binary_sensor as haec

from custom_components.meross_lan.binary_sensor import MLBinarySensor
from custom_components.meross_lan.devices.thermostat.mts960 import Mts960Climate
from custom_components.meross_lan.devices.thermostat.mtsthermostat import (
    MtsWarningSensor,
    MtsWindowOpened,
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

    ENTITY_TYPE = haec.BinarySensorEntity

    DIGEST_ENTITIES = {
        mc.KEY_THERMOSTAT: {
            mc.KEY_MODEB: [
                Mts960Climate.PlugState,
            ],
        },
    }

    NAMESPACES_ENTITIES = {
        mn_t.Appliance_Control_Thermostat_Frost.name: [MtsWarningSensor],
        mn_t.Appliance_Control_Thermostat_Overheat.name: [MtsWarningSensor],
        mn_t.Appliance_Control_Thermostat_WindowOpened.name: [MtsWindowOpened],
    }

    HUB_SUBDEVICES_ENTITIES = {
        mc.KEY_DOORWINDOW: [
            MLBinarySensor,  # window opened
        ],
        mc.TYPE_MTS100: [MLBinarySensor],  # window opened
        mc.TYPE_MTS100V3: [MLBinarySensor],  # window opened
        mc.TYPE_MTS150: [MLBinarySensor],  # window opened
        mc.KEY_SMOKEALARM: [
            MLBinarySensor,  # alarm
            MLBinarySensor,  # error
            MLBinarySensor,  # muted
        ],
    }

    async def async_test_enabled_callback(self, entity):
        pass

    async def async_test_disabled_callback(self, entity):
        pass
