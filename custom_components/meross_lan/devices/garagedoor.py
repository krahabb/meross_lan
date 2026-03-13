from typing import TYPE_CHECKING, override

from homeassistant.helpers import entity_registry as er
from homeassistant.util.dt import now

from ..binary_sensor import BinarySensor
from ..cover import Cover
from ..helpers import clamp
from ..helpers.namespaces import NamespaceHandler, mc, mn
from ..merossclient.client import Transport
from ..number import EmulatedNumber, NumberParser
from ..switch import SwitchParser

if TYPE_CHECKING:
    from typing import ClassVar, Final, NotRequired, TypedDict, Unpack

    from ..helpers.device import Device, MerossMessage
    from ..helpers.entity import ValueParser
    from ..merossclient.protocol.types import JsonList
    from ..number import NumberEntity


class GarageTimeoutBinarySensor(BinarySensor):

    ENTITY_KEY = "problem"

    # the time at which the transition timeout occurred
    ATTR_TRANSITION_TIMEOUT = "transition_timeout"
    # the target state which was not reached
    ATTR_TRANSITION_TARGET = "transition_target"

    # HA core entity attributes:
    _attr_device_class = BinarySensor.DeviceClass.PROBLEM
    _attr_entity_category = BinarySensor.EntityCategory.DIAGNOSTIC
    _unrecorded_attributes = frozenset(
        {
            ATTR_TRANSITION_TARGET,
            ATTR_TRANSITION_TIMEOUT,
            *BinarySensor._unrecorded_attributes,
        }
    )

    def __init__(self, garage: "Garagedoor", /):
        self.extra_state_attributes = {}
        super().__init__(garage.channel, garage.parent, is_on=False)

    def update_ok(self, was_closing, /):
        extra_state_attributes = self.extra_state_attributes
        if extra_state_attributes.get(self.ATTR_TRANSITION_TARGET) == (
            Cover.CoverState.CLOSED if was_closing else Cover.CoverState.OPEN
        ):
            extra_state_attributes.pop(self.ATTR_TRANSITION_TIMEOUT, None)
            extra_state_attributes.pop(self.ATTR_TRANSITION_TARGET, None)
        self.update_boolean_value(False)

    def update_timeout(self, was_closing, /):
        self.extra_state_attributes[self.ATTR_TRANSITION_TARGET] = (
            Cover.CoverState.CLOSED if was_closing else Cover.CoverState.OPEN
        )
        self.extra_state_attributes[self.ATTR_TRANSITION_TIMEOUT] = now().isoformat()
        self.is_on = True
        self.flush_state()


class GarageConfigMixin(ValueParser if TYPE_CHECKING else object):
    if TYPE_CHECKING:

        class Args(TypedDict):
            device_value: NotRequired[int]

    # Assuming by default we're parsing MultipleConfig
    # This will be overriden when creating entities for Appliance.GarageDoor.Config
    ns = mn.Appliance_GarageDoor_MultipleConfig

    def __init__(
        self,
        channel: int | None,
        device: "Device",
        key: str,
        /,
        **kwargs: "Unpack[Args]",
    ):
        super().__init__(
            channel,
            device,
            entity_key=f"config_{key}",
            key_value=key,
            name=key,
            **kwargs,
        )


class GarageConfigSwitch(GarageConfigMixin, SwitchParser):
    """
    switch entity to manage MSG configuration (buzzer, enable)
    'x channel' through mc.NS_APPLIANCE_GARAGEDOOR_MULTIPLECONFIG
    """

    pass


class GarageEnableSwitch(GarageConfigSwitch):
    """
    Dedicated entity for "doorEnable" config option in mc.NS_APPLIANCE_GARAGEDOOR_MULTIPLECONFIG
    in order to try enable/disable the same channel associated entities in HA too
    when done with the Meross app (#330)
    """

    def __init__(
        self,
        channel: int | None,
        parent: "Device",
        key: str,
        /,
        **kwargs: "Unpack[GarageEnableSwitch.Args]",
    ):
        GarageConfigSwitch.__init__(self, channel, parent, key, **kwargs)
        self._channel_enable(self.is_on)

    @override
    def update_boolean_value(self, is_on, /):
        if self.is_on != is_on:
            self.is_on = is_on
            self.flush_state()
            self._channel_enable(is_on)
            return True

    def _channel_enable(self, enabled, /):
        """enables/disables all the entities of this channel garageDoor in the
        entity registry"""
        registry_update_entity = self.parent.api.entity_registry.async_update_entity
        disabler = er.RegistryEntryDisabler.INTEGRATION
        for entity in self.parent.entities.values():
            if (
                (entity.channel == self.channel)
                and (entity is not self)
                and (entry := entity.registry_entry)
            ):
                if enabled:
                    if entry.disabled_by == disabler:
                        registry_update_entity(entry.entity_id, disabled_by=None)
                else:
                    if not entry.disabled_by:
                        registry_update_entity(entry.entity_id, disabled_by=disabler)


