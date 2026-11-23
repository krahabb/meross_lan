from typing import TYPE_CHECKING, override

from homeassistant.helpers.entity_registry import RegistryEntryDisabler
from homeassistant.util.dt import now

from ..binary_sensor import BinarySensorEntity, BinarySensorParser
from ..cover import Cover
from ..helpers import clamp
from ..merossclient.client import Transport
from ..merossclient.device.handler import (
    MappingParser,
    MappingParserHandler,
    NamespaceHandler,
)
from ..merossclient.protocol import const as mc, namespaces as mn
from ..number import EmulatedNumber, NumberParser
from ..switch import SwitchParser

if TYPE_CHECKING:
    from typing import Any, Final, NotRequired, Unpack

    from ..helpers.device import Device, MerossMessage
    from ..helpers.entity import ValueParser
    from ..merossclient.protocol import types as mt
    from ..number import NumberEntity


class GarageTimeoutBinarySensor(BinarySensorEntity):

    # the time at which the transition timeout occurred
    ATTR_TRANSITION_TIMEOUT = "transition_timeout"
    # the target state which was not reached
    ATTR_TRANSITION_TARGET = "transition_target"

    _INIT_entity_key = "problem"
    _INIT_is_on = False
    _INIT_extra_state_attributes = dict

    # HA core entity attributes:
    _attr_device_class = BinarySensorEntity.DeviceClass.PROBLEM
    _attr_entity_category = BinarySensorEntity.EntityCategory.DIAGNOSTIC
    _unrecorded_attributes = frozenset(
        (ATTR_TRANSITION_TARGET, ATTR_TRANSITION_TIMEOUT)
    )

    _AUTO_INIT = ("extra_state_attributes",)

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


class GarageObstructionBinarySensor(BinarySensorParser):
    """
    Binary sensor for MSG150 obstruction status (1: detected, 2: cleared).
    """

    _INIT_entity_key = mc.KEY_OBSTRUCTION
    _INIT_key_value = BinarySensorParser.KeyValue(mc.KEY_OBSTRUCTION)
    _INIT_value_off = 2

    # HA core entity attributes:
    _attr_device_class = BinarySensorParser.DeviceClass.PROBLEM
    _attr_name = "Obstruction"


class GarageConfigMixin(ValueParser if TYPE_CHECKING else object):
    if TYPE_CHECKING:

        type InitArgs = ValueParser.InitArgs
        type Args = ValueParser.Args

    # Assuming by default we're parsing MultipleConfig
    # This will be overriden in kwargs when creating entities for Appliance.GarageDoor.Config
    _INIT_ns = mn.Appliance_GarageDoor_MultipleConfig

    def __init__(self, *args: "*InitArgs", **kwargs: "Unpack[Args]"):
        key_value_str = str(kwargs["key_value"])  # type: ignore
        kwargs["entity_key"] = f"config_{key_value_str}"
        kwargs.setdefault("name", key_value_str)
        super().__init__(*args, **kwargs)
        if self.index.value is not None:
            setattr(
                self.parent.ns_handlers[mn.Appliance_GarageDoor_State].parsers[
                    self.index
                ],  # GarageDoor instance
                key_value_str,
                self,
            )

    async def async_request_value(self, device_value, /):
        device = self.parent
        if self.ns is not mn.Appliance_GarageDoor_Config or not (
            device.descriptor.type.startswith(mc.TYPE_MSG150)
        ):
            return await super().async_request_value(device_value)

        # MSG150 config SET requests also carry signalDuration when changing
        # another setting. The cached value may be missing after going offline.
        handler = device.ns_handlers[self.ns]
        signal_duration = handler.parsers.get(mc.KEY_SIGNALDURATION)
        if signal_duration is None or signal_duration.ns_value is None:
            await handler.async_get()
            signal_duration = handler.parsers[mc.KEY_SIGNALDURATION]
        if signal_duration.ns_value is None:
            raise ValueError("MSG150 config is missing signalDuration")
        payload = {mc.KEY_SIGNALDURATION: signal_duration.ns_value}
        payload.update(self.key_value.payload(device_value))
        await device.async_request(*self.ns.request_set(payload))
        self.update_device_value(device_value)


