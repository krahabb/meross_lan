from time import time
import typing

from homeassistant.helpers import entity_registry
from homeassistant.util.dt import now

from ..binary_sensor import MLBinarySensor
from ..const import (
    CONF_PROTOCOL_HTTP,
    PARAM_GARAGEDOOR_TRANSITION_MAXDURATION,
    PARAM_GARAGEDOOR_TRANSITION_MINDURATION,
)
from ..cover import MLCover
from ..helpers import clamp, get_entity_last_state_available, schedule_async_callback
from ..helpers.namespaces import NamespaceHandler, SmartPollingStrategy
from ..merossclient import const as mc, request_get
from ..number import MLConfigNumber
from ..switch import MLSwitch

if typing.TYPE_CHECKING:
    from ..meross_device import MerossDevice

# garagedoor extra attributes
NOTIFICATION_ID_TIMEOUT = "garagedoor_timeout"
EXTRA_ATTR_TRANSITION_DURATION = "transition_duration"
EXTRA_ATTR_TRANSITION_TIMEOUT = (
    "transition_timeout"  # the time at which the transition timeout occurred
)
EXTRA_ATTR_TRANSITION_TARGET = (
    "transition_target"  # the target state which was not reached
)


class MLGarage(MLCover):

    binary_sensor_timeout: "MLGarageTimeoutBinarySensor"
    number_signalClose: "MLGarageMultipleConfigNumber | None"
    number_signalOpen: "MLGarageMultipleConfigNumber | None"
    switch_buzzerEnable: "MLGarageMultipleConfigSwitch | None"
    switch_doorEnable: "MLGarageDoorEnableSwitch | None"

    # HA core entity attributes:
    supported_features: MLCover.EntityFeature = (
        MLCover.EntityFeature.OPEN | MLCover.EntityFeature.CLOSE
    )

    __slots__ = (
        "_transition_duration",
        "_transition_start",
        "binary_sensor_timeout",
        "number_signalClose",
        "number_signalOpen",
        "switch_buzzerEnable",
        "switch_doorEnable",
    )

    def __init__(self, manager: "MerossDevice", channel: object):
        self._transition_duration = (
            PARAM_GARAGEDOOR_TRANSITION_MAXDURATION
            + PARAM_GARAGEDOOR_TRANSITION_MINDURATION
        ) / 2
        self._transition_start = 0.0
        self.extra_state_attributes = {
            EXTRA_ATTR_TRANSITION_DURATION: self._transition_duration
        }
        super().__init__(manager, channel, MLCover.DeviceClass.GARAGE)
        ability = manager.descriptor.ability
        manager.register_parser(mc.NS_APPLIANCE_GARAGEDOOR_STATE, self)
        if mc.NS_APPLIANCE_CONTROL_TOGGLEX in ability:
            manager.register_parser(mc.NS_APPLIANCE_CONTROL_TOGGLEX, self)
        self.binary_sensor_timeout = MLGarageTimeoutBinarySensor(self)
        if mc.NS_APPLIANCE_GARAGEDOOR_MULTIPLECONFIG in ability:
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
            manager.register_parser(mc.NS_APPLIANCE_GARAGEDOOR_MULTIPLECONFIG, self)
        else:
            self.number_signalClose = None
            self.number_signalOpen = None
            self.switch_buzzerEnable = None
            self.switch_doorEnable = None

    # interface: MerossEntity
    async def async_shutdown(self):
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
                    self.extra_state_attributes[EXTRA_ATTR_TRANSITION_DURATION] = (
                        self._transition_duration
                    )

    # interface: cover.CoverEntity
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
            example (historical) payload in SETACK:
            {"state": {"channel": 0, "open": 0, "lmTime": 0, "execute": 1}}
            "open" reports the current state and not the command
            "execute" represents command ack (I guess: never seen this == 0)
            Beware: if the garage is 'closed' and we send a 'close' "execute" will
            be replied as "1" and the garage will stay closed
            Update (2023-10-29): the trace in issue #272 shows "execute" == 0 when
            the command is not executed because already opened (maybe fw is smarter now)
            Update (2024-01-02): issue #361 points to the fact the payload is a list and
            so it looks that even garageDoors are (fully) moving to a 'channelized' struct
            {"state": [{"channel": 0, "open": 0, "lmTime": 0, "execute": 1}]}
            """
            self._transition_cancel()
            try:
                p_state = response[mc.KEY_PAYLOAD][mc.KEY_STATE]
                if isinstance(p_state, list):
                    # we eventually expect a 1 item list with our channel of course
                    p_state = p_state[0]
                _open = p_state[mc.KEY_OPEN]
                self.is_closed = not _open
                if p_state.get(mc.KEY_EXECUTE) and open_request != _open:
                    self._transition_start = self.manager.lastresponse
                    if open_request:
                        self.is_closing = False
                        self.is_opening = True
                        try:
                            timeout = self.number_signalOpen.native_value  # type: ignore
                        except AttributeError:
                            # this happens (once) when we don't have MULTIPLECONFIG ns support
                            # we'll then try use the 'x device' CONFIG or (since it could be missing)
                            # just build an emulated config entity
                            self.number_signalOpen = self.manager.entities.get(
                                f"config_{mc.KEY_DOOROPENDURATION}"
                            ) or MLGarageEmulatedConfigNumber(  # type: ignore
                                self, mc.KEY_DOOROPENDURATION
                            )
                            timeout = self.number_signalOpen.native_value  # type: ignore
                    else:
                        self.is_closing = True
                        self.is_opening = False
                        try:
                            timeout = self.number_signalClose.native_value  # type: ignore
                        except AttributeError:
                            # this happens (once) when we don't have MULTIPLECONFIG ns support
                            # we'll then try use the 'x device' CONFIG or (since it could be missing)
                            # just build an emulated config entity
                            self.number_signalClose = self.manager.entities.get(
                                f"config_{mc.KEY_DOORCLOSEDURATION}"
                            ) or MLGarageEmulatedConfigNumber(  # type: ignore
                                self, mc.KEY_DOORCLOSEDURATION
                            )
                            timeout = self.number_signalClose.native_value  # type: ignore

                    self._transition_unsub = schedule_async_callback(
                        self.hass, 0.9, self._async_transition_callback
                    )
                    # check the timeout 1 sec after expected to account
                    # for delays in communication
                    self._transition_end_unsub = schedule_async_callback(
                        self.hass,
                        (timeout or self._transition_duration) + 1,  # type: ignore
                        self._async_transition_end_callback,
                    )

                self.flush_state()

            except Exception as exception:
                self.log_exception(
                    self.WARNING,
                    exception,
                    "async_request_position (payload:%s)",
                    str(response[mc.KEY_PAYLOAD]),
                )

    def _parse_state(self, payload: dict):
        # {"channel": 0, "open": 1, "lmTime": 0}
        is_closed = not payload[mc.KEY_OPEN]
        if self._transition_start:
            if self.is_closed == is_closed:
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
            if is_closed:
                transition_duration = self.manager.lastresponse - self._transition_start
                # autoregression filtering applying 20% of last updated sample
                self._update_transition_duration(
                    int((4 * self._transition_duration + transition_duration) / 5)
                )
                self._transition_cancel()

        if self.is_closed != is_closed:
            self.is_closed = is_closed
            self.flush_state()

    def _parse_config(self, payload):
        if mc.KEY_SIGNALCLOSE in payload:
            self.number_signalClose.update_device_value(payload[mc.KEY_SIGNALCLOSE])  # type: ignore
        if mc.KEY_SIGNALOPEN in payload:
            self.number_signalOpen.update_device_value(payload[mc.KEY_SIGNALOPEN])  # type: ignore
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
        """
        pass

    def _transition_cancel(self):
        self.is_closing = False
        self.is_opening = False
        self._transition_start = 0.0
        super()._transition_cancel()

    async def _async_transition_callback(self):
        self._transition_unsub = None
        manager = self.manager
        if manager.curr_protocol is CONF_PROTOCOL_HTTP and not manager._mqtt_active:
            await manager.async_http_request(
                *request_get(mc.NS_APPLIANCE_GARAGEDOOR_STATE)
            )

    async def _async_transition_end_callback(self):
        """
        checks the transition did finish as per the timeout(s)
        """
        self._transition_end_unsub = None
        was_closing = self.is_closing
        if was_closing:
            # when closing we expect this callback not to be called since
            # the transition should be terminated by '_set_open' provided it gets
            # called on time (on polling this is not guaranteed).
            # If we're here, we still havent received a proper 'physical close'
            # because our configured closeduration is too short
            # or the garage didnt close at all
            if self._transition_duration < (time() - self._transition_start):
                self._update_transition_duration(self._transition_duration + 1)

        self.is_closing = False
        self.is_opening = False
        self._transition_start = 0.0

        if was_closing != self.is_closed:
            # looks like on MQTT we don't receive a PUSHed state update? (#415)
            if await self.manager.async_request_ack(
                *request_get(mc.NS_APPLIANCE_GARAGEDOOR_STATE)
            ):
                # the request/response parse already flushed the state
                if was_closing == self.is_closed:
                    self.binary_sensor_timeout.update_ok()
                else:
                    self.binary_sensor_timeout.update_timeout(
                        MLCover.ENTITY_COMPONENT.STATE_CLOSED
                        if was_closing
                        else MLCover.ENTITY_COMPONENT.STATE_OPEN
                    )
            else:
                self.flush_state()
                self.binary_sensor_timeout.update_timeout(
                    MLCover.ENTITY_COMPONENT.STATE_CLOSED
                    if was_closing
                    else MLCover.ENTITY_COMPONENT.STATE_OPEN
                )
        else:
            self.flush_state()
            self.binary_sensor_timeout.update_ok()

    def _update_transition_duration(self, transition_duration):
        self._transition_duration = clamp(
            transition_duration,
            PARAM_GARAGEDOOR_TRANSITION_MINDURATION,
            PARAM_GARAGEDOOR_TRANSITION_MAXDURATION,
        )
        self.extra_state_attributes[EXTRA_ATTR_TRANSITION_DURATION] = (
            self._transition_duration
        )