class GarageConfigNumber(GarageConfigMixin, NumberParser):
    """
    number entity to manage MSG configuration (open/close timeout and the likes)
    'x channel' through mc.NS_APPLIANCE_GARAGEDOOR_MULTIPLECONFIG
    """

    if TYPE_CHECKING:

        class Args(NumberParser.Args):
            pass

        def __init__(
            self,
            channel: int | None,
            parent: Device,
            /,
            **kwargs: Unpack[Args],
        ): ...

    # these are ok for almost all config entities (they're mostly durations with
    # milliseconds device_value)
    # customize those when needed...
    _attr_device_scale = 1000
    # HA core entity attributes:
    _attr_device_class = NumberParser.DEVICE_CLASS_DURATION
    _attr_native_max_value = 60
    _attr_native_min_value = 1
    _attr_native_step = 1


class _DurationHelper:
    """
    GarageDoor helper class to manage the automatic instantiation of number entities for
    doorOpenDuration/doorCloseDuration should they be missing in MultipleConfig (legacy fw) and eventually
    update the related attribute(s) in GarageDoor.
    The logic here is to check if Garagedoor.Config carries any candidate or fallback to an emulated number.
    """

    if TYPE_CHECKING:
        garage_door: Final["Garagedoor"]
        key: Final[str]

    __slots__ = ("garage_door", "key")

    def __init__(self, garage_door: "Garagedoor", key: str):
        self.garage_door = garage_door
        self.key = key

    @property
    def native_value(self):
        # Invoked only once (per key) since it'll also install a definitive
        # number entity
        gd = self.garage_door
        try:
            number: "NumberEntity" = gd.parent.entities[f"config_{self.key}"]  # type: ignore
        except KeyError:
            number = EmulatedNumber(
                gd.channel,
                gd.parent,
                entity_key=f"config_{self.key}",
                native_value=gd._transition_duration,
                name=self.key,
                device_class=EmulatedNumber.DEVICE_CLASS_DURATION,
                native_max_value=60,
                native_min_value=1,
                native_step=1,
            )
        setattr(gd, f"number_{self.key}", number)
        return number.native_value


