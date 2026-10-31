from unittest.mock import AsyncMock, patch

from homeassistant.components import cover as haec

from custom_components.meross_lan import const as mlc
from custom_components.meross_lan.cover import Cover
from custom_components.meross_lan.devices.garagedoor import GarageDoor
from custom_components.meross_lan.devices.rollershutter import RollerShutter
from custom_components.meross_lan.merossclient.protocol import (
    const as mc,
    namespaces as mn,
)
from emulator.mixins.rollershutter import RollerShutterMixin

from tests.entities import EntityComponentTest


class EntityTest(EntityComponentTest):

    ENTITY_TYPE = haec.CoverEntity

    DIGEST_ENTITIES = {
        mc.KEY_GARAGEDOOR: [GarageDoor],
    }

    NAMESPACES_ENTITIES = {
        mn.Appliance_RollerShutter_State: [RollerShutter],
    }

    COVER_TRANSITIONS = {
        haec.CoverState.OPEN.value: (
            haec.SERVICE_CLOSE_COVER,
            haec.CoverState.CLOSING,
            haec.CoverState.CLOSED,
        ),
        haec.CoverState.CLOSED.value: (
            haec.SERVICE_OPEN_COVER,
            haec.CoverState.OPENING,
            haec.CoverState.OPEN,
        ),
    }

    async def async_test_each_callback(self, entity: Cover):
        await super().async_test_each_callback(entity)

        if isinstance(entity, GarageDoor):
            assert (
                entity.supported_features
                == haec.CoverEntityFeature.OPEN | haec.CoverEntityFeature.CLOSE
            )
            self._check_remove_togglex(entity)
            if entity.parent.descriptor.type.startswith(mc.TYPE_MSG150):
                config = entity.parent.ns_handlers[mn.Appliance_GarageDoor_Config]
                self.expected_entity_types.append(type(config.parsers[mc.KEY_LED]))
                self.expected_entity_types.append(type(config.parsers[mc.KEY_OBENABLE]))
                self.expected_entity_types.append(
                    type(config.parsers[mc.KEY_LOCKREMOTE])
                )
                self.expected_entity_types.append(
                    type(entity.binary_sensor_obstruction)
                )
                self.expected_entity_types.append(
                    type(config.parsers[mc.KEY_BUZZERDELAYTIME])
                )

        elif isinstance(entity, RollerShutter):
            assert (
                entity.supported_features
                >= haec.CoverEntityFeature.OPEN
                | haec.CoverEntityFeature.CLOSE
                | haec.CoverEntityFeature.STOP
            )
            assert entity.number_signalClose.ns_value == RollerShutterMixin.SIGNALCLOSE
            assert entity.number_signalOpen.ns_value == RollerShutterMixin.SIGNALOPEN

    async def async_test_enabled_callback(self, entity: Cover):
        get_hass_state = EntityComponentTest.get_hass_state
        if isinstance(entity, GarageDoor):
            await self._async_test_cover_transition(entity)
            await self._async_test_cover_transition(entity)
            if entity.parent.descriptor.type.startswith(mc.TYPE_MSG150):
                await self._async_test_msg150_recheck(entity)
                await self._async_test_msg150_light(entity)
                await self._async_test_msg150_obstruction(entity)
                buzzer = entity.parent.ns_handlers[
                    mn.Appliance_GarageDoor_Config
                ].parsers[mc.KEY_BUZZERENABLE]
                await buzzer.async_turn_off()
                await self._async_test_cover_transition(entity)
                await self._async_test_cover_transition(entity)
                await buzzer.async_turn_on()
                await self._async_test_msg150_activity(entity)
        elif isinstance(entity, RollerShutter):
            # RollerShutter could need at least a run to enable
            # support for SET_POSITION
            # this should open the cover (emulator starts with closed)
            await self._async_test_cover_transition(entity)
            assert haec.CoverEntityFeature.OPEN in entity.supported_features
            # this should close the cover
            state = await self._async_test_cover_transition(entity)
            assert (
                state.attributes[haec.ATTR_CURRENT_POSITION] == 0
            ), f"{haec.ATTR_CURRENT_POSITION}!=0"

            # open a bit up to 30% ensuring it transitions correctly
            await self._async_test_set_position(entity, 30)

            # now we'll interrupt a transition
            await self.async_service_call(
                haec.SERVICE_SET_COVER_POSITION, {haec.ATTR_POSITION: 60}
            )
            # advance the time a bit
            await self.device_context.time_mock.async_tick(
                RollerShutter.PARAM_TRANSITION_POLL_TIMEOUT
            )
            # ensure we're still opening
            assert (state := get_hass_state(self.entity_id))
            assert (
                state.state == haec.CoverState.OPENING
            ), f"{haec.SERVICE_SET_COVER_POSITION}: state!={haec.CoverState.OPENING}"
            # now stop
            await self._async_test_stop()

            # also test setting position to fully close/open
            # since there are some subtleties with emulated position runs
            await self._async_test_set_position(entity, 0)
            await self._async_test_set_position(entity, 100)

    async def async_test_disabled_callback(self, entity: Cover):
        pass

    async def _async_test_msg150_recheck(self, entity: GarageDoor):
        handler = entity.handler_ns
        for error in (None, TimeoutError):
            entity._transition_cancel()
            entity.is_closed = False
            entity.is_closing = True
            start = entity._transition_start = entity.time()
            with patch.object(
                type(handler), "async_get", new=AsyncMock(side_effect=error)
            ):
                await entity._async_transition_end_callback()
                assert entity._transition_start == start
                assert entity._transition_timeout_retry is True
                assert entity.is_closing is True
                assert not entity.binary_sensor_timeout.is_on
                timer = entity._timers[entity._async_transition_end_callback]
                assert abs(timer.when() - self.hass.loop.time() - 5) < 1
                entity.cancel_callback(entity._async_transition_end_callback)
                await entity._async_transition_end_callback()
                assert entity.binary_sensor_timeout.is_on
                assert entity._async_transition_end_callback not in entity._timers
            entity._transition_cancel()
            await handler.async_get(entity.index)
            entity.binary_sensor_timeout.update_ok(True)

        # A final position arriving during the extra check cancels its timer.
        entity.is_closed = False
        entity.is_closing = True
        entity._transition_start = entity.time()
        with patch.object(type(handler), "async_get", new=AsyncMock()):
            await entity._async_transition_end_callback()
        await handler.async_get(entity.index)
        assert entity.is_closed is True
        assert not entity.binary_sensor_timeout.is_on
        assert entity._async_transition_end_callback not in entity._timers

    async def _async_test_msg150_light(self, entity: GarageDoor):
        handler = entity.parent.ns_handlers[mn.Appliance_GarageDoor_Config]
        light = handler.parsers[mc.KEY_LED]
        assert light.name == "Opener light"
        assert light.entity_category is None
        assert handler.polling_period == 10
        assert handler.polling_period_cloud == 1200
        config = self.device_context.emulator.namespaces[handler.id][mc.KEY_CONFIG]
        # The wall panel or motion sensor can change the light independently.
        # A config poll should then update the entity through the normal parser.
        for value, expected in ((1, True), (2, False), (0, None), (2, False)):
            config[mc.KEY_LED] = value
            await handler.async_get()
            assert light.is_on is expected

    async def _async_test_msg150_obstruction(self, entity: GarageDoor):
        handler = entity.handler_ns
        sensor = entity.binary_sensor_obstruction
        state = self.device_context.emulator.descriptor.digest[mc.KEY_GARAGEDOOR][0]
        is_closed = entity.is_closed
        for value, expected in ((1, True), (2, False), (0, None), (3, None), (1, True)):
            state[mc.KEY_OBSTRUCTION] = value
            state[mc.KEY_OBLMTIME] += 1
            await handler.async_get(entity.index)
            assert sensor.is_on is expected
            assert entity.is_closed is is_closed
            assert entity.extra_state_attributes[mc.KEY_OBSTRUCTION] == value
            assert (
                entity.extra_state_attributes[mc.KEY_OBLMTIME] == state[mc.KEY_OBLMTIME]
            )
        # A missing obstruction field leaves the last reported state intact.
        del state[mc.KEY_OBSTRUCTION]
        await handler.async_get(entity.index)
        assert sensor.is_on is True
        state[mc.KEY_OBSTRUCTION] = 2
        await handler.async_get(entity.index)
        assert sensor.is_on is False

    async def _async_test_msg150_activity(self, entity: GarageDoor):
        from homeassistant.auth.const import GROUP_ID_ADMIN
        from homeassistant.components.logbook.models import (
            LogbookConfig,
            async_event_to_row,
        )
        from homeassistant.components.logbook.processor import EventProcessor
        from homeassistant.const import EVENT_STATE_CHANGED
        from homeassistant.core import Context

        from custom_components.meross_lan.logbook import (
            EVENT_GARAGEDOOR_TRIGGER,
            async_describe_events,
        )
        from custom_components.meross_lan.merossclient.protocol.message import (
            MerossMessage,
        )

        hass = self.hass
        events = []
        unsub_trigger = hass.bus.async_listen(EVENT_GARAGEDOOR_TRIGGER, events.append)
        unsub_state = hass.bus.async_listen(EVENT_STATE_CHANGED, events.append)
        external_events = {}
        async_describe_events(
            hass,
            lambda domain, event_type, describe: external_events.update(
                {event_type: (domain, describe)}
            ),
        )
        previous_logbook = hass.data.get("logbook")
        hass.data["logbook"] = LogbookConfig(external_events)
        state = self.device_context.emulator.descriptor.digest[mc.KEY_GARAGEDOOR][0]

        async def notify(source, opened, method=mc.METHOD_PUSH, as_list=True):
            state[mc.KEY_OPEN] = opened
            entity.handler_ns.handle(
                MerossMessage.build(
                    mn.Appliance_GarageDoor_State,
                    method,
                    {mc.KEY_STATE: [dict(state)] if as_list else dict(state)},
                    self.device_context.emulator.key,
                    triggerSrc=source,
                ),
                False,
            )
            await hass.async_block_till_done()

        def trigger_events():
            return [
                event
                for event in events
                if event.event_type == EVENT_GARAGEDOOR_TRIGGER
            ]

        try:
            for source, label, opened in (
                ("WallButton", "Wall Panel", 1),
                ("iOS", "App", 0),
                ("Android", "App", 1),
                ("DevicePysical", "Remote", 0),
                ("DevicePhysical", "Remote", 1),
            ):
                before = len(trigger_events())
                await notify(source, opened, as_list=bool(opened))
                assert len(trigger_events()) == before + 1
                assert trigger_events()[-1].data["source"] == label
                await notify(source, opened)
                assert len(trigger_events()) == before + 1
            before = len(trigger_events())
            await notify("Unrecognized", 0)
            await notify("WallButton", 1, mc.METHOD_GETACK)
            assert len(trigger_events()) == before

            user = await hass.auth.async_create_user(
                "Test garage user", group_ids=[GROUP_ID_ADMIN]
            )
            await hass.services.async_call(
                haec.DOMAIN,
                haec.SERVICE_CLOSE_COVER,
                {"entity_id": entity.entity_id},
                blocking=True,
                context=Context(user_id=user.id),
            )
            await notify("DevicePysical", 0)
            assert (
                trigger_events()[-1].data["source"]
                == "Home Assistant (Test garage user)"
            )
            assert trigger_events()[-1].context.user_id == user.id
            processor = EventProcessor(hass, [EVENT_GARAGEDOOR_TRIGGER])
            entries = processor.humanify(
                [async_event_to_row(event) for event in events]
            )
            entries = [
                entry for entry in entries if entry.get("entity_id") == entity.entity_id
            ]
            assert any(
                entry.get("context_name") == "Garage Door triggered by Wall Panel"
                for entry in entries
            )
            assert any(
                entry.get("context_name")
                == "Garage Door triggered by Home Assistant (Test garage user)"
                and entry.get("context_user_id") == user.id
                for entry in entries
            )
            await self.device_context.time_mock.async_warp(40, tick=1)
        finally:
            unsub_trigger()
            unsub_state()
            if previous_logbook is None:
                hass.data.pop("logbook", None)
            else:
                hass.data["logbook"] = previous_logbook

    async def _async_test_cover_transition(self, entity):
        """Start and follow the transition from open to close or
        close to open depending on current state."""
        get_hass_state = EntityComponentTest.get_hass_state
        assert (state := get_hass_state(self.entity_id))
        trans = self.COVER_TRANSITIONS[state.state]
        state = await self.async_service_call(trans[0])
        assert state.state == trans[1], trans[1]
        if isinstance(entity, GarageDoor) and entity.parent.descriptor.type.startswith(
            mc.TYPE_MSG150
        ):
            # MSG150 echoes the target in SETACK before movement finishes.
            # The cover should still show movement until a state GET confirms it.
            assert entity.is_closed == (trans[0] == haec.SERVICE_OPEN_COVER)
            config = self.device_context.emulator.namespaces[
                mn.Appliance_GarageDoor_Config
            ][mc.KEY_CONFIG]
            opening = trans[0] == haec.SERVICE_OPEN_COVER
            expected_timeout = (
                config[mc.KEY_DOOROPENDURATION if opening else mc.KEY_DOORCLOSEDURATION]
                / 1000
            )
            if not opening and config[mc.KEY_BUZZERENABLE]:
                expected_timeout += config[mc.KEY_BUZZERDELAYTIME] / 1000
            timer = entity._timers[entity._async_transition_end_callback]
            assert abs(timer.when() - self.hass.loop.time() - expected_timeout) < 1
        # The GarageDoor/RollerShutter state machine has a timed callback mechanism
        # TODO: use and check that callback instead of the raw 60 seconds timeout
        # we're using async_warp over 40 seconds (which are enough on our emulators
        # to complete the transition) so to better 'match' the callbacks state refresh
        await self.device_context.time_mock.async_warp(
            40,
            tick=1,
        )
        assert (state := get_hass_state(self.entity_id))
        assert state.state == trans[2], trans[2]
        return state

    async def _async_test_set_position(
        self, entity: RollerShutter, target_position: int
    ):
        state = await self.async_service_call(
            haec.SERVICE_SET_COVER_POSITION, {haec.ATTR_POSITION: target_position}
        )
        current_position = state.attributes[haec.ATTR_CURRENT_POSITION]
        # check the cover is moving
        if target_position > current_position:
            expected_state = haec.CoverState.OPENING
            expected_duration = (
                RollerShutterMixin.SIGNALOPEN
                * (target_position - current_position)
                / 100000
            )
            expected_duration_max = RollerShutterMixin.SIGNALOPEN / 1000
        else:
            expected_state = haec.CoverState.CLOSING
            expected_duration = (
                RollerShutterMixin.SIGNALCLOSE
                * (current_position - target_position)
                / 100000
            )
            expected_duration_max = RollerShutterMixin.SIGNALCLOSE / 1000
        expected_duration_max += RollerShutterMixin.SIGNAL_TRANSITION_PERIOD
        assert (
            state.state == expected_state
        ), f"started {haec.SERVICE_SET_COVER_POSITION}({target_position}): state!={expected_state}"

        # process the transition state machine
        time_mock = self.device_context.time_mock
        loop_time = self.hass.loop.time
        current_epoch = loop_time()
        transition_end_epoch = current_epoch + expected_duration
        transition_end_epoch_max = current_epoch + expected_duration_max

        while current_epoch < transition_end_epoch_max:
            # Advances the time mocker up to the next transition polling cycle and executes it
            _transition_timer = entity._timers.get(entity._transition_callback)
            if not _transition_timer:
                break
            _when = _transition_timer.when()
            if _when >= transition_end_epoch:
                if entity._position_native_isgood:
                    # the entity just monitors the device state
                    # and it should end the transition when, at the next poll, it senses
                    # the device idling
                    await time_mock.async_tick(_when - current_epoch)
                    break
                else:
                    if target_position in (
                        mc.ROLLERSHUTTER_POSITION_CLOSED,
                        mc.ROLLERSHUTTER_POSITION_OPENED,
                    ):
                        # this is a special transition type for emulated position covers in meross_lan
                        # since due to expected behavior (not that I really agree with this expectation)
                        # the cover will run a full close/open when asked for 0/100 as target position
                        pass  # let it loop until transition_end_epoch_max
                    else:
                        # the entity is timing the transition and it ends when then
                        # internal _async_transition_end_callback kicks-in
                        _transition_end_timer = entity._timers.get(
                            entity._async_transition_end_callback
                        )
                        assert _transition_end_timer, "missing transition_end callback"
                        assert transition_end_epoch == _transition_end_timer.when()
                        await time_mock.async_tick(transition_end_epoch - current_epoch)
                        break
            # kicks an entity transition polling
            await time_mock.async_tick(_when - current_epoch)
            current_epoch = loop_time()

        _transition_timer = entity._timers.get(entity._transition_callback)
        assert (
            _transition_timer is None
        ), f"transition to {target_position} still pending:current_epoch=={current_epoch} transition_end={_transition_timer.when()}"

        assert (state := EntityComponentTest.get_hass_state(self.entity_id))
        expected_state = (
            haec.CoverState.OPEN if target_position else haec.CoverState.CLOSED
        )
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
            haec.CoverState.OPEN
            if state.attributes[haec.ATTR_CURRENT_POSITION] > 0
            else haec.CoverState.CLOSED
        )
        assert (
            state.state == excpected_state
        ), f"{haec.SERVICE_STOP_COVER}: state=={state.state}"
