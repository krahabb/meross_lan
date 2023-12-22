from __future__ import annotations

from logging import DEBUG
from time import time
import typing

from homeassistant.components import cover
from homeassistant.components.cover import (
    ATTR_CURRENT_POSITION,
    ATTR_POSITION,
    STATE_CLOSED,
    STATE_CLOSING,
    STATE_OPEN,
    STATE_OPENING,
    CoverDeviceClass,
    CoverEntityFeature,
)
from homeassistant.const import TIME_SECONDS
from homeassistant.core import callback
from homeassistant.helpers import entity_registry
from homeassistant.util.dt import now

from . import meross_entity as me
from .binary_sensor import MLBinarySensor
from .const import (
    CONF_PROTOCOL_HTTP,
    PARAM_GARAGEDOOR_TRANSITION_MAXDURATION,
    PARAM_GARAGEDOOR_TRANSITION_MINDURATION,
)
from .helpers import (
    PollingStrategy,
    SmartPollingStrategy,
    clamp,
    get_entity_last_state_available,
    schedule_async_callback,
    schedule_callback,
    versiontuple,
)
from .merossclient import const as mc, get_default_arguments
from .number import MLConfigNumber
from .switch import MLSwitch

if typing.TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

    from .meross_device import MerossDevice, MerossDeviceDescriptor

STATE_MAP = {0: STATE_CLOSED, 1: STATE_OPEN}

POSITION_FULLY_CLOSED = 0
POSITION_FULLY_OPENED = 100

# garagedoor extra attributes
NOTIFICATION_ID_TIMEOUT = "garagedoor_timeout"
EXTRA_ATTR_TRANSITION_DURATION = "transition_duration"
EXTRA_ATTR_TRANSITION_TIMEOUT = (
    "transition_timeout"  # the time at which the transition timeout occurred
)
EXTRA_ATTR_TRANSITION_TARGET = (
    "transition_target"  # the target state which was not reached
)

# rollershutter extra attributes
EXTRA_ATTR_DURATION_OPEN = "duration_open"
EXTRA_ATTR_DURATION_CLOSE = "duration_close"
EXTRA_ATTR_POSITION_NATIVE = "position_native"


async def async_setup_entry(
    hass: HomeAssistant, config_entry: ConfigEntry, async_add_devices
):
    me.platform_setup_entry(hass, config_entry, async_add_devices, cover.DOMAIN)


class MLGarageTimeoutBinarySensor(MLBinarySensor):
    _attr_entity_category = MLBinarySensor.EntityCategory.DIAGNOSTIC

    def __init__(self, cover: MLGarage):
        super().__init__(
            cover.manager, cover.channel, "problem", self.DeviceClass.PROBLEM
        )
        self._attr_extra_state_attributes = {}
        self._attr_state = self.STATE_OFF

    @property
    def available(self):
        return True

    def set_unavailable(self):
        pass

    def update_ok(self):
        self._attr_extra_state_attributes.pop(EXTRA_ATTR_TRANSITION_TIMEOUT, None)
        self._attr_extra_state_attributes.pop(EXTRA_ATTR_TRANSITION_TARGET, None)
        self.update_onoff(0)

    def update_timeout(self, target_state):
        self._attr_extra_state_attributes[EXTRA_ATTR_TRANSITION_TARGET] = target_state
        self._attr_extra_state_attributes[
            EXTRA_ATTR_TRANSITION_TIMEOUT
        ] = now().isoformat()
        self.update_onoff(1)


class MLGarageMultipleConfigSwitch(MLSwitch):
    """
    switch entity to manage MSG configuration (buzzer, enable)
    'x channel' through mc.NS_APPLIANCE_GARAGEDOOR_MULTIPLECONFIG
    """

    manager: GarageMixin

    _attr_entity_category = MLSwitch.EntityCategory.CONFIG

    def __init__(self, manager: GarageMixin, channel, key: str):
        self.key_onoff = key
        self._attr_name = key
        super().__init__(manager, channel, f"config_{key}", None)

    async def async_request_onoff(self, onoff: int):
        if await self.manager.async_request_ack(
            mc.NS_APPLIANCE_GARAGEDOOR_MULTIPLECONFIG,
            mc.METHOD_SET,
            {
                mc.KEY_CONFIG: [
                    {
                        mc.KEY_CHANNEL: self.channel,
                        self.key_onoff: onoff,
                    }
                ]
            },
        ):
            self.update_onoff(onoff)


class MLGarageDoorEnableSwitch(MLGarageMultipleConfigSwitch):
    """
    Dedicated entity for "doorEnable" config option in mc.NS_APPLIANCE_GARAGEDOOR_MULTIPLECONFIG
    in order to try enable/disable the same channel associated entities in HA too
    when done with the Meross app (#330)
    """

    def update_onoff(self, onoff):
        MLGarageMultipleConfigSwitch.update_onoff(self, onoff)
        registry = entity_registry.async_get(self.hass)
        disabler = entity_registry.RegistryEntryDisabler.INTEGRATION
        for entity in self.manager.entities.values():
            if (
                (entity.channel == self.channel)
                and (entity is not self)
                and (entry := entity.registry_entry)
            ):
                if onoff and entry.disabled_by is disabler:
                    registry.async_update_entity(entry.entity_id, disabled_by=None)
                elif not onoff and not entry.disabled_by:
                    registry.async_update_entity(entry.entity_id, disabled_by=disabler)