class Garagedoor(Cover):

    if TYPE_CHECKING:

        ENTITY_DEFS: dict[str, type[GarageConfigMixin]]

        binary_sensor_timeout: GarageTimeoutBinarySensor
        number_doorCloseDuration: NumberEntity | _DurationHelper
        number_doorOpenDuration: NumberEntity | _DurationHelper

    ns = mn.Appliance_GarageDoor_State
    key_value = mc.KEY_OPEN

    PARAM_TRANSITION_MAXDURATION = 60
    PARAM_TRANSITION_MINDURATION = 10
    ATTR_TRANSITION_DURATION = "transition_duration"

    # these keys in Appliance.GarageDoor.MultipleConfig are to be ignored
    CONFIG_KEY_EXCLUDED = (mc.KEY_CHANNEL, mc.KEY_TIMESTAMP, mc.KEY_TIMESTAMPMS)
    ENTITY_DEFS = {
        mc.KEY_BUZZERENABLE: GarageConfigSwitch,
        mc.KEY_DOORENABLE: GarageEnableSwitch,
        mc.KEY_SIGNALDURATION: GarageConfigNumber,
        mc.KEY_SIGNALCLOSE: GarageConfigNumber,
        mc.KEY_SIGNALOPEN: GarageConfigNumber,
        mc.KEY_DOORCLOSEDURATION: GarageConfigNumber,
        mc.KEY_DOOROPENDURATION: GarageConfigNumber,
    }

    # HA core entity attributes:
    _attr_device_class = Cover.DeviceClass.GARAGE
    _attr_supported_features = Cover.EntityFeature.OPEN | Cover.EntityFeature.CLOSE

    __slots__ = (
        "_config",
        "_transition_duration",
        "_transition_start",
        "binary_sensor_timeout",
        "number_doorCloseDuration",
        "number_doorOpenDuration",
    )

    def __init__(self, channel: int, device: "Device", /):
        self._config = {}
        self._transition_duration = (
            self.PARAM_TRANSITION_MAXDURATION + self.PARAM_TRANSITION_MINDURATION
        ) / 2
        self._transition_start = 0.0
        self.extra_state_attributes = {
            self.ATTR_TRANSITION_DURATION: self._transition_duration
        }
        Cover.__init__(self, channel, device)
        self.binary_sensor_timeout = GarageTimeoutBinarySensor(self)
        if mn.Appliance_GarageDoor_MultipleConfig in device.descriptor.ability:
            # historically, when MultipleConfig appeared, these used to be
            # the available timeouts while recent fw (4.2.8) shows presence
            # of more 'natural' doorOpenDuration/doorCloseDuration keys.
            # We'll then override this initial guessing when we _parse_config
            # should those new keys appear
            self.number_doorCloseDuration = self.ENTITY_DEFS[mc.KEY_SIGNALCLOSE](
                channel, device, mc.KEY_SIGNALCLOSE
            )  # type: ignore
            self.number_doorOpenDuration = self.ENTITY_DEFS[mc.KEY_SIGNALOPEN](
                channel, device, mc.KEY_SIGNALOPEN
            )  # type: ignore
            device.get_handler(mn.Appliance_GarageDoor_MultipleConfig).register_parser(
                self
            )
        else:
            self.number_doorCloseDuration = _DurationHelper(
                self, mc.KEY_DOORCLOSEDURATION
            )
            self.number_doorOpenDuration = _DurationHelper(
                self, mc.KEY_DOOROPENDURATION
            )

    def shutdown(self):
        super().shutdown()
        del self.number_doorCloseDuration
        del self.number_doorOpenDuration

    async def async_added_to_hass(self):
        await Cover.async_added_to_hass(self)
        with self.exception_warning("restoring previous state"):
            if last_state := await self.get_last_state_available():
                try:
                    # to restore previously estimated transition duration
                    self._transition_duration = last_state.attributes[
                        self.ATTR_TRANSITION_DURATION
                    ]
                    self.extra_state_attributes[self.ATTR_TRANSITION_DURATION] = (
                        self._transition_duration
                    )
                except KeyError:
                    pass

    def set_unavailable(self):
        self._config = {}
        Cover.set_unavailable(self)

    # interface: cover.CoverEntity
    async def async_open_cover(self, **kwargs):
        await self.async_request_position(1)

    async def async_close_cover(self, **kwargs):
        await self.async_request_position(0)

    # interface: self
    async def async_request_position(self, open_request: int, /):
        self._transition_cancel()
        response = await self.async_request_payload({self.key_value: open_request})
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

        p_state = response.payload[mc.KEY_STATE]
        if type(p_state) is list:
            # we eventually expect a 1 item list with our channel of course
            p_state = p_state[0]
        _open = p_state[mc.KEY_OPEN]
        self.is_closed = not _open
        if p_state.get(mc.KEY_EXECUTE) and open_request != _open:
            device = self.parent
            self._transition_start = device.last_rx_epoch
            if open_request:
                self.is_closing = False
                self.is_opening = True
                timeout = self.number_doorOpenDuration.native_value
            else:
                self.is_closing = True
                self.is_opening = False
                timeout = self.number_doorCloseDuration.native_value
            self.schedule_callback(0.9, self._transition_callback)
            # check the timeout after expected to account
            # for delays in communication
            self.schedule_async_callback(
                (timeout or self._transition_duration),  # type: ignore
                self._async_transition_end_callback,
            )

        self.flush_state()

    def _parse_state(self, payload: dict, /):
        """
        {
            "channel": 0,
            "doorEnable": 1, # appeared on msg200 fw:4.2.8
            "open": 1,
            "lmTime": 0
        }
        """
        if (mc.KEY_DOORENABLE in payload) and (
            self._config.get(mc.KEY_DOORENABLE) != payload[mc.KEY_DOORENABLE]
        ):
            self._parse_multipleConfig({mc.KEY_DOORENABLE: payload[mc.KEY_DOORENABLE]})

        is_closed = not payload[mc.KEY_OPEN]
        if self.is_closed == is_closed:
            if self._transition_start:
                # keep monitoring the transition
                self.schedule_callback(0.9, self._transition_callback)
            return

        # door open state changed
        if self._transition_start:
            # We're "in transition" and the physical contact has reached the target.
            # we can monitor the (sampled) exact time when the garage closes to
            # estimate the transition_duration and dynamically update it since
            # during the transition the state will be closed only at the end
            # while during opening the garagedoor contact will open right at the beginning
            # and so will be unuseful. This is why we're not 'terminating' the transition in
            # case the garage was opening...(the '_async_transition_end_callback' will then take care).
            # Also to note: if we're on HTTP this sampled time could happen anyway after the 'real'
            # state switched to 'closed' so we're likely going to measure in exceed of real transition duration
            if is_closed:
                transition_duration = self.parent.last_rx_epoch - self._transition_start
                # autoregression filtering applying 20% of last updated sample
                self._update_transition_duration(
                    int((4 * self._transition_duration + transition_duration) / 5)
                )
                self._transition_cancel()
            self.binary_sensor_timeout.update_ok(is_closed)

        self.is_closed = is_closed
        self.flush_state()

    def _parse_multipleConfig(self, payload: dict, /):
        """
        {
          "channel": 1,
          "doorEnable": 1,
          "timestamp": 0,
          "timestampMs": 0,
          "doorCloseDuration": 15000, # appeared on msg200 fw:4.2.8
          "doorOpenDuration": 15000, # appeared on msg200 fw:4.2.8
          "signalClose": 3000,
          "signalOpen": 3000,
          "buzzerEnable": 0
        },
        """
        entities = self.parent.entities
        entity_id_prefix = f"{self.channel}_config_"
        for key, value in payload.items():
            if key in Garagedoor.CONFIG_KEY_EXCLUDED or (
                self._config.get(key) == value
            ):
                continue
            self._config[key] = value  # useless ?
            try:
                try:
                    entities[f"{entity_id_prefix}{key}"].update_device_value(value)
                except KeyError:
                    if key in (mc.KEY_DOORCLOSEDURATION, mc.KEY_DOOROPENDURATION):
                        setattr(
                            self,
                            f"number_{key}",
                            self.ENTITY_DEFS[key](
                                self.channel, self.parent, key, device_value=value
                            ),
                        )
                    else:
                        self.ENTITY_DEFS[key](
                            self.channel, self.parent, key, device_value=value
                        )
            except Exception as exception:
                self.log_exception(
                    self.WARNING,
                    exception,
                    "_parse_config (payload=%s)",
                    _payload=payload,
                )

    def _transition_cancel(self, /):
        self.is_closing = False
        self.is_opening = False
        self._transition_start = 0.0
        Cover._transition_cancel(self)

    @override
    def _transition_callback(self, /):
        if self.parent.transport is Transport.HTTP and not self.parent.mqtt_active:
            self.handler_ns.schedule_get(self.channel)

    @override
    async def _async_transition_end_callback(self, /):
        """
        checks the transition did finish as per the timeout(s)
        """
        was_closing = self.is_closing
        if was_closing:
            # when closing we expect this callback not to be called since
            # the transition should be terminated by '_parse_state' provided it gets
            # called on time (on polling this is not guaranteed).
            # If we're here, we still havent received a proper 'physical close'
            # because our configured closeduration is too short
            # or the garage didnt close at all
            if self._transition_duration < (self.time() - self._transition_start):
                self._update_transition_duration(self._transition_duration + 1)

        self.is_closing = False
        self.is_opening = False
        self._transition_start = 0.0

        if was_closing != self.is_closed:
            # looks like on MQTT we don't receive a PUSHed state update? (#415)
            try:
                await self.handler_ns.async_get(self.channel)
                # the request/response parse already flushed the state
                if was_closing == self.is_closed:
                    self.binary_sensor_timeout.update_ok(was_closing)
                else:
                    self.binary_sensor_timeout.update_timeout(was_closing)
            except Exception:
                self.flush_state()
                self.binary_sensor_timeout.update_timeout(was_closing)
        else:
            self.flush_state()
            self.binary_sensor_timeout.update_ok(was_closing)

    def _update_transition_duration(self, transition_duration, /):
        self._transition_duration = clamp(
            transition_duration,
            self.PARAM_TRANSITION_MINDURATION,
            self.PARAM_TRANSITION_MAXDURATION,
        )
        self.extra_state_attributes[self.ATTR_TRANSITION_DURATION] = (
            self._transition_duration
        )

    @classmethod
    @override
    def digest_init(
        cls, device: "Device", digest: "JsonList", /
    ) -> "Device.DigestInitReturnType":
        device.platforms.setdefault(NumberParser.PLATFORM, None)
        device.platforms.setdefault(SwitchParser.PLATFORM, None)

        handler = NamespaceHandler(mn.Appliance_GarageDoor_State, device)
        descriptor = device.descriptor
        if descriptor.type.startswith(mc.TYPE_MSG200) and (
            descriptor.firmware_version <= (4, 2, 1)
        ):
            # trying to patch lacking of state polling (#538)
            # It's not sure querying with the list of channels works.
            # Also, in fw 4.0.0 the default polling with empty dict correctly returns
            # the list of channels so this should not be needed.
            # Here the issue arises when we optimize NS_ALL polling by issuing single digest
            # namespaces requests: it looks like we're unable to get in a single query the full
            # state of all channels, at least on these old firmwares.
            # So we disable NS_ALL 'optimization' and we go straigth to querying for that every time.
            # As we know it now, this namespace accepts this queries:
            # - single channel in a DICT_C_STRICT
            # - all channels in an empty dict (only confirmed in 4.0.0+ fw)
            device.ns_handlers[mn.Appliance_System_All].polling_period = 0

        # do not register_parser_class since we don't want to create spurious
        # GarageDoor at channel 0 (msg200)
        for channel_digest in digest:
            handler.register_parser(Garagedoor(channel_digest[mc.KEY_CHANNEL], device))

        if mn.Appliance_GarageDoor_Config in descriptor.ability:
            GarageDoorConfigNamespaceHandler(mn.Appliance_GarageDoor_Config, device)

        return handler.parse_list, (handler,)


