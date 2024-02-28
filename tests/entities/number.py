from homeassistant.components.number import (
    ATTR_VALUE,
    DOMAIN,
    SERVICE_SET_VALUE,
    NumberEntity,
)

from custom_components.meross_lan.cover import (
    MLGarageConfigNumber,
    MLGarageMultipleConfigNumber,
    MLRollerShutterConfigNumber,
)
from custom_components.meross_lan.devices.hub import MLHubSensorAdjustNumber
from custom_components.meross_lan.devices.mts100 import Mts100AdjustNumber
from custom_components.meross_lan.devices.screenbrightness import (
    MLScreenBrightnessNumber,
)
from custom_components.meross_lan.merossclient import const as mc
from custom_components.meross_lan.number import MLConfigNumber

from tests.entities import EntityComponentTest


class EntityTest(EntityComponentTest):

    ENTITY_TYPE = NumberEntity

    NAMESPACES_ENTITIES = {
        mc.NS_APPLIANCE_GARAGEDOOR_CONFIG: [MLGarageConfigNumber],
        mc.NS_APPLIANCE_GARAGEDOOR_MULTIPLECONFIG: [MLGarageMultipleConfigNumber],
        mc.NS_APPLIANCE_ROLLERSHUTTER_CONFIG: [
            MLRollerShutterConfigNumber,
            MLRollerShutterConfigNumber,
        ],
        mc.NS_APPLIANCE_CONTROL_SCREEN_BRIGHTNESS: [
            MLScreenBrightnessNumber,
            MLScreenBrightnessNumber,
        ],
    }
    HUB_SUBDEVICES_ENTITIES = {
        mc.TYPE_MS100: [MLHubSensorAdjustNumber, MLHubSensorAdjustNumber],
        mc.TYPE_MTS100: [Mts100AdjustNumber],
        mc.TYPE_MTS100V3: [Mts100AdjustNumber],
        mc.TYPE_MTS150: [Mts100AdjustNumber],
    }

    async def async_test_each_callback(self, entity: MLConfigNumber):
        pass

    async def async_test_enabled_callback(self, entity: MLConfigNumber):
        states = self.hass_states
        await self.async_service_call(SERVICE_SET_VALUE, {ATTR_VALUE: entity.max_value})
        await self.device_context.async_tick(entity.DEBOUNCE_DELAY)
        assert (state := states.get(self.entity_id))
        assert float(state.state) == entity.max_value, "max_value"
        await self.async_service_call(SERVICE_SET_VALUE, {ATTR_VALUE: entity.min_value})
        await self.device_context.async_tick(entity.DEBOUNCE_DELAY)
        assert (state := states.get(self.entity_id))
        assert float(state.state) == entity.min_value, "min_value"

    async def async_test_disabled_callback(self, entity: MLConfigNumber):
        await entity.async_set_native_value(entity.native_max_value)
        await self.device_context.async_tick(entity.DEBOUNCE_DELAY)
        assert entity.state == entity.max_value
        await entity.async_set_native_value(entity.native_min_value)
        await self.device_context.async_tick(entity.DEBOUNCE_DELAY)
        assert entity.state == entity.min_value