class MLGarageTimeoutBinarySensor(MLBinarySensor):

    # HA core entity attributes:
    _attr_available = True
    entity_category = MLBinarySensor.EntityCategory.DIAGNOSTIC

    def __init__(self, cover: MLGarage):
        self.extra_state_attributes = {}
        super().__init__(
            cover.manager,
            cover.channel,
            "problem",
            self.DeviceClass.PROBLEM,
            onoff=False,
        )

    def set_available(self):
        pass

    def set_unavailable(self):
        pass

    def update_ok(self):
        self.extra_state_attributes.pop(EXTRA_ATTR_TRANSITION_TIMEOUT, None)
        self.extra_state_attributes.pop(EXTRA_ATTR_TRANSITION_TARGET, None)
        self.update_onoff(False)

    def update_timeout(self, target_state):
        self.extra_state_attributes[EXTRA_ATTR_TRANSITION_TARGET] = target_state
        self.extra_state_attributes[EXTRA_ATTR_TRANSITION_TIMEOUT] = now().isoformat()
        self.is_on = True
        self.flush_state()


class MLGarageMultipleConfigSwitch(MLSwitch):
    """
    switch entity to manage MSG configuration (buzzer, enable)
    'x channel' through mc.NS_APPLIANCE_GARAGEDOOR_MULTIPLECONFIG
    """

    manager: "MerossDevice"

    namespace = mc.NS_APPLIANCE_GARAGEDOOR_MULTIPLECONFIG
    key_namespace = mc.KEY_CONFIG

    # HA core entity attributes:
    entity_category = MLSwitch.EntityCategory.CONFIG

    def __init__(
        self,
        manager: "MerossDevice",
        channel,
        key: str,
        *,
        onoff=None,
    ):
        self.key_value = key
        self.name = key
        super().__init__(
            manager,
            channel,
            f"config_{key}",
            self.DeviceClass.SWITCH,
            onoff=onoff,
        )


