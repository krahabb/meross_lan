from homeassistant.components import binary_sensor as haec

from custom_components.meross_lan.binary_sensor import BinarySensor
from custom_components.meross_lan.devices import garagedoor as gd, hub
from custom_components.meross_lan.devices.thermostat import (
    MtsWindowOpened,
)
from custom_components.meross_lan.devices.thermostat.mts960 import Mts960Climate
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
        mc.KEY_GARAGEDOOR: [gd.GarageTimeoutBinarySensor],
        mc.KEY_THERMOSTAT: {
            mc.KEY_MODEB: [
                Mts960Climate.PlugState,
            ],
        },
    }

    NAMESPACES_ENTITIES = {
        mn.Appliance_Control_Presence_Config: [
            # These are entities installed by mn.Appliance_Control_Sensor_LatestX
            # but we use this namespace to detect presence capability (ms600)
            BinarySensor,
        ],
        mn_t.Appliance_Control_Thermostat_WindowOpened: [MtsWindowOpened],
    }

    HUB_SUBDEVICES_ENTITIES = {
        mc.TYPE_MTS100: [BinarySensor],  # window opened
        mc.TYPE_MTS100V3: [BinarySensor],  # window opened
        mc.TYPE_MTS150: [BinarySensor],  # window opened
        mc.KEY_DOORWINDOW: [hub.DoorWindowSensor],
        mc.KEY_SMOKEALARM: [
            BinarySensor,  # alarm
            BinarySensor,  # error
            BinarySensor,  # muted
        ],
        mc.KEY_WATERLEAK: [hub.WaterLeakSensor],
    }

    async def async_test_enabled_callback(self, entity):
        pass

    async def async_test_disabled_callback(self, entity):
        pass