class MLGarageConfigSwitch(MLGarageMultipleConfigSwitch):
    """
    switch entity to manage MSG configuration (buzzer)
    'x device' through mc.NS_APPLIANCE_GARAGEDOOR_CONFIG
    """

    def __init__(self, manager: GarageMixin, key: str, init_payload: dict):
        super().__init__(manager, None, key)
        self._attr_state = self.STATE_ON if init_payload[key] else self.STATE_OFF

    async def async_request_onoff(self, onoff: int):
        if await self.manager.async_request_ack(
            mc.NS_APPLIANCE_GARAGEDOOR_CONFIG,
            mc.METHOD_SET,
            {mc.KEY_CONFIG: {self.key_onoff: onoff}},
        ):
            self.update_onoff(onoff)


class MLGarageMultipleConfigNumber(MLConfigNumber):
    """
    number entity to manage MSG configuration (open/close timeout and the likes)
    'x channel' through mc.NS_APPLIANCE_GARAGEDOOR_MULTIPLECONFIG
    """

    manager: GarageMixin

    namespace = mc.NS_APPLIANCE_GARAGEDOOR_MULTIPLECONFIG
    key_namespace = mc.KEY_CONFIG

    def __init__(self, manager: GarageMixin, channel, key: str):
        self.key_value = key
        self._attr_name = key
        # these are ok for open/close durations
        # customize those when needed...
        self._attr_native_max_value = 60
        self._attr_native_min_value = 1
        self._attr_native_step = 1
        super().__init__(manager, channel, f"config_{key}")

    @property
    def native_unit_of_measurement(self):
        return TIME_SECONDS

    @property
    def device_scale(self):
        return 1000


class MLGarageConfigNumber(MLGarageMultipleConfigNumber):
    """
    number entity to manage MSG configuration (open/close timeout and the likes)
    'x device' through mc.NS_APPLIANCE_GARAGEDOOR_CONFIG
    """

    def __init__(self, manager: GarageMixin, key: str, init_payload: dict):
        super().__init__(manager, None, key)
        self._attr_state = init_payload[key] / self.device_scale

    async def async_request(self, device_value):
        return await self.manager.async_request_ack(
            mc.NS_APPLIANCE_GARAGEDOOR_CONFIG,
            mc.METHOD_SET,
            {mc.KEY_CONFIG: {self.key_value: device_value}},
        )


class MLGarageEmulatedConfigNumber(MLGarageMultipleConfigNumber):
    """
    number entity to manage MSG configuration (open/close timeout)
    'x channel' when mc.NS_APPLIANCE_GARAGEDOOR_MULTIPLECONFIG is unavailable
    and the mc.NS_APPLIANCE_GARAGEDOOR_CONFIG too does not carry open/close
    timeouts (this happens particularly on fw 3.2.7 as per #338).
    This entity will just provide an 'HA only' storage for these parameters
    """

    @property
    def available(self):
        return True

    def set_unavailable(self):
        pass

    async def async_added_to_hass(self):
        await super().async_added_to_hass()
        if self._attr_state is None:
            self._attr_state = self.manager.entities[self.channel]._transition_duration  # type: ignore
            with self.exception_warning("restoring previous state"):
                if last_state := await get_entity_last_state_available(
                    self.hass, self.entity_id
                ):
                    self._attr_state = float(last_state.state)  # type: ignore

    async def async_set_native_value(self, value: float):
        self.update_state(value)