class MLGarageDoorEnableSwitch(MLGarageMultipleConfigSwitch):
    """
    Dedicated entity for "doorEnable" config option in mc.NS_APPLIANCE_GARAGEDOOR_MULTIPLECONFIG
    in order to try enable/disable the same channel associated entities in HA too
    when done with the Meross app (#330)
    """

    def update_onoff(self, onoff):
        if self.is_on != onoff:
            self.is_on = onoff
            self.flush_state()
            registry_update_entity = self.get_entity_registry().async_update_entity
            disabler = entity_registry.RegistryEntryDisabler.INTEGRATION
            for entity in self.manager.entities.values():
                if (
                    (entity.channel == self.channel)
                    and (entity is not self)
                    and (entry := entity.registry_entry)
                ):
                    if onoff:
                        if entry.disabled_by == disabler:
                            registry_update_entity(entry.entity_id, disabled_by=None)
                    else:
                        if not entry.disabled_by:
                            registry_update_entity(
                                entry.entity_id, disabled_by=disabler
                            )


class MLGarageConfigSwitch(MLGarageMultipleConfigSwitch):
    """
    switch entity to manage MSG configuration (buzzer)
    'x device' through mc.NS_APPLIANCE_GARAGEDOOR_CONFIG
    """

    namespace = mc.NS_APPLIANCE_GARAGEDOOR_CONFIG
    key_namespace = mc.KEY_CONFIG

    def __init__(self, manager: "MerossDevice", key: str, payload: dict):
        super().__init__(
            manager,
            None,
            key,
            onoff=payload[key],
        )


