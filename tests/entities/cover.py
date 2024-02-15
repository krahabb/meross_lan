from datetime import timedelta

from homeassistant.components import cover as haec
from homeassistant.components.cover import CoverEntity, CoverEntityFeature

from custom_components.meross_lan import const as mlc
from custom_components.meross_lan.cover import MLGarage, MLRollerShutter
from custom_components.meross_lan.merossclient import const as mc
from custom_components.meross_lan.switch import MLSwitch
from emulator.mixins.garagedoor import GarageDoorMixin
from emulator.mixins.rollershutter import RollerShutterMixin

from tests.entities import EntityComponentTest


class EntityTest(EntityComponentTest):

    ENTITY_TYPE = CoverEntity

    DIGEST_ENTITIES = {
        mc.KEY_GARAGEDOOR: [MLGarage],
    }

    NAMESPACES_ENTITIES = {
        mc.NS_APPLIANCE_ROLLERSHUTTER_STATE: [MLRollerShutter],
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
            assert entity._signalClose == RollerShutterMixin.SIGNALCLOSE
            assert entity._signalOpen == RollerShutterMixin.SIGNALOPEN

    async def async_test_enabled_callback(self, entity: CoverEntity):
        states = self.hass_states
        if isinstance(entity, MLGarage):
            await self._async_test_garage_transition(entity)
            await self._async_test_garage_transition(entity)
        elif isinstance(entity, MLRollerShutter):
            # MLRollerShutter could need at least a run to enable
            # support for SET_POSITION
            # this should open the cover (emulator starts with closed)
            await self._async_test_garage_transition(entity)
            assert CoverEntityFeature.OPEN in entity.supported_features
            # this should close the cover
            state = await self._async_test_garage_transition(entity)
            assert (
                state.attributes[haec.ATTR_CURRENT_POSITION] == 0
            ), f"{haec.ATTR_CURRENT_POSITION}!=0"

            # open a bit up to 30% ensuring it transitions correctly
            await self._async_test_set_position(entity, 30)

            # now we'll interrupt a transition
            state = await self.async_service_call(
                haec.SERVICE_SET_COVER_POSITION, {haec.ATTR_POSITION: 60}
            )
            # advance the time a bit
            await self.device_context.async_tick(
                mlc.PARAM_ROLLERSHUTTER_TRANSITION_POLL_TIMEOUT
            )
            # ensure we're still opening
            assert (state := states.get(self.entity_id))
            assert (
                state.state == haec.STATE_OPENING
            ), f"{haec.SERVICE_SET_COVER_POSITION}: state!={haec.STATE_OPENING}"
            # now stop
            await self._async_test_stop()

            # also test setting position to fully close/open
            # since there are some subtleties with emulated position runs
            await self._async_test_set_position(entity, 0)
            await self._async_test_set_position(entity, 100)

    async def async_test_disabled_callback(self, entity: CoverEntity):
        pass

    async def _async_test_garage_transition(self, entity):
        """Start and follow the transition from open to close or
        close to open depending on current state."""
        states = self.hass_states
        assert (state := states.get(self.entity_id))
        trans = self.COVER_TRANSITIONS[state.state]
        state = await self.async_service_call(trans[0])
        assert state.state == trans[1], trans[1]
        # The MLGarage/MLRollerShutter state machine has a timed callback mechanism
        # TODO: use and check that callback instead of the raw 60 seconds timeout
        # we're using async_warp over 40 seconds (which are enough on our emulators
        # to complete the transition) so to better 'match' the callbacks state refresh
        await self.device_context.async_warp(
            40,
            tick=1,
        )
        assert (state := states.get(self.entity_id))
        assert state.state == trans[2], trans[2]
        return state

    async def _async_test_set_position(
        self, entity: MLRollerShutter, target_position: int
    ):

        state = await self.async_service_call(
            haec.SERVICE_SET_COVER_POSITION, {haec.ATTR_POSITION: target_position}
        )
        current_position = state.attributes[haec.ATTR_CURRENT_POSITION]
        # check the cover is moving
        if target_position > current_position:
            expected_state = haec.STATE_OPENING
            expected_duration = (
                RollerShutterMixin.SIGNALOPEN
                * (target_position - current_position)
                / 100000
            )
            expected_duration_max = RollerShutterMixin.SIGNALOPEN / 1000
        else:
            expected_state = haec.STATE_CLOSING
            expected_duration = (
                RollerShutterMixin.SIGNALCLOSE
                * (current_position - target_position)
                / 100000
            )
            expected_duration_max = RollerShutterMixin.SIGNALCLOSE / 1000
        assert (
            state.state == expected_state
        ), f"started {haec.SERVICE_SET_COVER_POSITION}({target_position}): state!={expected_state}"

        # process the transition state machine
        time_mock = self.device_context._time_mock
        loop_time = self.hass.loop.time
        current_epoch = loop_time()
        transition_end_epoch = current_epoch + expected_duration
        transition_end_epoch_max = current_epoch + expected_duration_max

        while current_epoch < transition_end_epoch_max:
            # Advances the time mocker up to the next transition polling cycle and executes it
            _transition_unsub = entity._transition_unsub
            assert _transition_unsub, "missing transition callback"
            _when = _transition_unsub.when()
            if _when >= transition_end_epoch:
                if entity._position_native_isgood:
                    # the entity just monitors the device state
                    # and it should end the transition when, at the next poll, it senses
                    # the device idling
                    await time_mock.async_tick(_when - current_epoch)
                    break
                else:
                    if target_position in (mc.ROLLERSHUTTER_POSITION_CLOSED, mc.ROLLERSHUTTER_POSITION_OPENED):
                        # this is a special transition type for emulated position covers in meross_lan
                        # since due to expected behavior (not that I really agree with this expectation)
                        # the cover will run a full close/open when asked for 0/100 as target position
                        pass  # let it loop until transition_end_epoch_max
                    else:
                        # the entity is timing the transition and it ends when then
                        # internal _transition_end_unsub kicks-in
                        _transition_end_unsub = entity._transition_end_unsub
                        assert _transition_end_unsub, "missing transition_end callback"
                        assert transition_end_epoch == _transition_end_unsub.when()
                        await time_mock.async_tick(transition_end_epoch - current_epoch)
                        break
            # kicks an entity transition polling
            await time_mock.async_tick(_when - current_epoch)
            current_epoch = loop_time()

        assert entity._transition_unsub is None, "transition still pending"

        assert (state := self.hass_states.get(self.entity_id))
        expected_state = haec.STATE_OPEN if target_position else haec.STATE_CLOSED
        assert (
            state.state == expected_state
        ), f"finished {haec.SERVICE_SET_COVER_POSITION}({target_position}): state=={state.state}"
        current_position = state.attributes[haec.ATTR_CURRENT_POSITION]
        assert (
            current_position == target_position
        ), f"finished {haec.SERVICE_SET_COVER_POSITION}({target_position}): current_position=={current_position}"

    async def _async_test_stop(self):
        state = await self.async_service_call(haec.SERVICE_STOP_COVER)
        excpected_state = (
            haec.STATE_OPEN
            if state.attributes[haec.ATTR_CURRENT_POSITION] > 0
            else haec.STATE_CLOSED
        )
        assert (
            state.state == excpected_state
        ), f"{haec.SERVICE_STOP_COVER}: state=={state.state}"

    """
    async def _async_warp_shutter_transition(
        self, entity: MLRollerShutter, timeout_sec: float
    ):
        ""Advances the time mocker up to the timeout (delta or absolute)
        stepping exactly through each single polling loop.""
        time_mock = self.device_context._time_mock
        loop_time = self.hass.loop.time

        if not entity._position_native_isgood:
            # the entity is controlling the transition and it ends when then
            # internal timer kicks-in
            _transition_end_unsub = entity._transition_end_unsub
            assert _transition_end_unsub
            timeout_sec = _transition_end_unsub.when() - loop_time()

        timeout = time_mock.time() + timedelta(seconds=timeout_sec)
        while time_mock.time() < timeout:
            # Advances the time mocker up to the next transition polling cycle and executes it
            _transition_unsub = entity._transition_unsub
            assert _transition_unsub
            await time_mock.async_tick(_transition_unsub.when() - loop_time())
    """