class MLGarage(me.MerossEntity, cover.CoverEntity):
    PLATFORM = cover.DOMAIN

    manager: GarageMixin
    binary_sensor_timeout: MLGarageTimeoutBinarySensor
    number_signalClose: MLGarageMultipleConfigNumber | None
    number_signalOpen: MLGarageMultipleConfigNumber | None
    switch_buzzerEnable: MLGarageMultipleConfigSwitch | None
    switch_doorEnable: MLGarageDoorEnableSwitch | None

    __slots__ = (
        "_transition_duration",
        "_transition_start",
        "_transition_unsub",
        "_transition_end_unsub",
        "_open",
        "_open_request",
        "binary_sensor_timeout",
        "number_signalClose",
        "number_signalOpen",
        "switch_buzzerEnable",
        "switch_doorEnable",
    )

    def __init__(self, manager: GarageMixin, channel: object):
        super().__init__(manager, channel, None, CoverDeviceClass.GARAGE)
        self._transition_duration = (
            PARAM_GARAGEDOOR_TRANSITION_MAXDURATION
            + PARAM_GARAGEDOOR_TRANSITION_MINDURATION
        ) / 2
        self._transition_start = None
        self._transition_unsub = None
        self._transition_end_unsub = None
        # this is the last known (or actual) physical state from device state
        self._open = None
        # cache issued request since device reply doesnt report it
        self._open_request = None
        self._attr_extra_state_attributes = {
            EXTRA_ATTR_TRANSITION_DURATION: self._transition_duration
        }
        self.binary_sensor_timeout = MLGarageTimeoutBinarySensor(self)
        if mc.NS_APPLIANCE_GARAGEDOOR_MULTIPLECONFIG in manager.descriptor.ability:
            self.number_signalClose = MLGarageMultipleConfigNumber(
                manager, channel, mc.KEY_SIGNALCLOSE
            )
            self.number_signalOpen = MLGarageMultipleConfigNumber(
                manager, channel, mc.KEY_SIGNALOPEN
            )
            self.switch_buzzerEnable = MLGarageMultipleConfigSwitch(
                manager, channel, mc.KEY_BUZZERENABLE
            )
            self.switch_doorEnable = MLGarageDoorEnableSwitch(
                manager, channel, mc.KEY_DOORENABLE
            )
        else:
            self.number_signalClose = None
            self.number_signalOpen = None
            self.switch_buzzerEnable = None
            self.switch_doorEnable = None

    # interface: MerossEntity
    async def async_shutdown(self):
        self._transition_cancel()
        await super().async_shutdown()
        self.binary_sensor_timeout = None  # type: ignore
        self.number_signalClose = None
        self.number_signalOpen = None
        self.switch_buzzerEnable = None
        self.switch_doorEnable = None

    async def async_added_to_hass(self):
        await super().async_added_to_hass()
        """
        we're trying to recover the '_transition_duration' from previous state
        """
        with self.exception_warning("restoring previous state"):
            if last_state := await get_entity_last_state_available(
                self.hass, self.entity_id
            ):
                _attr = last_state.attributes  # type: ignore
                if EXTRA_ATTR_TRANSITION_DURATION in _attr:
                    # restore anyway besides PARAM_RESTORESTATE_TIMEOUT
                    # since this is no harm and unlikely to change
                    # better than defaulting to a pseudo-random value
                    self._transition_duration = _attr[EXTRA_ATTR_TRANSITION_DURATION]
                    self._attr_extra_state_attributes[
                        EXTRA_ATTR_TRANSITION_DURATION
                    ] = self._transition_duration

    async def async_will_remove_from_hass(self):
        self._transition_cancel()
        await super().async_will_remove_from_hass()

    def set_unavailable(self):
        self._open = None
        self._transition_cancel()
        super().set_unavailable()

    # interface: cover.CoverEntity
    @property
    def supported_features(self):
        return CoverEntityFeature.OPEN | CoverEntityFeature.CLOSE

    @property
    def is_opening(self):
        return self._attr_state == STATE_OPENING

    @property
    def is_closing(self):
        return self._attr_state == STATE_CLOSING

    @property
    def is_closed(self):
        return self._attr_state == STATE_CLOSED

    async def async_open_cover(self, **kwargs):
        await self.async_request_position(1)

    async def async_close_cover(self, **kwargs):
        await self.async_request_position(0)

    # interface: self
    async def async_request_position(self, open_request: int):
        if response := await self.manager.async_request_ack(
            mc.NS_APPLIANCE_GARAGEDOOR_STATE,
            mc.METHOD_SET,
            {mc.KEY_STATE: {mc.KEY_CHANNEL: self.channel, mc.KEY_OPEN: open_request}},
        ):
            """
            example payload in SETACK:
            {"state": {"channel": 0, "open": 0, "lmTime": 0, "execute": 1}}
            "open" reports the current state and not the command
            "execute" represents command ack (I guess: never seen this == 0)
            Beware: if the garage is 'closed' and we send a 'close' "execute" will
            be replied as "1" and the garage will stay closed
            Update (2023-10-29): the trace in issue #272 shows "execute" == 0 when
            the command is not executed because already opened (maybe fw is smarter now)
            """
            self._transition_cancel()
            p_state: dict = response[mc.KEY_PAYLOAD][mc.KEY_STATE]
            self._open = p_state[mc.KEY_OPEN]
            if p_state.get(mc.KEY_EXECUTE) and open_request != self._open:
                self._open_request = open_request
                self._transition_start = time()
                self.update_state(STATE_OPENING if open_request else STATE_CLOSING)
                if open_request:
                    try:
                        timeout = self.number_signalOpen.native_value  # type: ignore
                    except AttributeError:
                        # this happens (once) when we don't have MULTIPLECONFIG ns support
                        # we'll then try use the 'x device' CONFIG or (since it could be missing)
                        # just build an emulated config entity
                        self.number_signalOpen = (
                            self.manager.number_doorOpenDuration
                            or MLGarageEmulatedConfigNumber(
                                self.manager, self.channel, mc.KEY_DOOROPENDURATION
                            )
                        )
                        timeout = self.number_signalOpen.native_value
                else:
                    try:
                        timeout = self.number_signalClose.native_value  # type: ignore
                    except AttributeError:
                        # this happens (once) when we don't have MULTIPLECONFIG ns support
                        # we'll then try use the 'x device' CONFIG or (since it could be missing)
                        # just build an emulated config entity
                        self.number_signalClose = (
                            self.manager.number_doorCloseDuration
                            or MLGarageEmulatedConfigNumber(
                                self.manager, self.channel, mc.KEY_DOORCLOSEDURATION
                            )
                        )
                        timeout = self.number_signalClose.native_value

                self._transition_unsub = schedule_async_callback(
                    self.hass, 0.9, self._async_transition_callback
                )
                # check the timeout 1 sec after expected to account
                # for delays in communication
                self._transition_end_unsub = schedule_callback(
                    self.hass,
                    (timeout or self._transition_duration) + 1,  # type: ignore
                    self._transition_end_callback,
                )
            else:
                self.update_state(STATE_MAP.get(self._open))

    def _parse_state(self, payload: dict):
        # {"channel": 0, "open": 1, "lmTime": 0}
        self._open = _open = payload[mc.KEY_OPEN]
        if not self._transition_start:
            # our state machine is idle and we could be receiving a
            # state change triggered by any external means (app, remote)
            self.update_state(STATE_MAP.get(_open))
            return

        # state will be updated on _transition_end_callback
        # but we monitor the contact switch in order to
        # update our estimates for transition duration
        if self._open_request != _open:
            # keep monitoring the transition in less than 1 sec
            if not self._transition_unsub:
                self._transition_unsub = schedule_async_callback(
                    self.hass, 0.9, self._async_transition_callback
                )
            return

        # We're "in transition" and the physical contact has reached the target.
        # we can monitor the (sampled) exact time when the garage closes to
        # estimate the transition_duration and dynamically update it since
        # during the transition the state will be closed only at the end
        # while during opening the garagedoor contact will open right at the beginning
        # and so will be unuseful
        # Also to note: if we're on HTTP this sampled time could happen anyway after the 'real'
        # state switched to 'closed' so we're likely going to measure in exceed of real transition duration
        if not _open:
            transition_duration = self.manager.lastresponse - self._transition_start
            # autoregression filtering applying 20% of last updated sample
            self._update_transition_duration(
                int((4 * self._transition_duration + transition_duration) / 5)
            )
            self._transition_cancel()
            self.update_state(STATE_CLOSED)

        # garage contact is opened but since it opens way sooner than the transition
        # ending we'll wait our transition_end in order to update the state

    def _parse_config(self, payload):
        if mc.KEY_SIGNALCLOSE in payload:
            self.number_signalClose.update_native_value(payload[mc.KEY_SIGNALCLOSE])  # type: ignore
        if mc.KEY_SIGNALOPEN in payload:
            self.number_signalOpen.update_native_value(payload[mc.KEY_SIGNALOPEN])  # type: ignore
        if mc.KEY_BUZZERENABLE in payload:
            self.switch_buzzerEnable.update_onoff(payload[mc.KEY_BUZZERENABLE])  # type: ignore
        if mc.KEY_DOORENABLE in payload:
            self.switch_doorEnable.update_onoff(payload[mc.KEY_DOORENABLE])  # type: ignore

    def _parse_togglex(self, payload: dict):
        """
        MSG100 exposes a 'togglex' interface so my code interprets that as a switch state
        Here we'll intercept that behaviour and right now the guess is:
        The toggle state represents the contact of the garagedoor which is likely a short
        pulse so we'll use it to guess state transitions in our cover (disabled this until further knowledge)

        if onoff:
            if self._attr_state == STATE_CLOSED:
                self._start_transition(STATE_OPEN)
            elif self._attr_state == STATE_OPEN:
                self._start_transition(STATE_CLOSED)
        #else: RIP!
        """
        pass

    def _transition_cancel(self):
        if self._transition_unsub:
            self._transition_unsub.cancel()
            self._transition_unsub = None
        if self._transition_end_unsub:
            self._transition_end_unsub.cancel()
            self._transition_end_unsub = None
        self._open_request = None
        self._transition_start = None

    async def _async_transition_callback(self):
        self._transition_unsub = None
        manager = self.manager
        if manager.curr_protocol is CONF_PROTOCOL_HTTP and not manager._mqtt_active:
            await manager.async_http_request(
                *get_default_arguments(mc.NS_APPLIANCE_GARAGEDOOR_STATE)
            )

    @callback
    def _transition_end_callback(self):
        """
        checks the transition did finish as per the timeout(s)
        """
        self._transition_end_unsub = None
        if not self._open_request:
            # when closing we expect this callback not to be called since
            # the transition should be terminated by '_set_open' provided it gets
            # called on time (on polling this is not guaranteed).
            # If we're here, we still havent received a proper 'physical close'
            # because our configured closeduration is too short
            # or the garage didnt close at all
            transition_duration = time() - self._transition_start  # type: ignore
            if self._transition_duration < transition_duration:
                self._update_transition_duration(self._transition_duration + 1)

        self.update_state(STATE_MAP.get(self._open))  # type: ignore
        if self._open_request == self._open:
            self.binary_sensor_timeout.update_ok()
        else:
            self.binary_sensor_timeout.update_timeout(STATE_MAP.get(self._open_request))  # type: ignore

        self._open_request = None
        self._transition_start = None

    def _update_transition_duration(self, transition_duration):
        self._transition_duration = clamp(
            transition_duration,
            PARAM_GARAGEDOOR_TRANSITION_MINDURATION,
            PARAM_GARAGEDOOR_TRANSITION_MAXDURATION,
        )
        self._attr_extra_state_attributes[
            EXTRA_ATTR_TRANSITION_DURATION
        ] = self._transition_duration