# TODO: generalize similar namespaces where no channel indexing is in place (much like EntityNamespaceMixini)
# but we have multiple (likely dynamic) parsers to register (see Appliance.Control.Sensor.Latest/latestX)
class GarageDoorConfigNamespaceHandler(NamespaceHandler):

    if TYPE_CHECKING:
        ENTITY_DEFS: dict[str, type[GarageConfigMixin]]

        _check_missing_config_keys: ClassVar[bool] | bool
        """Guard used to eventually initialize emulated entities for garage door open/close durations
        should they be missed in MultipleConfig."""

    POLLING_CONFIG_DEFAULT = NamespaceHandler.POLLING_CONFIG_CONFIGURATION_NS

    ENTITY_DEFS = {
        mc.KEY_BUZZERENABLE: GarageConfigSwitch,
        mc.KEY_SIGNALDURATION: GarageConfigNumber.ENTITY_DEF(
            native_step=0.1,
            native_min_value=0.1,
        ),
        mc.KEY_DOORCLOSEDURATION: GarageConfigNumber,
        mc.KEY_DOOROPENDURATION: GarageConfigNumber,
    }

    _check_missing_config_keys = True

    def _handle(self, message: "MerossMessage", /):
        # {"config": {"signalDuration": 1000, "buzzerEnable": 0, "doorOpenDuration": 30000, "doorCloseDuration": 30000}}
        entities = self.parent.entities
        for key, value in message.payload[mc.KEY_CONFIG].items():

            try:
                entities[f"config_{key}"].update_device_value(value)
            except KeyError:
                self.ENTITY_DEFS[key](None, self.parent, key, device_value=value).ns = (
                    self.id
                )

        if self._check_missing_config_keys:
            # mc.KEY_DOOROPENDURATION and mc.KEY_DOORCLOSEDURATION config keys have been
            # removed in recent firmwares (migrated to MultipleConfig x channel #82).
            # We keep implementing emulated entities in case for legacy firmwares.
            self._check_missing_config_keys = False
            # we'll let every channel manage it's own doorOpenDuration config parameter
            for channel_digest in self.parent.descriptor.digest[mc.KEY_GARAGEDOOR]:
                garage = entities[channel_digest[mc.KEY_CHANNEL]]
                # in case MULTIPLECONFIG is supported this code does nothing
                # since everything is already in place.
                # This is just to trigger the _DurationHelper in case it's installed
                for key in (mc.KEY_DOOROPENDURATION, mc.KEY_DOORCLOSEDURATION):
                    getattr(garage, f"number_{key}").native_value


NamespaceHandler.POLLING_CONFIG_MAP.update(
    {
        mn.Appliance_GarageDoor_MultipleConfig: NamespaceHandler.POLLING_CONFIG_CONFIGURATION_NS,
    }
)
