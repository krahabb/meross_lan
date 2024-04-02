from __future__ import annotations

from time import time
import typing

from homeassistant.components import cover
from homeassistant.components.cover import (
    ATTR_CURRENT_POSITION,
    ATTR_POSITION,
    STATE_CLOSED,
    STATE_OPEN,
    CoverDeviceClass,
    CoverEntityFeature,
)
from homeassistant.core import callback
from homeassistant.exceptions import InvalidStateError
from homeassistant.helpers import entity_registry
from homeassistant.util.dt import now

from . import meross_entity as me
from .binary_sensor import MLBinarySensor
from .const import (
    CONF_PROTOCOL_HTTP,
    PARAM_GARAGEDOOR_TRANSITION_MAXDURATION,
    PARAM_GARAGEDOOR_TRANSITION_MINDURATION,
    PARAM_ROLLERSHUTTER_TRANSITION_POLL_TIMEOUT,
)
from .helpers import (
    clamp,
    get_entity_last_state_available,
    schedule_async_callback,
    schedule_callback,
    versiontuple,
)
from .helpers.namespaces import PollingStrategy, SmartPollingStrategy
from .merossclient import const as mc, request_get
from .number import MLConfigNumber
from .switch import MLSwitch

if typing.TYPE_CHECKING:

    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

    from .meross_device import MerossDevice


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

    manager: GarageMixin

    entity_category = MLSwitch.EntityCategory.CONFIG

    def __init__(
        self,
        manager: GarageMixin,
        channel,
        key: str,
        *,
        onoff=None,
        namespace=mc.NS_APPLIANCE_GARAGEDOOR_MULTIPLECONFIG,
    ):
        self.key_value = key
        self.name = key
        super().__init__(
            manager,
            channel,
            f"config_{key}",
            self.DeviceClass.SWITCH,
            onoff=onoff,
            namespace=namespace,
        )

    async def async_request_onoff(self, onoff: int):
        if await self.manager.async_request_ack(
            mc.NS_APPLIANCE_GARAGEDOOR_MULTIPLECONFIG,
            mc.METHOD_SET,
            {
                mc.KEY_CONFIG: [
                    {
                        mc.KEY_CHANNEL: self.channel,
                        self.key_value: onoff,
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

    def __init__(self, manager: GarageMixin, key: str, payload: dict):
        super().__init__(
            manager,
            None,
            key,
            onoff=payload[key],
            namespace=mc.NS_APPLIANCE_GARAGEDOOR_CONFIG,
        )

    async def async_request_onoff(self, onoff: int):
        if await self.manager.async_request_ack(
            mc.NS_APPLIANCE_GARAGEDOOR_CONFIG,
            mc.METHOD_SET,
            {mc.KEY_CONFIG: {self.key_value: onoff}},
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

    device_scale = 1000
    # HA core entity attributes:
    # these are ok for open/close durations
    # customize those when needed...
    native_max_value = 60
    native_min_value = 1
    native_step = 1

    def __init__(self, manager: GarageMixin, channel, key: str, *, device_value=None):
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

    def __init__(self, manager: GarageMixin, key: str, payload: dict):
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


class MLGarage(me.MerossEntity, cover.CoverEntity):
    PLATFORM = cover.DOMAIN

    manager: GarageMixin
    binary_sensor_timeout: MLGarageTimeoutBinarySensor
    number_signalClose: MLGarageMultipleConfigNumber | None
    number_signalOpen: MLGarageMultipleConfigNumber | None
    switch_buzzerEnable: MLGarageMultipleConfigSwitch | None
    switch_doorEnable: MLGarageDoorEnableSwitch | None

    # HA core entity attributes:
    is_closed: bool | None
    is_closing: bool
    is_opening: bool
    supported_features: CoverEntityFeature = (
        CoverEntityFeature.OPEN | CoverEntityFeature.CLOSE
    )

    __slots__ = (
        "is_closed",
        "is_closing",
        "is_opening",
        "_transition_duration",
        "_transition_start",
        "_transition_unsub",
        "_transition_end_unsub",
        "binary_sensor_timeout",
        "number_signalClose",
        "number_signalOpen",
        "switch_buzzerEnable",
        "switch_doorEnable",
    )

    def __init__(self, manager: GarageMixin, channel: object):
        super().__init__(manager, channel, None, CoverDeviceClass.GARAGE)
        self.is_closed = None
        self.is_closing = False
        self.is_opening = False
        self._transition_duration = (
            PARAM_GARAGEDOOR_TRANSITION_MAXDURATION
            + PARAM_GARAGEDOOR_TRANSITION_MINDURATION
        ) / 2
        self._transition_start = None
        self._transition_unsub = None
        self._transition_end_unsub = None
        self.extra_state_attributes = {
            EXTRA_ATTR_TRANSITION_DURATION: self._transition_duration
        }
        manager.register_parser(mc.NS_APPLIANCE_GARAGEDOOR_STATE, self)
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
            manager.register_parser(mc.NS_APPLIANCE_GARAGEDOOR_MULTIPLECONFIG, self)
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
                    self.extra_state_attributes[EXTRA_ATTR_TRANSITION_DURATION] = (
                        self._transition_duration
                    )

    async def async_will_remove_from_hass(self):
        self._transition_cancel()
        await super().async_will_remove_from_hass()

    def set_unavailable(self):
        self._transition_cancel()
        self.is_closed = None
        super().set_unavailable()

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
                    self._transition_start = time()
                    if open_request:
                        self.is_closing = False
                        self.is_opening = True
                        try:
                            timeout = self.number_signalOpen.native_value  # type: ignore
                        except AttributeError:
                            # this happens (once) when we don't have MULTIPLECONFIG ns support
                            # we'll then try use the 'x device' CONFIG or (since it could be missing)
                            # just build an emulated config entity
                            self.number_signalOpen = (
                                self.manager.number_doorOpenDuration
                                or MLGarageEmulatedConfigNumber(
                                    self, mc.KEY_DOOROPENDURATION
                                )
                            )
                            timeout = self.number_signalOpen.native_value
                    else:
                        self.is_closing = True
                        self.is_opening = False
                        try:
                            timeout = self.number_signalClose.native_value  # type: ignore
                        except AttributeError:
                            # this happens (once) when we don't have MULTIPLECONFIG ns support
                            # we'll then try use the 'x device' CONFIG or (since it could be missing)
                            # just build an emulated config entity
                            self.number_signalClose = (
                                self.manager.number_doorCloseDuration
                                or MLGarageEmulatedConfigNumber(
                                    self, mc.KEY_DOORCLOSEDURATION
                                )
                            )
                            timeout = self.number_signalClose.native_value

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
        self._transition_start = None
        if self._transition_unsub:
            self._transition_unsub.cancel()
            self._transition_unsub = None
        if self._transition_end_unsub:
            self._transition_end_unsub.cancel()
            self._transition_end_unsub = None

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
            if self._transition_duration < (time() - self._transition_start):  # type: ignore
                self._update_transition_duration(self._transition_duration + 1)

        self.is_closing = False
        self.is_opening = False
        self._transition_start = None

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
                        STATE_CLOSED if was_closing else STATE_OPEN
                    )
            else:
                self.flush_state()
                self.binary_sensor_timeout.update_timeout(
                    STATE_CLOSED if was_closing else STATE_OPEN
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


class GarageMixin(
    MerossDevice if typing.TYPE_CHECKING else object
):  # pylint: disable=used-before-assignment
    number_signalDuration: MLGarageConfigNumber = None  # type: ignore
    switch_buzzerEnable: MLGarageConfigSwitch = None  # type: ignore
    number_doorOpenDuration: MLGarageMultipleConfigNumber = None  # type: ignore
    number_doorCloseDuration: MLGarageMultipleConfigNumber = None  # type: ignore

    async def async_shutdown(self):
        await super().async_shutdown()
        self.number_signalDuration = None  # type: ignore
        self.switch_buzzerEnable = None  # type: ignore
        self.number_doorOpenDuration = None  # type: ignore
        self.number_doorCloseDuration = None  # type: ignore

    def _init_garageDoor(self, digest: list):
        channel_count = len(digest)
        self._polling_payload = []
        self.platforms.setdefault(MLConfigNumber.PLATFORM, None)
        self.platforms.setdefault(MLSwitch.PLATFORM, None)
        ability = self.descriptor.ability
        if mc.NS_APPLIANCE_GARAGEDOOR_CONFIG in ability:
            SmartPollingStrategy(self, mc.NS_APPLIANCE_GARAGEDOOR_CONFIG)
        if mc.NS_APPLIANCE_GARAGEDOOR_MULTIPLECONFIG in ability:
            SmartPollingStrategy(
                self,
                mc.NS_APPLIANCE_GARAGEDOOR_MULTIPLECONFIG,
                payload=self._polling_payload,
                item_count=channel_count,
            )
        for channel_digest in digest:
            channel = channel_digest[mc.KEY_CHANNEL]
            MLGarage(self, channel)
            self._polling_payload.append({mc.KEY_CHANNEL: channel})

    def _parse_garageDoor(self, digest: list):
        self.namespace_handlers[mc.NS_APPLIANCE_GARAGEDOOR_STATE]._parse_list(digest)

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
                    self,
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
                    self, mc.KEY_BUZZERENABLE, payload
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
                            garage, mc.KEY_DOORCLOSEDURATION
                        )
                    )
                    # set guard so we don't repeat this 'late conditional init'
                    self.number_doorCloseDuration = garage.number_signalClose


class MLRollerShutter(me.MerossEntity, cover.CoverEntity):
    """
    MRS100 SHUTTER ENTITY
    """

    PLATFORM = cover.DOMAIN

    manager: MerossDevice

    # HA core entity attributes:
    assumed_state = True
    current_cover_position: int | None
    is_closed: bool | None
    is_closing: bool | None
    is_opening: bool | None
    supported_features: CoverEntityFeature

    __slots__ = (
        "current_cover_position",
        "is_closed",
        "is_closing",
        "is_opening",
        "supported_features",
        "number_signalOpen",
        "number_signalClose",
        "_mrs_state",
        "_signalOpen",
        "_signalClose",
        "_position_native",
        "_position_native_isgood",
        "_position_start",
        "_position_starttime",
        "_transition_unsub",
        "_transition_end_unsub",
    )

    def __init__(self, manager: MerossDevice):
        self.current_cover_position = None
        self.is_closed = None
        self.is_closing = None
        self.is_opening = None
        self.supported_features = (
            CoverEntityFeature.OPEN | CoverEntityFeature.CLOSE | CoverEntityFeature.STOP
        )
        self.extra_state_attributes = {}
        self._mrs_state = None
        self._signalOpen: int = 30000  # msec to fully open (config'd on device)
        self._signalClose: int = 30000  # msec to fully close (config'd on device)
        self._position_native = None  # as reported by the device
        self._position_start = 0  # set when when we're controlling a timed position
        self._position_starttime = 0  # epoch of transition start
        self._transition_unsub = None
        self._transition_end_unsub = None
        descriptor = manager.descriptor
        # flag indicating the device position is reliable (#227)
        # this will anyway be set in case we 'decode' a meaningful device position
        try:
            if versiontuple(descriptor.firmwareVersion) >= versiontuple("6.6.6"):
                self._position_native_isgood = True
                self.supported_features |= CoverEntityFeature.SET_POSITION
            else:
                self._position_native_isgood = False
        except Exception:
            self._position_native_isgood = False
        super().__init__(manager, 0, None, CoverDeviceClass.SHUTTER)
        self.number_signalOpen = MLRollerShutterConfigNumber(self, mc.KEY_SIGNALOPEN)
        self.number_signalClose = MLRollerShutterConfigNumber(self, mc.KEY_SIGNALCLOSE)
        if mc.NS_APPLIANCE_ROLLERSHUTTER_ADJUST in descriptor.ability:
            manager.register_parser(mc.NS_APPLIANCE_ROLLERSHUTTER_ADJUST, self)
            SmartPollingStrategy(
                manager, mc.NS_APPLIANCE_ROLLERSHUTTER_ADJUST, item_count=1
            )
        manager.register_parser(mc.NS_APPLIANCE_ROLLERSHUTTER_CONFIG, self)
        SmartPollingStrategy(
            manager, mc.NS_APPLIANCE_ROLLERSHUTTER_CONFIG, item_count=1
        )
        manager.register_parser(mc.NS_APPLIANCE_ROLLERSHUTTER_POSITION, self)
        PollingStrategy(manager, mc.NS_APPLIANCE_ROLLERSHUTTER_POSITION, item_count=1)
        manager.register_parser(mc.NS_APPLIANCE_ROLLERSHUTTER_STATE, self)
        PollingStrategy(manager, mc.NS_APPLIANCE_ROLLERSHUTTER_STATE, item_count=1)

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
                    self.extra_state_attributes[EXTRA_ATTR_DURATION_OPEN] = (
                        self._signalOpen
                    )
                if EXTRA_ATTR_DURATION_CLOSE in _attr:
                    self._signalClose = _attr[EXTRA_ATTR_DURATION_CLOSE]
                    self.extra_state_attributes[EXTRA_ATTR_DURATION_CLOSE] = (
                        self._signalClose
                    )
                if not self._position_native_isgood:
                    # at this stage, the euristic on fw version doesn't say anything
                    if EXTRA_ATTR_POSITION_NATIVE in _attr:
                        # this means we haven't detected (so far) a reliable 'native_position'
                        # so we restore the cover position (which was emulated)
                        self.extra_state_attributes[EXTRA_ATTR_POSITION_NATIVE] = _attr[
                            EXTRA_ATTR_POSITION_NATIVE
                        ]
                        if ATTR_CURRENT_POSITION in _attr:
                            self.current_cover_position = _attr[ATTR_CURRENT_POSITION]
                            self.supported_features |= CoverEntityFeature.SET_POSITION

    async def async_will_remove_from_hass(self):
        self._transition_cancel()
        await super().async_will_remove_from_hass()

    async def async_open_cover(self, **kwargs):
        await self.async_request_position(mc.ROLLERSHUTTER_POSITION_OPENED)

    async def async_close_cover(self, **kwargs):
        await self.async_request_position(mc.ROLLERSHUTTER_POSITION_CLOSED)

    async def async_set_cover_position(self, **kwargs):
        position = kwargs[ATTR_POSITION]
        if (
            self._position_native_isgood
            or (position == mc.ROLLERSHUTTER_POSITION_OPENED)
            or (position == mc.ROLLERSHUTTER_POSITION_CLOSED)
        ):
            # ensure a full 'untimed' run when asked for
            # fully opened/closed (#170)
            await self.async_request_position(position)
        else:
            # this is the estimate: could be None on very first run
            # or when the entity state is not properly restored anyway
            current_position = self.current_cover_position
            if current_position is None:
                raise InvalidStateError(
                    "Cannot estimate command direction. Please use open_cover or close_cover"
                )
            if position > current_position:
                timeout = ((position - current_position) * self._signalOpen) / 100000
                position = mc.ROLLERSHUTTER_POSITION_OPENED
            elif position < current_position:
                timeout = ((current_position - position) * self._signalClose) / 100000
                position = mc.ROLLERSHUTTER_POSITION_CLOSED
            else:
                return  # No-Op
            if await self.async_request_position(position):
                self._transition_end_unsub = schedule_async_callback(
                    self.hass, timeout, self._async_transition_end_callback
                )

    async def async_stop_cover(self, **kwargs):
        await self.async_request_position(mc.ROLLERSHUTTER_POSITION_STOP)

    async def async_request_position(self, position: int):
        self._transition_cancel()
        manager = self.manager
        channel = self.channel
        """ REMOVE: looks like the mrs100 doesn't love set_position in multiple req
        if (manager.multiple_max >= 3) and (
            responses := await manager.async_multiple_requests_ack(
                (
                    (
                        mc.NS_APPLIANCE_ROLLERSHUTTER_POSITION,
                        mc.METHOD_SET,
                        {
                            mc.KEY_POSITION: [
                                {
                                    mc.KEY_CHANNEL: channel,
                                    mc.KEY_POSITION: position,
                                }
                            ]
                        },
                    ),
                    (
                        mc.NS_APPLIANCE_ROLLERSHUTTER_STATE,
                        mc.METHOD_GET,
                        {mc.KEY_STATE: [{mc.KEY_CHANNEL: channel}]},
                    ),
                    (
                        mc.NS_APPLIANCE_ROLLERSHUTTER_POSITION,
                        mc.METHOD_GET,
                        {mc.KEY_POSITION: [{mc.KEY_CHANNEL: channel}]},
                    ),
                )
            )
        ):
            # we expect a full success (3 responses) 99% of the times
            # since the only reason for failing is the device not supporting
            # ns_multiple (unlikely) or the response being truncated due to
            # overflow (unlikely too)
            # At this stage the responses are already processed by the MerossDevice
            # interface and we should already be 'in transition'
            if responses[0][mc.KEY_HEADER][mc.KEY_METHOD] == mc.METHOD_SETACK:
                if (
                    (len(responses) == 3)
                    and (responses[1][mc.KEY_HEADER][mc.KEY_METHOD] == mc.METHOD_GETACK)
                    and (responses[2][mc.KEY_HEADER][mc.KEY_METHOD] == mc.METHOD_GETACK)
                ):
                    # our state machine is already updated since the STATE and POSITION
                    # messages were correctly processed
                    return True

                if (
                    not self._transition_unsub
                    and position != mc.ROLLERSHUTTER_POSITION_STOP
                ):
                    # this could happen if the shutter was already 'at position'
                    # so that it didn't start an internal transition (guessing)
                    # or if the 2nd message in our requests failed somehow
                    # at any rate, we'll monitor the state
                    await self._async_transition_callback()
                return True
        """

        # in case the ns_multiple didn't succesfully kick-in we'll
        # fallback to the legacy procedure
        if await manager.async_request_ack(
            mc.NS_APPLIANCE_ROLLERSHUTTER_POSITION,
            mc.METHOD_SET,
            {
                mc.KEY_POSITION: {
                    mc.KEY_CHANNEL: channel,
                    mc.KEY_POSITION: position,
                }
            },
        ):
            # re-ensure current transitions are clean after await
            self._transition_cancel()
            await self._async_transition_callback()
            return True

    def set_unavailable(self):
        self.is_closed = None
        self.is_closing = None
        self.is_opening = None
        self._mrs_state = None
        self._transition_cancel()
        super().set_unavailable()

    def _parse_adjust(self, payload: dict):
        # payload = {"channel": 0, "status": 0}
        for key, value in payload.items():
            if key != mc.KEY_CHANNEL:
                self.extra_state_attributes[f"adjust_{key}"] = value

    def _parse_config(self, payload: dict):
        # payload = {"channel": 0, "signalOpen": 50000, "signalClose": 50000}
        if mc.KEY_SIGNALOPEN in payload:
            self._signalOpen = payload[mc.KEY_SIGNALOPEN]
            self.number_signalOpen.update_device_value(self._signalOpen)
            self.extra_state_attributes[EXTRA_ATTR_DURATION_OPEN] = self._signalOpen
        if mc.KEY_SIGNALCLOSE in payload:
            self._signalClose = payload[mc.KEY_SIGNALCLOSE]
            self.number_signalClose.update_device_value(self._signalClose)
            self.extra_state_attributes[EXTRA_ATTR_DURATION_CLOSE] = self._signalClose

    def _parse_position(self, payload: dict):
        """
        legacy devices only reported 0 or 100 as position
        so we used to store this as an extra attribute and perform
        a trajectory calculation to emulate time based positioning
        now (#227) we'll detect devices reporting 'actual' good
        positioning and switch entity behaviour to trust this value
        bypassing all of the 'time based' emulation
        """
        position = payload[mc.KEY_POSITION]

        if self._position_native_isgood:
            if position != self.current_cover_position:
                self.current_cover_position = position
                self.is_closed = position == mc.ROLLERSHUTTER_POSITION_CLOSED
                self.flush_state()
            return

        if position == self._position_native:
            # no news...
            return

        if (position > 0) and (position < 100):
            # detecting a device reporting 'good' positions
            self._position_native_isgood = True
            self._position_native = None
            self.is_closed = False
            self.extra_state_attributes.pop(EXTRA_ATTR_POSITION_NATIVE, None)
            self.supported_features |= CoverEntityFeature.SET_POSITION
            self.current_cover_position = position
        else:
            self._position_native = position
            self.is_closed = position == mc.ROLLERSHUTTER_POSITION_CLOSED
            self.extra_state_attributes[EXTRA_ATTR_POSITION_NATIVE] = position
            if self.current_cover_position is None:
                # only happening when we didn't restore state on devices
                # which are likely not supporting native positioning
                # at this stage we'll enable set_position anyway and
                # trusting the device position as the better guess
                # If current_cover_position is already set, it represents the
                # emulated state and so we don't touch it
                self.supported_features |= CoverEntityFeature.SET_POSITION
                self.current_cover_position = position

        self.flush_state()

    def _parse_state(self, payload: dict):
        state = payload[mc.KEY_STATE]
        if not self._position_native_isgood:
            epoch = self.manager.lastresponse
            if self.is_opening:
                self.current_cover_position = round(
                    self._position_start
                    + ((epoch - self._position_starttime) * 100000) / self._signalOpen
                )
                if self.current_cover_position > mc.ROLLERSHUTTER_POSITION_OPENED:
                    self.current_cover_position = mc.ROLLERSHUTTER_POSITION_OPENED
                self._mrs_state = None  # ensure flushing state
            elif self.is_closing:
                self.current_cover_position = round(
                    self._position_start
                    - ((epoch - self._position_starttime) * 100000) / self._signalClose
                )
                if self.current_cover_position < mc.ROLLERSHUTTER_POSITION_CLOSED:
                    self.current_cover_position = mc.ROLLERSHUTTER_POSITION_CLOSED
                self._mrs_state = None  # ensure flushing state

            if state == mc.ROLLERSHUTTER_STATE_OPENING:
                if not self.is_opening:
                    if self.current_cover_position is None:
                        # this should never really happen since we've
                        # already set current_cover_position in _parse_position
                        self.current_cover_position = mc.ROLLERSHUTTER_POSITION_CLOSED
                        self.supported_features |= CoverEntityFeature.SET_POSITION
                    self._position_start = self.current_cover_position
                    self._position_starttime = epoch
            elif state == mc.ROLLERSHUTTER_STATE_CLOSING:
                if not self.is_closing:
                    if self.current_cover_position is None:
                        self.current_cover_position = mc.ROLLERSHUTTER_POSITION_OPENED
                        self.supported_features |= CoverEntityFeature.SET_POSITION
                    self._position_start = self.current_cover_position
                    self._position_starttime = epoch

        if self._mrs_state != state:
            self._mrs_state = state
            self.is_closed = (
                self.current_cover_position == mc.ROLLERSHUTTER_POSITION_CLOSED
            )
            if state == mc.ROLLERSHUTTER_STATE_IDLE:
                self.is_closing = False
                self.is_opening = False
            else:
                self.is_closing = state == mc.ROLLERSHUTTER_STATE_CLOSING
                self.is_opening = not self.is_closing
                if not self._transition_unsub:
                    # ensure we 'follow' cover movement
                    self._transition_unsub = schedule_async_callback(
                        self.hass,
                        PARAM_ROLLERSHUTTER_TRANSITION_POLL_TIMEOUT,
                        self._async_transition_callback,
                    )
            self.flush_state()

        if self._transition_unsub and (state == mc.ROLLERSHUTTER_STATE_IDLE):
            self._transition_cancel()

    async def _async_transition_callback(self):
        """Schedule a repetitive callback when we detect or suspect shutter movement.
        It will be invalidated only when a successful state message is parsed stating
        there's no movement.
        This is a very 'gentle' polling happening only on HTTP when we're sure we're
        not receiving MQTT updates. If device was configured for MQTT only we could
        not setup this at all."""
        self._transition_unsub = schedule_async_callback(
            self.hass,
            PARAM_ROLLERSHUTTER_TRANSITION_POLL_TIMEOUT,
            self._async_transition_callback,
        )
        manager = self.manager
        if (
            manager.curr_protocol is CONF_PROTOCOL_HTTP and not manager._mqtt_active
        ) or (self._mrs_state == mc.ROLLERSHUTTER_STATE_IDLE):
            p_channel_payload = [{mc.KEY_CHANNEL: self.channel}]
            if manager.multiple_max >= 2:
                await manager.async_multiple_requests_ack(
                    (
                        (
                            mc.NS_APPLIANCE_ROLLERSHUTTER_STATE,
                            mc.METHOD_GET,
                            {mc.KEY_STATE: p_channel_payload},
                        ),
                        (
                            mc.NS_APPLIANCE_ROLLERSHUTTER_POSITION,
                            mc.METHOD_GET,
                            {mc.KEY_POSITION: p_channel_payload},
                        ),
                    )
                )
            else:
                await manager.async_request(
                    mc.NS_APPLIANCE_ROLLERSHUTTER_STATE,
                    mc.METHOD_GET,
                    {mc.KEY_STATE: p_channel_payload},
                )
                if self._position_native_isgood:
                    await manager.async_request(
                        mc.NS_APPLIANCE_ROLLERSHUTTER_POSITION,
                        mc.METHOD_GET,
                        {mc.KEY_POSITION: p_channel_payload},
                    )

    async def _async_transition_end_callback(self):
        self._transition_end_unsub = None
        self.log(self.DEBUG, "_async_transition_end_callback")
        await self.async_stop_cover()

    def _transition_cancel(self):
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

    device_scale = 1000

    # HA core entity attributes:
    # these are ok for open/close durations
    # customize those when needed...
    native_max_value = 60
    native_min_value = 1
    native_step = 1

    __slots__ = ("_cover",)

    def __init__(self, cover: MLRollerShutter, key: str):
        self._cover = cover
        self.key_value = key
        self.name = key
        super().__init__(
            cover.manager, cover.channel, f"config_{key}", self.DEVICE_CLASS_DURATION
        )

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