class GarageMixin(
    MerossDevice if typing.TYPE_CHECKING else object
):  # pylint: disable=used-before-assignment
    number_signalDuration: MLGarageConfigNumber = None  # type: ignore
    switch_buzzerEnable: MLGarageConfigSwitch = None  # type: ignore
    number_doorOpenDuration: MLGarageMultipleConfigNumber = None  # type: ignore
    number_doorCloseDuration: MLGarageMultipleConfigNumber = None  # type: ignore

    def __init__(self, descriptor: MerossDeviceDescriptor, entry):
        self._polling_payload = []
        super().__init__(descriptor, entry)
        self.platforms.setdefault(MLConfigNumber.PLATFORM, None)
        self.platforms.setdefault(MLSwitch.PLATFORM, None)
        if mc.NS_APPLIANCE_GARAGEDOOR_CONFIG in descriptor.ability:
            self.polling_dictionary[
                mc.NS_APPLIANCE_GARAGEDOOR_CONFIG
            ] = SmartPollingStrategy(mc.NS_APPLIANCE_GARAGEDOOR_CONFIG)
        if mc.NS_APPLIANCE_GARAGEDOOR_MULTIPLECONFIG in descriptor.ability:
            self.polling_dictionary[
                mc.NS_APPLIANCE_GARAGEDOOR_MULTIPLECONFIG
            ] = SmartPollingStrategy(
                mc.NS_APPLIANCE_GARAGEDOOR_MULTIPLECONFIG,
                {mc.KEY_CONFIG: self._polling_payload},
            )

    async def async_shutdown(self):
        await super().async_shutdown()
        self.number_signalDuration = None  # type: ignore
        self.switch_buzzerEnable = None  # type: ignore
        self.number_doorOpenDuration = None  # type: ignore
        self.number_doorCloseDuration = None  # type: ignore

    def _init_garageDoor(self, payload: dict):
        MLGarage(self, channel := payload[mc.KEY_CHANNEL])
        self._polling_payload.append({mc.KEY_CHANNEL: channel})

    def _handle_Appliance_GarageDoor_State(self, header: dict, payload: dict):
        self._parse__generic(mc.KEY_STATE, payload.get(mc.KEY_STATE))

    def _handle_Appliance_GarageDoor_Config(self, header: dict, payload: dict):
        # {"config": {"signalDuration": 1000, "buzzerEnable": 0, "doorOpenDuration": 30000, "doorCloseDuration": 30000}}
        payload = payload[mc.KEY_CONFIG]
        if mc.KEY_SIGNALDURATION in payload:
            try:
                self.number_signalDuration.update_native_value(
                    payload[mc.KEY_SIGNALDURATION]
                )
            except AttributeError:
                self.number_signalDuration = MLGarageConfigNumber(
                    self,
                    mc.KEY_SIGNALDURATION,
                    payload,
                )
                self.number_signalDuration._attr_native_step = 0.1
                self.number_signalDuration._attr_native_min_value = 0.1

        if mc.KEY_BUZZERENABLE in payload:
            try:
                self.switch_buzzerEnable.update_onoff(payload[mc.KEY_BUZZERENABLE])
            except AttributeError:
                self.switch_buzzerEnable = MLGarageConfigSwitch(
                    self, mc.KEY_BUZZERENABLE, payload
                )

        if mc.KEY_DOOROPENDURATION in payload:
            # this config key has been removed in recent firmwares
            # now we have door open/close duration set per channel (#82)
            # but legacy ones still manage this
            try:
                self.number_doorOpenDuration.update_native_value(  # type: ignore
                    payload[mc.KEY_DOOROPENDURATION]
                )
            except AttributeError:
                self.number_doorOpenDuration = MLGarageConfigNumber(
                    self,
                    mc.KEY_DOOROPENDURATION,
                    payload,
                )
        else:
            # no config for KEY_DOOROPENDURATION: we'll let every channel manage it's own
            if not self.number_doorOpenDuration:  # use as a guard...
                for i in self._polling_payload:
                    channel = i[mc.KEY_CHANNEL]
                    garage: MLGarage = self.entities[channel]  # type: ignore
                    # in case MULTIPLECONFIG is supported this code does nothing
                    # since everything is already in place
                    garage.number_signalOpen = (
                        garage.number_signalOpen
                        or MLGarageEmulatedConfigNumber(
                            self, channel, mc.KEY_DOOROPENDURATION
                        )
                    )
                    # set guard so we don't repeat this 'late conditional init'
                    self.number_doorOpenDuration = garage.number_signalOpen

        if mc.KEY_DOORCLOSEDURATION in payload:
            # this config key has been removed in recent firmwares
            # now we have door open/close duration set per channel (#82)
            try:
                self.number_doorCloseDuration.update_native_value(  # type: ignore
                    payload[mc.KEY_DOORCLOSEDURATION]
                )
            except AttributeError:
                self.number_doorCloseDuration = MLGarageConfigNumber(
                    self,
                    mc.KEY_DOORCLOSEDURATION,
                    payload,
                )
        else:
            # no config for KEY_DOORCLOSEDURATION: we'll let every channel manage it's own
            if not self.number_doorCloseDuration:  # use as a guard...
                for i in self._polling_payload:
                    channel = i[mc.KEY_CHANNEL]
                    garage: MLGarage = self.entities[channel]  # type: ignore
                    # in case MULTIPLECONFIG is supported this code does nothing
                    # since everything is already in place
                    garage.number_signalClose = (
                        garage.number_signalClose
                        or MLGarageEmulatedConfigNumber(
                            self, channel, mc.KEY_DOORCLOSEDURATION
                        )
                    )
                    # set guard so we don't repeat this 'late conditional init'
                    self.number_doorCloseDuration = garage.number_signalClose

    def _handle_Appliance_GarageDoor_MultipleConfig(self, header: dict, payload: dict):
        """
        payload := {
            "config": [
                {"channel": 1,"doorEnable": 1,"timestamp": 0,"timestampMs": 0,"signalClose": 2000,"signalOpen": 2000,"buzzerEnable": 1},
                {"channel": 2,"doorEnable": 0,"timestamp": 1699130744,"timestampMs": 87,"signalClose": 2000,"signalOpen": 2000,"buzzerEnable": 1},
                {"channel": 3,"doorEnable": 0,"timestamp": 1699130748,"timestampMs": 663,"signalClose": 2000,"signalOpen": 2000,"buzzerEnable": 1},
            ]
        }
        """
        self._parse__generic(mc.KEY_CONFIG, payload.get(mc.KEY_CONFIG))

    def _parse_garageDoor(self, payload):
        self._parse__generic(mc.KEY_STATE, payload)