class MLGarageMultipleConfigNumber(MLConfigNumber):
    """
    number entity to manage MSG configuration (open/close timeout and the likes)
    'x channel' through mc.NS_APPLIANCE_GARAGEDOOR_MULTIPLECONFIG
    """

    namespace = mc.NS_APPLIANCE_GARAGEDOOR_MULTIPLECONFIG
    key_namespace = mc.KEY_CONFIG

    device_scale = 1000
    # HA core entity attributes:
    # these are ok for open/close durations
    # customize those when needed...
    native_max_value = 60
    native_min_value = 1
    native_step = 1

    def __init__(
        self, manager: "MerossDevice", channel, key: str, *, device_value=None
    ):
        self.key_value = key
        self.name = key
        super().__init__(
            manager,
            channel,
            f"config_{key}",
            self.DEVICE_CLASS_DURATION,
            device_value=device_value,
        )


class MLGarageConfigNumber(MLGarageMultipleConfigNumber):
    """
    number entity to manage MSG configuration (open/close timeout and the likes)
    'x device' through mc.NS_APPLIANCE_GARAGEDOOR_CONFIG
    """

    def __init__(self, manager: "MerossDevice", key: str, payload: dict):
        super().__init__(manager, None, key, device_value=payload[key])

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

    # HA core entity attributes:
    _attr_available = True

    def __init__(self, garage: MLGarage, key: str):
        super().__init__(
            garage.manager,
            garage.channel,
            key,
            device_value=garage._transition_duration * self.device_scale,
        )

    def set_available(self):
        pass

    def set_unavailable(self):
        pass

    async def async_added_to_hass(self):
        await super().async_added_to_hass()
        with self.exception_warning("restoring previous state"):
            if last_state := await get_entity_last_state_available(
                self.hass, self.entity_id
            ):
                self.native_value = float(last_state.state)  # type: ignore

    async def async_set_native_value(self, value: float):
        self.update_native_value(value)


