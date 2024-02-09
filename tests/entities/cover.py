from homeassistant.components import cover as haec
from homeassistant.components.cover import CoverEntity, CoverEntityFeature

from custom_components.meross_lan.cover import MLGarage, MLRollerShutter
from custom_components.meross_lan.merossclient import const as mc
from custom_components.meross_lan.switch import MLSwitch
from emulator.mixins.garagedoor import GarageDoorMixin
from emulator.mixins.rollershutter import RollerShutterMixin

from tests.entities import EntityComponentTest


class EntityTest(EntityComponentTest):

    ENTITY_TYPE = CoverEntity

    DIGEST_ENTITIES = {
        mc.KEY_GARAGEDOOR: {MLGarage},
    }

    NAMESPACES_ENTITIES = {
        mc.NS_APPLIANCE_ROLLERSHUTTER_STATE: {MLRollerShutter},
    }

    COVER_TRANSITIONS = {
        haec.STATE_OPEN: (
            haec.SERVICE_CLOSE_COVER,
            haec.STATE_CLOSING,
            haec.STATE_CLOSED,
        ),
        haec.STATE_CLOSED: (
            haec.SERVICE_OPEN_COVER,
            haec.STATE_OPENING,
            haec.STATE_OPEN,
        ),
    }

    async def async_test_each_callback(self, entity: CoverEntity):
        ability = self.ability
        # check the other specialized implementations
        if mc.NS_APPLIANCE_CONTROL_TOGGLEX in ability:
            if MLSwitch in EntityComponentTest.expected_entity_types:
                EntityComponentTest.expected_entity_types.remove(MLSwitch)

        if isinstance(entity, MLGarage):
            assert (
                entity.supported_features
                == CoverEntityFeature.OPEN | CoverEntityFeature.CLOSE
            )
        elif isinstance(entity, MLRollerShutter):
            assert (
                entity.supported_features
                >= CoverEntityFeature.OPEN
                | CoverEntityFeature.CLOSE
                | CoverEntityFeature.STOP
            )

    async def async_test_enabled_callback(self, entity: CoverEntity):
        states = self.hass.states
        if isinstance(entity, MLGarage):
            await self._async_test_garage_transition(entity)
            await self._async_test_garage_transition(entity)
        elif isinstance(entity, MLRollerShutter):
            await self._async_test_garage_transition(entity)
            await self._async_test_garage_transition(entity)

    async def async_test_disabled_callback(self, entity: CoverEntity):
        pass

    async def _async_test_garage_transition(self, entity):
        """Start and follow the transition from open to close or
        close to open depending on current state."""
        states = self.hass.states
        assert (state := states.get(self.entity_id))
        trans = self.COVER_TRANSITIONS[state.state]
        state = await self.async_service_call(trans[0])
        assert state.state == trans[1], trans[1]
        # The MLGarage state machine has a timed callback mechanism
        # TODO: use and check that callback instead of the raw 60 seconds timeout
        await self.device_context.async_tick(60)
        assert (state := states.get(self.entity_id))
        assert state.state == trans[2], trans[2]