class MLRollerShutter(me.MerossEntity, cover.CoverEntity):
    """
    MRS100 SHUTTER ENTITY
    """

    PLATFORM = cover.DOMAIN

    manager: RollerShutterMixin

    __slots__ = (
        "number_signalOpen",
        "number_signalClose",
        "_signalOpen",
        "_signalClose",
        "_position_native",
        "_position_start",
        "_position_starttime",
        "_position_endtime",
        "_transition_unsub",
        "_transition_end_unsub",
    )

    def __init__(self, manager: RollerShutterMixin, channel: object):
        super().__init__(manager, channel, None, CoverDeviceClass.SHUTTER)
        self.number_signalOpen = MLRollerShutterConfigNumber(self, mc.KEY_SIGNALOPEN)
        self.number_signalClose = MLRollerShutterConfigNumber(self, mc.KEY_SIGNALCLOSE)
        self._signalOpen: int = 30000  # msec to fully open (config'd on device)
        self._signalClose: int = 30000  # msec to fully close (config'd on device)
        self._position_native = None  # as reported by the device
        self._position_start = None  # set when when we're controlling a timed position
        self._position_starttime = None  # epoch of transition start
        self._position_endtime = None  # epoch of 'target position reached'
        self._transition_unsub = None
        self._transition_end_unsub = None
        self._attr_current_cover_position: int | None = None
        self._attr_extra_state_attributes = {}
        # flag indicating the device position is reliable (#227)
        # this will anyway be set in case we 'decode' a meaningful device position
        try:
            self._position_native_isgood = versiontuple(
                manager.descriptor.firmwareVersion
            ) >= versiontuple("6.6.6")
        except Exception:
            self._position_native_isgood = None

    @property
    def assumed_state(self):
        """RollerShutter position is unreliable"""
        return True

    @property
    def supported_features(self):
        return (
            CoverEntityFeature.OPEN
            | CoverEntityFeature.CLOSE
            | CoverEntityFeature.STOP
            | CoverEntityFeature.SET_POSITION
        )

    @property
    def is_opening(self):
        return self._attr_state == STATE_OPENING

    @property
    def is_closing(self):
        return self._attr_state == STATE_CLOSING

    @property
    def is_closed(self):
        return self._attr_state == STATE_CLOSED

    async def async_added_to_hass(self):
        await super().async_added_to_hass()
        """
        we're trying to recover the 'timed' position from previous state
        if it happens it wasn't updated too far in time
        """
        with self.exception_warning("restoring previous state"):
            if last_state := await get_entity_last_state_available(
                self.hass, self.entity_id
            ):
                _attr = last_state.attributes  # type: ignore
                if EXTRA_ATTR_DURATION_OPEN in _attr:
                    self._signalOpen = _attr[EXTRA_ATTR_DURATION_OPEN]
                    self._attr_extra_state_attributes[
                        EXTRA_ATTR_DURATION_OPEN
                    ] = self._signalOpen
                if EXTRA_ATTR_DURATION_CLOSE in _attr:
                    self._signalClose = _attr[EXTRA_ATTR_DURATION_CLOSE]
                    self._attr_extra_state_attributes[
                        EXTRA_ATTR_DURATION_CLOSE
                    ] = self._signalClose
                if ATTR_CURRENT_POSITION in _attr:
                    self._attr_current_cover_position = _attr[ATTR_CURRENT_POSITION]

    async def async_will_remove_from_hass(self):
        self._transition_cancel()
        await super().async_will_remove_from_hass()

    async def async_open_cover(self, **kwargs):
        await self.async_request_position(POSITION_FULLY_OPENED)

    async def async_close_cover(self, **kwargs):
        await self.async_request_position(POSITION_FULLY_CLOSED)

    async def async_set_cover_position(self, **kwargs):
        if ATTR_POSITION in kwargs:
            position = kwargs[ATTR_POSITION]
            if (
                self._position_native_isgood
                or (position == POSITION_FULLY_OPENED)
                or (position == POSITION_FULLY_CLOSED)
            ):
                # ensure a full 'untimed' run when asked for
                # fully opened/closed (#170)
                await self.async_request_position(position)
            else:
                if position > self._attr_current_cover_position:
                    await self.async_request_position(
                        POSITION_FULLY_OPENED,
                        (
                            (position - self._attr_current_cover_position)
                            * self._signalOpen
                        )
                        / 100000,
                    )
                elif position < self._attr_current_cover_position:
                    await self.async_request_position(
                        POSITION_FULLY_CLOSED,
                        (
                            (self._attr_current_cover_position - position)
                            * self._signalClose
                        )
                        / 100000,
                    )

    async def async_stop_cover(self, **kwargs):
        await self.async_request_position(-1)

    def request_position(self, position: int, timeout: float | None = None):
        self.hass.async_create_task(self.async_request_position(position, timeout))

    async def async_request_position(self, position: int, timeout: float | None = None):
        self._transition_cancel()
        if await self.manager.async_request_ack(
            mc.NS_APPLIANCE_ROLLERSHUTTER_POSITION,
            mc.METHOD_SET,
            {
                mc.KEY_POSITION: {
                    mc.KEY_CHANNEL: self.channel,
                    mc.KEY_POSITION: position,
                }
            },
        ):
            # re-ensure current transitions are clean after await
            self._transition_cancel()
            self._transition_unsub = schedule_async_callback(
                self.hass, 0, self._async_transition_callback
            )
            if timeout is not None:
                self._position_endtime = time() + timeout
                self._transition_end_unsub = schedule_callback(
                    self.hass, timeout, self._transition_end_callback
                )

    def set_unavailable(self):
        self._transition_cancel()
        super().set_unavailable()

    def _parse_position(self, payload: dict):
        """
        legacy devices only reported 0 or 100 as position
        so we used to store this as an extra attribute and perform
        a trajectory calculation to emulate time based positioning
        now (#227) we'll detect devices reporting 'actual' good
        positioning and switch entity behaviour to trust this value
        bypassing all of the 'time based' emulation
        """
        if not isinstance(position := payload.get(mc.KEY_POSITION), int):
            return

        if self._position_native_isgood:
            if position != self._attr_current_cover_position:
                self._attr_current_cover_position = position
                if self._hass_connected:
                    self._async_write_ha_state()
            return

        if position == self._position_native:
            # no news...
            return

        if (position > 0) and (position < 100):
            # detecting a device reporting 'good' positions
            self._position_native_isgood = True
            self._position_native = None
            self._attr_extra_state_attributes.pop(EXTRA_ATTR_POSITION_NATIVE, None)
            self._attr_current_cover_position = position
        else:
            self._position_native = position
            self._attr_extra_state_attributes[EXTRA_ATTR_POSITION_NATIVE] = position
        if self._hass_connected:
            self._async_write_ha_state()

    def _parse_state(self, payload: dict):
        epoch = self.manager.lastresponse
        state = payload.get(mc.KEY_STATE)
        if self._position_native_isgood:
            if state == mc.ROLLERSHUTTER_STATE_OPENING:
                self.update_state(STATE_OPENING)
            elif state == mc.ROLLERSHUTTER_STATE_CLOSING:
                self.update_state(STATE_CLOSING)
            else:  # state == mc.ROLLERSHUTTER_STATE_IDLE:
                self._transition_cancel()
                self.update_state(
                    STATE_OPEN if self._attr_current_cover_position else STATE_CLOSED
                )
                return
        else:
            if self._attr_state == STATE_OPENING:
                self._attr_current_cover_position = int(self._position_start + ((epoch - self._position_starttime) * 100000) / self._signalOpen)  # type: ignore
                if self._attr_current_cover_position > POSITION_FULLY_OPENED:
                    self._attr_current_cover_position = POSITION_FULLY_OPENED
                if self._hass_connected and (state == mc.ROLLERSHUTTER_STATE_OPENING):
                    self._async_write_ha_state()
            elif self._attr_state == STATE_CLOSING:
                self._attr_current_cover_position = int(self._position_start - ((epoch - self._position_starttime) * 100000) / self._signalClose)  # type: ignore
                if self._attr_current_cover_position < POSITION_FULLY_CLOSED:
                    self._attr_current_cover_position = POSITION_FULLY_CLOSED
                if self._hass_connected and (state == mc.ROLLERSHUTTER_STATE_CLOSING):
                    self._async_write_ha_state()

            if state == mc.ROLLERSHUTTER_STATE_OPENING:
                if self._attr_state != STATE_OPENING:
                    self._position_start = (
                        self._attr_current_cover_position
                        if self._attr_current_cover_position is not None
                        else POSITION_FULLY_CLOSED
                    )
                    self._position_starttime = epoch
                    self.update_state(STATE_OPENING)
            elif state == mc.ROLLERSHUTTER_STATE_CLOSING:
                if self._attr_state != STATE_CLOSING:
                    self._position_start = (
                        self._attr_current_cover_position
                        if self._attr_current_cover_position is not None
                        else POSITION_FULLY_OPENED
                    )
                    self._position_starttime = epoch
                    self.update_state(STATE_CLOSING)
            else:  # state == mc.ROLLERSHUTTER_STATE_IDLE:
                self._transition_cancel()
                self.update_state(
                    STATE_OPEN if self.current_cover_position else STATE_CLOSED
                )
                return

        # here the cover is moving
        if self._position_endtime is not None:
            # in case our _close_calback has not yet been called or failed
            if epoch >= self._position_endtime:
                self.request_position(-1)
                return

        if not self._transition_unsub:
            # ensure we 'follow' cover movement
            self._transition_unsub = schedule_async_callback(
                self.hass, 2, self._async_transition_callback
            )

    def _parse_config(self, payload: dict):
        # payload = {"channel": 0, "signalOpen": 50000, "signalClose": 50000}
        if mc.KEY_SIGNALOPEN in payload:
            self._signalOpen = payload[
                mc.KEY_SIGNALOPEN
            ]  # time to fully open cover in msec
            self.number_signalOpen.update_native_value(self._signalOpen)
            self._attr_extra_state_attributes[
                EXTRA_ATTR_DURATION_OPEN
            ] = self._signalOpen
        if mc.KEY_SIGNALCLOSE in payload:
            self._signalClose = payload[
                mc.KEY_SIGNALCLOSE
            ]  # time to fully close cover in msec
            self.number_signalClose.update_native_value(self._signalClose)
            self._attr_extra_state_attributes[
                EXTRA_ATTR_DURATION_CLOSE
            ] = self._signalClose

    async def _async_transition_callback(self):
        self._transition_unsub = schedule_async_callback(
            self.hass, 2, self._async_transition_callback
        )
        manager = self.manager
        if manager.curr_protocol is CONF_PROTOCOL_HTTP and not manager._mqtt_active:
            await manager.async_http_request(
                *get_default_arguments(mc.NS_APPLIANCE_ROLLERSHUTTER_STATE)
            )
            if self._position_native_isgood:
                await manager.async_http_request(
                    *get_default_arguments(mc.NS_APPLIANCE_ROLLERSHUTTER_POSITION)
                )

    @callback
    def _transition_end_callback(self):
        self.log(DEBUG, "_transition_end_callback")
        self._transition_end_unsub = None
        self.request_position(-1)

    def _transition_cancel(self):
        self._position_endtime = None
        if self._transition_end_unsub:
            self._transition_end_unsub.cancel()
            self._transition_end_unsub = None
        if self._transition_unsub:
            self._transition_unsub.cancel()
            self._transition_unsub = None