class GarageDoorConfigNamespaceHandler(NamespaceHandler):

    number_signalDuration: MLGarageConfigNumber
    switch_buzzerEnable: MLGarageConfigSwitch
    number_doorOpenDuration: MLGarageMultipleConfigNumber
    number_doorCloseDuration: MLGarageMultipleConfigNumber

    __slots__ = (
        "number_signalDuration",
        "switch_buzzerEnable",
        "number_doorOpenDuration",
        "number_doorCloseDuration",
    )

    def __init__(self, device: "MerossDevice"):
        self.number_signalDuration = None  # type: ignore
        self.switch_buzzerEnable = None  # type: ignore
        self.number_doorOpenDuration = None  # type: ignore
        self.number_doorCloseDuration = None  # type: ignore
        super().__init__(
            device,
            mc.NS_APPLIANCE_GARAGEDOOR_CONFIG,
            handler=self._handle_Appliance_GarageDoor_Config,
        )
        SmartPollingStrategy(device, mc.NS_APPLIANCE_GARAGEDOOR_CONFIG)

    def _handle_Appliance_GarageDoor_Config(self, header: dict, payload: dict):
        # {"config": {"signalDuration": 1000, "buzzerEnable": 0, "doorOpenDuration": 30000, "doorCloseDuration": 30000}}
        payload = payload[mc.KEY_CONFIG]
        if mc.KEY_SIGNALDURATION in payload:
            try:
                self.number_signalDuration.update_device_value(
                    payload[mc.KEY_SIGNALDURATION]
                )
            except AttributeError:
                self.number_signalDuration = MLGarageConfigNumber(
                    self.device,
                    mc.KEY_SIGNALDURATION,
                    payload,
                )
                self.number_signalDuration.native_step = 0.1
                self.number_signalDuration.native_min_value = 0.1

        if mc.KEY_BUZZERENABLE in payload:
            try:
                self.switch_buzzerEnable.update_onoff(payload[mc.KEY_BUZZERENABLE])
            except AttributeError:
                self.switch_buzzerEnable = MLGarageConfigSwitch(
                    self.device, mc.KEY_BUZZERENABLE, payload
                )

        if mc.KEY_DOOROPENDURATION in payload:
            # this config key has been removed in recent firmwares
            # now we have door open/close duration set per channel (#82)
            # but legacy ones still manage this
            try:
                self.number_doorOpenDuration.update_device_value(  # type: ignore
                    payload[mc.KEY_DOOROPENDURATION]
                )
            except AttributeError:
                self.number_doorOpenDuration = MLGarageConfigNumber(
                    self.device,
                    mc.KEY_DOOROPENDURATION,
                    payload,
                )
        else:
            # no config for KEY_DOOROPENDURATION: we'll let every channel manage it's own
            if not self.number_doorOpenDuration:  # use as a guard...
                device = self.device
                for channel_payload in device.channels_payloads:
                    garage: MLGarage = device.entities[channel_payload[mc.KEY_CHANNEL]]  # type: ignore
                    # in case MULTIPLECONFIG is supported this code does nothing
                    # since everything is already in place
                    garage.number_signalOpen = (
                        garage.number_signalOpen
                        or MLGarageEmulatedConfigNumber(garage, mc.KEY_DOOROPENDURATION)
                    )
                    # set guard so we don't repeat this 'late conditional init'
                    self.number_doorOpenDuration = garage.number_signalOpen

        if mc.KEY_DOORCLOSEDURATION in payload:
            # this config key has been removed in recent firmwares
            # now we have door open/close duration set per channel (#82)
            try:
                self.number_doorCloseDuration.update_device_value(  # type: ignore
                    payload[mc.KEY_DOORCLOSEDURATION]
                )
            except AttributeError:
                self.number_doorCloseDuration = MLGarageConfigNumber(
                    self.device,
                    mc.KEY_DOORCLOSEDURATION,
                    payload,
                )
        else:
            # no config for KEY_DOORCLOSEDURATION: we'll let every channel manage it's own
            if not self.number_doorCloseDuration:  # use as a guard...
                device = self.device
                for channel_payload in device.channels_payloads:
                    garage: MLGarage = device.entities[channel_payload[mc.KEY_CHANNEL]]  # type: ignore
                    # in case MULTIPLECONFIG is supported this code does nothing
                    # since everything is already in place
                    garage.number_signalClose = (
                        garage.number_signalClose
                        or MLGarageEmulatedConfigNumber(
                            garage, mc.KEY_DOORCLOSEDURATION
                        )
                    )
                    # set guard so we don't repeat this 'late conditional init'
                    self.number_doorCloseDuration = garage.number_signalClose


def digest_init_garageDoor(device: "MerossDevice", digest: list):
    device.platforms.setdefault(MLConfigNumber.PLATFORM, None)
    device.platforms.setdefault(MLSwitch.PLATFORM, None)
    ability = device.descriptor.ability
    for channel_digest in digest:
        channel = channel_digest[mc.KEY_CHANNEL]
        MLGarage(device, channel)
        device.channels_payloads.append({mc.KEY_CHANNEL: channel})

    if mc.NS_APPLIANCE_GARAGEDOOR_CONFIG in ability:
        GarageDoorConfigNamespaceHandler(device)

    if mc.NS_APPLIANCE_GARAGEDOOR_MULTIPLECONFIG in ability:
        SmartPollingStrategy(
            device,
            mc.NS_APPLIANCE_GARAGEDOOR_MULTIPLECONFIG,
            payload=device.channels_payloads,
            item_count=len(device.channels_payloads),
        )

    return device.namespace_handlers[mc.NS_APPLIANCE_GARAGEDOOR_STATE].parse_list