class GarageConfigSwitch(GarageConfigMixin, SwitchParser):
    """
    switch entity to manage MSG configuration (buzzer, enable)
    'x channel' through mc.NS_APPLIANCE_GARAGEDOOR_MULTIPLECONFIG
    """

    pass


class GarageConfigSwitchMSG150(GarageConfigSwitch):
    """
    Config switch for MSG150 values encoded as 1 (on) and 2 (off).
    """

    _INIT_value_off = 2


class GarageEnableSwitch(GarageConfigSwitch):
    """
    Dedicated entity for "doorEnable" config option in mc.NS_APPLIANCE_GARAGEDOOR_MULTIPLECONFIG
    in order to try enable/disable the same channel associated entities in HA too
    when done with the Meross app (#330)
    """

    def __init__(self, *args, **kwargs):
        GarageConfigSwitch.__init__(self, *args, **kwargs)
        self.schedule_callback(1, self._check_channel_enable)

    @override
    def flush_state(self):
        super().flush_state()
        self.schedule_callback(1, self._check_channel_enable)

    def _check_channel_enable(self, /):
        """enables/disables all the entities of this channel garageDoor in the
        entity registry"""
        registry_update_entity = self.parent.parent.entity_registry.async_update_entity
        disabler = RegistryEntryDisabler.INTEGRATION
        if self.is_on:
            for entity_id in (
                _entry.entity_id
                for _entity in self.parent.entities_iterable
                if (_entity.device_info is self.device_info)
                and (_entity is not self)
                and (_entry := _entity.registry_entry)
                and (_entry.disabled_by == disabler)
            ):
                registry_update_entity(entity_id, disabled_by=None)
        else:
            for entity_id in (
                _entry.entity_id
                for _entity in self.parent.entities_iterable
                if (_entity.device_info is self.device_info)
                and (_entity is not self)
                and (_entry := _entity.registry_entry)
                and not _entry.disabled_by
            ):
                registry_update_entity(entity_id, disabled_by=disabler)