class MLRollerShutterConfigNumber(MLConfigNumber):
    """
    Helper entity to configure MRS open/close duration
    """

    __slots__ = ("_cover",)

    def __init__(self, cover: MLRollerShutter, key: str):
        self._cover = cover
        self.key_value = key
        self._attr_name = key
        self._attr_native_max_value = 60
        self._attr_native_min_value = 1
        self._attr_native_step = 1
        super().__init__(cover.manager, cover.channel, f"config_{key}")

    @property
    def native_unit_of_measurement(self):
        return TIME_SECONDS

    @property
    def device_scale(self):
        return 1000

    async def async_request(self, device_value):
        config = {
            mc.KEY_CHANNEL: self.channel,
            mc.KEY_SIGNALOPEN: self._cover._signalOpen,
            mc.KEY_SIGNALCLOSE: self._cover._signalClose,
        }
        config[self.key_value] = device_value
        if response := await self.manager.async_request_ack(
            mc.NS_APPLIANCE_ROLLERSHUTTER_CONFIG,
            mc.METHOD_SET,
            {mc.KEY_CONFIG: [config]},
        ):
            self._cover._parse_config(config)

        return response


class RollerShutterMixin(
    MerossDevice if typing.TYPE_CHECKING else object
):  # pylint: disable=used-before-assignment
    def __init__(self, descriptor, entry):
        super().__init__(descriptor, entry)
        with self.exception_warning("RollerShutterMixin init"):
            # looks like digest (in NS_ALL) doesn't carry state
            # so we're not implementing _init_xxx and _parse_xxx methods here
            MLRollerShutter(self, 0)
            self.polling_dictionary[
                mc.NS_APPLIANCE_ROLLERSHUTTER_STATE
            ] = PollingStrategy(mc.NS_APPLIANCE_ROLLERSHUTTER_STATE)
            self.polling_dictionary[
                mc.NS_APPLIANCE_ROLLERSHUTTER_POSITION
            ] = PollingStrategy(mc.NS_APPLIANCE_ROLLERSHUTTER_POSITION)
            self.polling_dictionary[
                mc.NS_APPLIANCE_ROLLERSHUTTER_CONFIG
            ] = SmartPollingStrategy(mc.NS_APPLIANCE_ROLLERSHUTTER_CONFIG)

    def _handle_Appliance_RollerShutter_Position(self, header: dict, payload: dict):
        self._parse__array(mc.KEY_POSITION, payload.get(mc.KEY_POSITION))

    def _handle_Appliance_RollerShutter_State(self, header: dict, payload: dict):
        self._parse__array(mc.KEY_STATE, payload.get(mc.KEY_STATE))

    def _handle_Appliance_RollerShutter_Config(self, header: dict, payload: dict):
        self._parse__array(mc.KEY_CONFIG, payload.get(mc.KEY_CONFIG))
