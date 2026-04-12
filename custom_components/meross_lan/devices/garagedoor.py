from typing import TYPE_CHECKING, override

from homeassistant.helpers.entity_registry import RegistryEntryDisabler
from homeassistant.util.dt import now

from ..binary_sensor import BinarySensorEntity
from ..cover import Cover
from ..helpers import clamp
from ..helpers.namespaces import EntityDefNamespaceHandler, NamespaceHandler, mc, mn
from ..merossclient.client import Transport
from ..number import EmulatedNumber, NumberParser
from ..switch import SwitchParser

if TYPE_CHECKING:
    from typing import Any, Final, NotRequired, Unpack

    from ..helpers.device import Device, MerossMessage
    from ..helpers.entity import ValueParser
    from ..merossclient.protocol import types as mt
    from ..number import NumberEntity


class GarageTimeoutBinarySensor(BinarySensorEntity):

    init_entity_key = "problem"
    init_is_on = False
    # the time at which the transition timeout occurred
    ATTR_TRANSITION_TIMEOUT = "transition_timeout"
    # the target state which was not reached
    ATTR_TRANSITION_TARGET = "transition_target"

    # HA core entity attributes:
    _attr_device_class = BinarySensorEntity.DeviceClass.PROBLEM
    _attr_entity_category = BinarySensorEntity.EntityCategory.DIAGNOSTIC
    _unrecorded_attributes = frozenset(
        {
            ATTR_TRANSITION_TARGET,
            ATTR_TRANSITION_TIMEOUT,
            *BinarySensorEntity._unrecorded_attributes,
        }
    )

    def __init__(self, *args, **kwargs):
        self.extra_state_attributes = {}
        super().__init__(*args, **kwargs)

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

        type InitArgs = ValueParser.InitArgs

        class Args(ValueParser.Args):
            device_value: NotRequired[int]

    # Assuming by default we're parsing MultipleConfig
    # This will be overriden in kwargs when creating entities for Appliance.GarageDoor.Config
    init_ns = mn.Appliance_GarageDoor_MultipleConfig

    def __init__(self, *args: "*InitArgs", **kwargs: "Unpack[Args]"):
        key_value = kwargs["key_value"]  # type: ignore
        kwargs["entity_key"] = f"config_{key_value}"
        kwargs["name"] = key_value
        super().__init__(*args, **kwargs)


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
        enabled = self.is_on
        registry_update_entity = self.parent.parent.entity_registry.async_update_entity
        disabler = RegistryEntryDisabler.INTEGRATION
        for entry in (
            _entry
            for entity in self.parent.entities.values()
            if (entity.device_info is self.device_info)
            and (entity is not self)
            and (_entry := entity.registry_entry)
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

        class Args(GarageConfigMixin.Args, NumberParser.Args):
            pass

        def __init__(self, *args, **kwargs: Unpack[Args]): ...

    # these are ok for almost all config entities (they're mostly durations with
    # milliseconds device_value)
    # customize those when needed...
    init_device_scale = 1000
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
        try:
            # When GarageDoor.MultipleConfig is not supported we try to use the 'eventually' installed
            # config entities from GarageDoor.Config (legacy fw). This is to avoid installing emulated
            # entities when the device natively supports this configuration.
            # We check if ns GarageConfig already installed a common number entity for this config key
            # (it should do if MultipleConfig is supported and the key is present in the payload)
            number: "NumberEntity" = gd.parent.ns_handlers[mn.Appliance_GarageDoor_Config].parsers[self.key]  # type: ignore
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
        setattr(gd, f"number_{self.key}", number)
        return number.native_value


class GarageDoor(Cover):

    if TYPE_CHECKING:

        ENTITY_DEFS: dict[str, type[GarageConfigMixin]]

        binary_sensor_timeout: GarageTimeoutBinarySensor
        number_doorCloseDuration: NumberEntity | _DurationHelper
        number_doorOpenDuration: NumberEntity | _DurationHelper

    PARAM_TRANSITION_MAXDURATION = 60
    PARAM_TRANSITION_MINDURATION = 10
    ATTR_TRANSITION_DURATION = "transition_duration"

    # these keys in Appliance.GarageDoor.MultipleConfig are to be ignored
    CONFIG_KEY_EXCLUDED = (mc.KEY_CHANNEL, mc.KEY_TIMESTAMP, mc.KEY_TIMESTAMPMS)
    ENTITY_DEFS = {
        mc.KEY_BUZZERENABLE: GarageConfigSwitch.ENTITY_DEF(
            key_value=GarageConfigSwitch.SimpleKeyValue(mc.KEY_BUZZERENABLE)
        ),
        mc.KEY_DOORENABLE: GarageEnableSwitch.ENTITY_DEF(
            key_value=GarageEnableSwitch.SimpleKeyValue(mc.KEY_DOORENABLE)
        ),
        mc.KEY_SIGNALDURATION: GarageConfigNumber.ENTITY_DEF(
            key_value=GarageConfigNumber.SimpleKeyValue(mc.KEY_SIGNALDURATION),
            native_step=0.1,
            native_min_value=0.1,
        ),
        mc.KEY_SIGNALCLOSE: GarageConfigNumber.ENTITY_DEF(
            key_value=GarageConfigNumber.SimpleKeyValue(mc.KEY_SIGNALCLOSE)
        ),
        mc.KEY_SIGNALOPEN: GarageConfigNumber.ENTITY_DEF(
            key_value=GarageConfigNumber.SimpleKeyValue(mc.KEY_SIGNALOPEN)
        ),
        mc.KEY_DOORCLOSEDURATION: GarageConfigNumber.ENTITY_DEF(
            key_value=GarageConfigNumber.SimpleKeyValue(mc.KEY_DOORCLOSEDURATION)
        ),
        mc.KEY_DOOROPENDURATION: GarageConfigNumber.ENTITY_DEF(
            key_value=GarageConfigNumber.SimpleKeyValue(mc.KEY_DOOROPENDURATION)
        ),
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

    def __init__(self, id, device: "Device", /, **kwargs: "Unpack[Cover.Args]"):
        self._config = {}
        self._transition_duration = (
            self.PARAM_TRANSITION_MAXDURATION + self.PARAM_TRANSITION_MINDURATION
        ) / 2
        self._transition_start = 0.0
        self.extra_state_attributes = {
            self.ATTR_TRANSITION_DURATION: self._transition_duration
        }
        Cover.__init__(self, id, device, **kwargs)
        self.binary_sensor_timeout = GarageTimeoutBinarySensor(self)
        if mn.Appliance_GarageDoor_MultipleConfig in device.descriptor.ability:
            # historically, when MultipleConfig appeared, these used to be
            # the available timeouts while recent fw (4.2.8) shows presence
            # of more 'natural' doorOpenDuration/doorCloseDuration keys.
            # We'll then override this initial guessing when we _parse_config
            # should those new keys appear
            self.number_doorCloseDuration = self.__class__.ENTITY_DEFS[  # type: ignore
                mc.KEY_SIGNALCLOSE
            ](self)
            self.number_doorOpenDuration = self.__class__.ENTITY_DEFS[  # type: ignore
                mc.KEY_SIGNALOPEN
            ](self)
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
        # ToggleX behavior in cover (garage/rollershutter) is not very clear
        # most devices expose the ns in abilities and maybe also channel indexes in digest
        # but the effect of toggling is unknown. We just silence any incoming message here.
        device.register_togglex_channel(self, False)

    def shutdown(self):
        Cover.shutdown(self)
        del self.number_doorCloseDuration
        del self.number_doorOpenDuration

    async def async_added_to_hass(self):
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
        await Cover.async_added_to_hass(self)

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
        response = await self.async_request_payload({mc.KEY_OPEN: open_request})
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

    @override
    def _parse(self, payload: "mt.garagedoor.State", /):
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

    def _parse_multipleConfig(self, payload: "mt.JsonMapping", /):
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
        # TODO: swap in a Dispatcher with the collection of entities.
        entities = self.parent.entities
        entity_id_prefix = f"{self.index.value}_config_"
        for key, value in payload.items():
            if key in GarageDoor.CONFIG_KEY_EXCLUDED or (
                self._config.get(key) == value
            ):
                continue
            self._config[key] = value  # useless ?
            try:
                try:
                    entities[f"{entity_id_prefix}{key}"].update_device_value(value)
                except KeyError:
                    entity = self.parent.add_entity(
                        self.__class__.ENTITY_DEFS[key](self, device_value=value)
                    )
                    if key in (mc.KEY_DOORCLOSEDURATION, mc.KEY_DOOROPENDURATION):
                        setattr(self, f"number_{key}", entity)
            except Exception as exception:
                self.log_exception(
                    self.WARNING,
                    exception,
                    "_parse_multipleConfig (payload=%s)",
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
            self.handler_ns.schedule_get(self.index)

    @override
    async def _async_transition_end_callback(self, /):
        """
        checks the transition did finish as per the timeout(s)
        """
        was_closing = self.is_closing
        if was_closing:
            # when closing we expect this callback not to be called since
            # the transition should be terminated by '_parse' provided it gets
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
                await self.handler_ns.async_get(self.index)
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
    def namespace_init(cls, ns: mn.Namespace, device: "Device", /):
        handler = NamespaceHandler(ns, device)
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
            device.handler_all.polling_period = 0

        # do not register_parser_class since we don't want to create spurious
        # GarageDoor at channel 0 (msg200)
        for channel_digest in ns.get_digest(descriptor.digest):
            index = mn.IndexType.channel.value_of(channel_digest)
            handler.register_parser(GarageDoor(index.value, device, ns=ns, index=index))


class GarageDoorConfigNamespaceHandler(EntityDefNamespaceHandler):

    if TYPE_CHECKING:

        parsers: Final[dict[str, ValueParser]]  # type: ignore[override]
        init_entity_defs: Final[dict[str, type[GarageConfigMixin]]]
        entity_defs: Final[dict[str, type[GarageConfigMixin]]]  # type: ignore[override]

    POLLING_CONFIG_DEFAULT = EntityDefNamespaceHandler.POLLING_CONFIG_CONFIGURATION

    init_entity_defs = {
        key: GarageDoor.ENTITY_DEFS[key]
        for key in (
            mc.KEY_BUZZERENABLE,
            mc.KEY_SIGNALDURATION,
            mc.KEY_DOORCLOSEDURATION,
            mc.KEY_DOOROPENDURATION,
        )
    }

    def _handle(self, message: "MerossMessage", /):
        EntityDefNamespaceHandler._handle(self, message)
        # mc.KEY_DOOROPENDURATION and mc.KEY_DOORCLOSEDURATION config keys have been
        # removed in recent firmwares (migrated to MultipleConfig x channel #82).
        # We keep implementing emulated entities in case for legacy firmwares.
        for garagedoor in self.parent.ns_handlers[
            mn.Appliance_GarageDoor_State
        ].parsers.values():
            assert isinstance(garagedoor, GarageDoor)
            # in case MULTIPLECONFIG is supported
            # this code does nothing since everything should already in place.
            # This is just to trigger the _DurationHelper in case it's installed
            for key in (mc.KEY_DOOROPENDURATION, mc.KEY_DOORCLOSEDURATION):
                getattr(garagedoor, f"number_{key}").native_value
        # Current class method is only executed once to detect mc.KEY_DOOROPENDURATION, mc.KEY_DOORCLOSEDURATION.
        # After this, we install the base class method as handler so to skip repeating checks.
        self.handler = super()._handle


NamespaceHandler.POLLING_CONFIG_MAP.update(
    {
        mn.Appliance_GarageDoor_MultipleConfig: NamespaceHandler.POLLING_CONFIG_CONFIGURATION,
    }
)