class GarageConfigNumber(GarageConfigMixin, NumberParser):
    """
    number entity to manage MSG configuration (open/close timeout and the likes)
    'x channel' through mc.NS_APPLIANCE_GARAGEDOOR_MULTIPLECONFIG
    """

    # these are ok for almost all config entities (they're mostly durations with
    # milliseconds device_value)
    # customize those when needed...
    _INIT_device_scale = 1000
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
    We'll use a fallback approach based on:
    - if MultipleConfig carries doorOpenDuration/doorCloseDuration keys this will not be invoked since
    the correct entities will be automatically installed (overwriting the initial _DurationHelper instance)
    - if MultipleConfig does not carry doorOpenDuration/doorCloseDuration keys, we check for signalOpen/signalClose
    entities (which are expected to carry the same values in legacy fw) and we use them.
    - if no MultipleConfig and/or no previous entity matches then inspect *.Config entities
    (they'll be used for all the GarageDoor channels as 'shared' configuration)
    - finally, if not any option available, install an emulated number entity with a reasonable default value and use it.
    """

    if TYPE_CHECKING:
        garage_door: Final["GarageDoor"]
        key: Final[str]

    __slots__ = ("garage_door", "key")

    def __init__(self, garage_door: "GarageDoor", key: str):
        self.garage_door = garage_door
        self.key = key

    @property
    def native_value(self):
        # Invoked only once (per key) since it'll also install a definitive
        # number entity
        gd = self.garage_door
        number: "NumberEntity"
        try:
            matching_key = {
                mc.KEY_DOORCLOSEDURATION: mc.KEY_SIGNALCLOSE,
                mc.KEY_DOOROPENDURATION: mc.KEY_SIGNALOPEN,
            }[self.key]
            number = gd.parent.ns_handlers[mn.Appliance_GarageDoor_MultipleConfig].parsers[gd.index].parsers[matching_key]  # type: ignore
        except KeyError:
            try:
                # When GarageDoor.MultipleConfig is not supported we try to use the 'eventually' installed
                # config entities from GarageDoor.Config (legacy fw). This is to avoid installing emulated
                # entities when the device natively supports this configuration.
                # We check if ns GarageConfig already installed a common number entity for this config key
                # (it should do if MultipleConfig is supported and the key is present in the payload)
                number = gd.parent.ns_handlers[mn.Appliance_GarageDoor_Config].parsers[self.key]  # type: ignore
            except KeyError:
                number = EmulatedNumber(
                    gd,
                    entity_key=f"config_{self.key}",
                    native_value=gd._transition_duration,
                    name=self.key,
                    device_class=EmulatedNumber.DEVICE_CLASS_DURATION,
                    native_max_value=60,
                    native_min_value=1,
                    native_step=1,
                )
        setattr(gd, self.key, number)
        return number.native_value


class GarageDoorMultipleConfig(MappingParser):

    _INIT_parser_defs = {
        mc.KEY_BUZZERENABLE: GarageConfigSwitch.DEF(),
        mc.KEY_DOORENABLE: GarageEnableSwitch.DEF(),
        mc.KEY_SIGNALDURATION: GarageConfigNumber.DEF(
            native_step=0.1,
            native_min_value=0.1,
        ),
        mc.KEY_SIGNALCLOSE: GarageConfigNumber.DEF(),
        mc.KEY_SIGNALOPEN: GarageConfigNumber.DEF(),
        mc.KEY_DOORCLOSEDURATION: GarageConfigNumber.DEF(),
        mc.KEY_DOOROPENDURATION: GarageConfigNumber.DEF(),
    }


class GarageDoorConfig(MappingParserHandler):

    PARAM_LIGHT_UPDATE_PERIOD = 10
    """local polling period for MSG150 opener light status"""

    _INIT_parser_defs = {
        key: GarageDoorMultipleConfig._INIT_parser_defs[key]
        for key in (
            mc.KEY_BUZZERENABLE,
            mc.KEY_SIGNALDURATION,
            mc.KEY_DOORCLOSEDURATION,
            mc.KEY_DOOROPENDURATION,
        )
    }

    @classmethod
    @override
    def namespace_init(
        cls,
        ns: mn.Namespace,
        device: "Device",
        /,
        **kwargs: "Unpack[NamespaceHandler.Args]",
    ):
        handler = super().namespace_init(ns, device, **kwargs)
        if device.descriptor.type.startswith(mc.TYPE_MSG150):
            handler.parser_defs = {
                **handler.parser_defs,
                mc.KEY_LED: GarageConfigSwitchMSG150.DEF(
                    name="Opener light", entity_category=None
                ),
                mc.KEY_OBENABLE: GarageConfigSwitchMSG150.DEF(
                    name="Obstruction reporting"
                ),
                mc.KEY_LOCKREMOTE: GarageConfigSwitchMSG150.DEF(name="Remote lock"),
            }
            # FW 7.1.8 does not PUSH Config updates for light changes made by
            # motion or the wall panel. Cloud polling keeps the config default.
            handler.polling_period = cls.PARAM_LIGHT_UPDATE_PERIOD
        return handler

    def _handle(self, message: "MerossMessage", /):
        handler = super()._handle
        handler(message)
        # mc.KEY_DOOROPENDURATION and mc.KEY_DOORCLOSEDURATION config keys have been
        # removed in recent firmwares (migrated to MultipleConfig x channel #82).
        # We keep implementing emulated entities in case for legacy firmwares.
        for gd in self.parent.ns_handlers[
            mn.Appliance_GarageDoor_State
        ].parsers.values():
            # in case MULTIPLECONFIG is supported
            # this code does nothing since everything should already be in place.
            # This is just to trigger the _DurationHelper in case it's installed
            # and install the related entities in GarageDoor if not already there.
            for key in (mc.KEY_DOOROPENDURATION, mc.KEY_DOORCLOSEDURATION):
                getattr(gd, key).native_value
        # Current method is only executed once to detect mc.KEY_DOOROPENDURATION, mc.KEY_DOORCLOSEDURATION.
        # After this, we install the base class method as handler so to skip repeating checks.
        self.handler = handler


class GarageDoor(Cover.RestoreEntity, Cover):

    if TYPE_CHECKING:

        ENTITY_DEFS: dict[str, type[GarageConfigMixin]]

        binary_sensor_timeout: GarageTimeoutBinarySensor
        binary_sensor_obstruction: GarageObstructionBinarySensor | None
        doorEnable: GarageEnableSwitch
        doorCloseDuration: NumberEntity | _DurationHelper
        doorOpenDuration: NumberEntity | _DurationHelper

    PARAM_TRANSITION_MAXDURATION = 60
    PARAM_TRANSITION_MINDURATION = 10
    ATTR_TRANSITION_DURATION = "transition_duration"

    # HA core entity attributes:
    _attr_device_class = Cover.DeviceClass.GARAGE
    _attr_supported_features = Cover.EntityFeature.OPEN | Cover.EntityFeature.CLOSE

    __slots__ = (
        "_config",
        "_transition_duration",
        "_transition_start",
        "_transition_timeout_retry",
        "binary_sensor_timeout",
        "binary_sensor_obstruction",
        *GarageDoorMultipleConfig._INIT_parser_defs.keys(),
    )

    def __init__(self, id, device: "Device", /, **kwargs: "Unpack[Cover.Args]"):
        self._transition_duration = (
            self.PARAM_TRANSITION_MAXDURATION + self.PARAM_TRANSITION_MINDURATION
        ) / 2
        self._transition_start = 0.0
        self._transition_timeout_retry = False
        self.extra_state_attributes = {
            self.ATTR_TRANSITION_DURATION: self._transition_duration
        }
        Cover.__init__(self, id, device, **kwargs)
        self.binary_sensor_timeout = GarageTimeoutBinarySensor(self)
        self.binary_sensor_obstruction = None
        # garage run timeouts config availability is rather articulated across different fw.
        # This approach allows to have dynamic entities that adapt to the actual device capabilities
        # while still providing a fallback solution to cover the cases where the
        # device does not report these configuration options at all.
        self.doorCloseDuration = _DurationHelper(self, mc.KEY_DOORCLOSEDURATION)
        self.doorOpenDuration = _DurationHelper(self, mc.KEY_DOOROPENDURATION)
        # ToggleX behavior in cover (garage/rollershutter) is not very clear
        # most devices expose the ns in abilities and maybe also channel indexes in digest
        # but the effect of toggling is unknown. We just silence any incoming message here.
        device.register_togglex_channel(self)

    async def async_added_to_hass(self):
        with self.exception_warning("restoring previous state"):
            if restored_state := self._async_get_restored_data():
                try:
                    # to restore previously estimated transition duration
                    self._transition_duration = restored_state.state.attributes[
                        self.ATTR_TRANSITION_DURATION
                    ]
                    self.extra_state_attributes[self.ATTR_TRANSITION_DURATION] = (
                        self._transition_duration
                    )
                except KeyError:
                    pass
        await Cover.async_added_to_hass(self)

    # interface: cover.CoverEntity
    async def async_open_cover(self, **kwargs):
        await self.async_request_position(1)

    async def async_close_cover(self, **kwargs):
        await self.async_request_position(0)

    # interface: self
    async def async_request_position(self, open_request: int, /):
        self._transition_cancel()
        is_msg150 = self.parent.descriptor.type.startswith(mc.TYPE_MSG150)
        payload = {mc.KEY_OPEN: open_request}
        if is_msg150:
            payload[mc.KEY_UUID] = self.parent.id
        response = await self.async_request_payload(payload)
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
        if is_msg150:
            # MSG150 echoes the target in SETACK before movement finishes.
            # The position is only confirmed by a GET, PUSH or digest update.
            _open = None if self.is_closed is None else not self.is_closed
        else:
            _open = p_state[mc.KEY_OPEN]
            self.is_closed = not _open
        if p_state.get(mc.KEY_EXECUTE) and open_request != _open:
            device = self.parent
            self._transition_start = device.last_rx_epoch
            if open_request:
                self.is_closing = False
                self.is_opening = True
                timeout = self.doorOpenDuration.native_value
            else:
                self.is_closing = True
                self.is_opening = False
                timeout = self.doorCloseDuration.native_value
            self.schedule_callback(0.9, self._transition_callback)
            # check the timeout after expected to account
            # for delays in communication
            self.schedule_callback(
                (timeout or self._transition_duration),  # type: ignore
                self._async_transition_end_callback,
            )

        self.flush_state()

    @override
    def __call__(self, payload: "mt.garagedoor.State", /):
        try:
            # appeared on msg200 fw:4.2.8
            self.doorEnable.update_device_value(payload[mc.KEY_DOORENABLE])  # type: ignore
        except (AttributeError, KeyError):
            pass

        attributes_changed = False
        if self.parent.descriptor.type.startswith(mc.TYPE_MSG150):
            if mc.KEY_OBSTRUCTION in payload:
                if self.binary_sensor_obstruction is None:
                    self.binary_sensor_obstruction = GarageObstructionBinarySensor(
                        self, ns=self.ns, ns_value=payload[mc.KEY_OBSTRUCTION]
                    )
                else:
                    self.binary_sensor_obstruction.update_device_value(
                        payload[mc.KEY_OBSTRUCTION]
                    )
            for key in (mc.KEY_OBSTRUCTION, mc.KEY_OBLMTIME, mc.KEY_SIGNALVALID):
                if (
                    key in payload
                    and self.extra_state_attributes.get(key) != payload[key]
                ):
                    self.extra_state_attributes[key] = payload[key]
                    attributes_changed = True

        is_closed = not payload[mc.KEY_OPEN]
        if self.is_closed == is_closed:
            if attributes_changed:
                self.flush_state()
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

    def _transition_cancel(self, /):
        self.is_closing = False
        self.is_opening = False
        self._transition_start = 0.0
        self._transition_timeout_retry = False
        Cover._transition_cancel(self)

    @override
    def _transition_callback(self, /):
        if self.parent.transport is Transport.HTTP and not self.parent.mqtt_active:
            self.handler_ns.schedule_get(self.index)

    @override
    async def _async_transition_end_callback(self, /):
        """
        checks the transition did finish as per the timeout(s)
        """
        was_closing = self.is_closing
        transition_start = self._transition_start
        if was_closing:
            # When closing we expect this callback not to be called since
            # the transition should be terminated by our parser callback,
            # provided it gets called on time (on polling this is not guaranteed).
            # If we're here, we still havent received a proper 'physical close'
            # because our configured closeduration is too short
            # or the garage didnt close at all
            if not self._transition_timeout_retry and self._transition_duration < (
                self.time() - transition_start
            ):
                self._update_transition_duration(self._transition_duration + 1)

        self.is_closing = False
        self.is_opening = False
        self._transition_start = 0.0

        if was_closing != self.is_closed:
            # looks like on MQTT we don't receive a PUSHed state update? (#415)
            try:
                await self.handler_ns.async_get(self.index)
                # the request/response parse already flushed the state
                if was_closing == self.is_closed:
                    self.binary_sensor_timeout.update_ok(was_closing)
                else:
                    self._transition_timeout(was_closing, transition_start)
            except Exception:
                self._transition_timeout(was_closing, transition_start, flush=True)
        else:
            self.flush_state()
            self.binary_sensor_timeout.update_ok(was_closing)

    def _transition_timeout(
        self, was_closing: bool, transition_start: float, /, *, flush: bool = False
    ):
        if (
            self.parent.descriptor.type.startswith(mc.TYPE_MSG150)
            and not self._transition_timeout_retry
        ):
            # MSG150 FW 7.1.8 can PUSH the final state after the configured
            # timeout. One extra check allows for this reporting delay.
            self._transition_timeout_retry = True
            self._transition_start = transition_start
            self.is_closing = was_closing
            self.is_opening = not was_closing
            self.schedule_callback(5, self._async_transition_end_callback)
            return
        if flush:
            self.flush_state()
        self.binary_sensor_timeout.update_timeout(was_closing)

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
    def namespace_init(
        cls,
        ns: mn.Namespace,
        device: "Device",
        /,
        **kwargs: "Unpack[NamespaceHandler.Args]",
    ):
        descriptor = device.descriptor
        if descriptor.type.startswith(mc.TYPE_MSG200) and (
            descriptor.fw_version_t <= (4, 2, 1)
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
            device.handler_all.polling_period = 0

        # do not register_parser_class since we don't want to create spurious
        # GarageDoor at channel 0 (msg200)
        nh = NamespaceHandler(ns, device, **kwargs)
        for channel_digest in ns.get_digest(descriptor.digest):
            channel = channel_digest[mc.KEY_CHANNEL]
            nh.register_parser(
                GarageDoor(
                    channel, device, ns=ns, index=mn.IndexType.channel.get(channel)
                )
            )
        return nh
