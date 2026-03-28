import enum
from typing import TYPE_CHECKING, override

from homeassistant.components import climate, sensor
from homeassistant.core import CoreState, callback
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.util.unit_conversion import TemperatureConverter

from .calendar import MtsSchedule
from .const import hac
from .helpers import reverse_lookup
from .helpers.entity import ParserEntity
from .number import NumberParser
from .select import SelectEntity
from .sensor import SensorParser

if TYPE_CHECKING:
    from typing import ClassVar, Final, Unpack

    from homeassistant.core import Event, State
    from homeassistant.helpers.event import EventStateChangedData

    from .helpers.device import Device
    from .helpers.entity import ChannelType
    from .helpers.namespaces import mn


class MtsClimate(ParserEntity, climate.ClimateEntity):

    class Preset(enum.StrEnum):
        CUSTOM = "custom"
        COMFORT = climate.PRESET_COMFORT
        SLEEP = climate.PRESET_SLEEP
        AWAY = climate.PRESET_AWAY
        AUTO = "auto"

    class AdjustNumber(NumberParser):

        _attr_name = "Calibration"
        _attr_device_class = NumberParser.DEVICE_CLASS_TEMPERATURE_DELTA

    class SetPointNumber(NumberParser):
        """
        Helper entity to configure MTS100/150/200 setpoints
        AKA: Heat(comfort) - Cool(sleep) - Eco(away)
        """

        if TYPE_CHECKING:
            climate: "MtsClimate"

            class Args(NumberParser.Args):
                climate: "MtsClimate"
                ns: mn.Namespace
                key_value: str

            def __init__(
                self,
                channel: ChannelType,
                parent: Device,
                /,
                **kwargs: Unpack[Args],
            ): ...

        _attr_device_class = NumberParser.DeviceClass.TEMPERATURE

        SLOTS_AUTO_INIT = ("climate", "icon")
        __slots__ = ()

        async def async_request_value(self, device_value, /):
            # This implementation is only valid for mts100/mts200 where
            # this entity state is actually parsed in the related MtsClimate.
            # We'll then forward the callback to the climate entity in order to
            # ensure the climate state is consistent after a setpoint change.
            # Consider both ns reply with the full state in the SETACK response.
            return await self.climate.async_request_parse_ex(
                {self.key_value: device_value}
            )

    Schedule = MtsSchedule
    """Overriden in derived to provide specific behavior."""

    class TrackSensorSelect(SelectEntity):
        """
        A select entity used to select among all temperature sensors in HA
        an entity to track so that the thermostat regulates T against
        that other sensor. The idea is to track changes in
        the tracked entitites and adjust the MTS temp correction on the fly
        """

        if TYPE_CHECKING:
            TRACKING_DELAY: Final[int]
            """Delay before tracking updates are applied after a triggering event."""
            TRACKING_DEADTIME: Final[int]
            """minimum delay (dead-time) between trying to adjust the climate entity."""
            climate: "MtsClimate"

            def __init__(
                self,
                channel: ChannelType | None,
                parent: Device,
                /,
                climate: "MtsClimate",
            ): ...

        init_entity_key = "tracked_sensor"
        TRACKING_DELAY = 5
        TRACKING_DEADTIME = 60

        # HA core entity attributes:
        _attr_available = True
        _attr_entity_registry_enabled_default = False

        init__track_last_epoch = 0
        SLOTS_AUTO_INIT = (
            "climate",
            "_tracking_state",
            "_tracking_state_change_unsub",
            "_track_last_epoch",
        )
        __slots__ = ()

        @override
        async def async_shutdown(self):
            self._tracking_stop()
            await super().async_shutdown()
            del self.climate

        @override
        def set_unavailable(self):
            self.cancel_callback(self._track)

        async def async_added_to_hass(self):
            hass = self.hass

            if not self.current_option:
                with self.exception_warning("restoring previous state"):
                    if last_state := await self.get_last_state_available():
                        self.current_option = last_state.state

            if hass.state == CoreState.running:
                self._setup_tracking_entities()
            else:
                # setup a temp list in order to not loose restored state
                # since HA validates 'current_option' against 'options'
                # when persisting the state and we could loose the
                # current restored state if we don't setup the tracking
                # list soon enough
                hass.bus.async_listen_once(
                    hac.EVENT_HOMEASSISTANT_STARTED,
                    self._setup_tracking_entities,
                )

            # call super after (eventually) calling _setup_tracking_entities since it
            # could flush the new state (it should only when called by the hass bus)
            await super().async_added_to_hass()

        async def async_will_remove_from_hass(self):
            self._tracking_stop()
            await super().async_will_remove_from_hass()

        # interface: SelectEntity
        @override
        async def async_select_option(self, option: str):
            self.update_option(option)
            self._tracking_start()

        # interface: self
        def check_tracking(self):
            """
            called when either the climate or the tracked_entity has a new
            temperature reading in order to see if the climate needs to be adjusted
            """
            self.cancel_callback(self._track)

            if not self.parent.is_connected or not self._tracking_state_change_unsub:
                return
            tracked_state = self._tracking_state
            if not tracked_state:
                # we've setup tracking but the entity doesn't exist in the
                # state machine...was it removed from HA ?
                self.log(
                    self.WARNING,
                    "Tracked entity (%s) state is missing: was it removed from HomeAssistant ?",
                    self.current_option,
                    timeout=14400,
                )
                return
            if tracked_state.state in (
                hac.STATE_UNAVAILABLE,
                hac.STATE_UNKNOWN,
            ):
                # might be transient so we don't take any action or log
                return

            # Always use some delay between this call and the effective calibration
            # since there might be some concurrent 'almost synchronous' updates in HA
            # and we want to avoid synchronizing in a 'glitch'.
            # This way, repeated 'check_tracking' calls will
            # invalidate each other and just apply the latest (supposedly stable) one.
            # See also https://github.com/krahabb/meross_lan/issues/593 for a particularly
            # difficult case (even tho a bit paroxysmal).
            delay = self.time() - self._track_last_epoch
            self.schedule_callback(
                (
                    self.TRACKING_DELAY
                    if delay > self.TRACKING_DEADTIME
                    else self.TRACKING_DEADTIME - delay
                ),
                self._track,
                tracked_state,
            )

        @callback
        def _setup_tracking_entities(self, *_):
            _units = (
                hac.UnitOfTemperature.CELSIUS,
                hac.UnitOfTemperature.FAHRENHEIT,
            )
            self.options = [hac.STATE_OFF]
            self.options.extend(
                entity.entity_id
                for entity in self.hass.data[sensor.DATA_COMPONENT].entities
                if getattr(entity, "native_unit_of_measurement", None) in _units
            )
            if self.current_option not in self.options:
                # this might happen when restoring a not anymore valid entity
                self.current_option = hac.STATE_OFF

            self.flush_state()
            self._tracking_start()

        def _tracking_start(self):
            self._tracking_stop()
            entity_id = self.current_option
            if entity_id not in (
                None,
                hac.STATE_OFF,
                hac.STATE_UNKNOWN,
                hac.STATE_UNAVAILABLE,
            ):

                @callback
                def _tracking_callback(event: "Event[EventStateChangedData]"):
                    with self.exception_warning("processing state update event"):
                        self._tracking_state_change(event.data.get("new_state"))

                self._tracking_state_change_unsub = async_track_state_change_event(
                    self.hass, entity_id, _tracking_callback
                )
                self._tracking_state_change(self.hass.states.get(entity_id))

        def _tracking_stop(self):
            if self._tracking_state_change_unsub:
                self._tracking_state_change_unsub()
                self._tracking_state_change_unsub = None
                self._tracking_state = None
            self.cancel_callback(self._track)

        def _tracking_state_change(self, tracked_state: "State | None"):
            self._tracking_state = tracked_state
            self.check_tracking()

        def _track(self, tracked_state: "State"):
            """This is only called internally after a timeout when tracking needs to be updated
            due to state changes in either tracked entity or climate."""
            climate = self.climate
            current_temperature = climate.current_temperature
            if not current_temperature:
                # should be transitory - just a safety check
                return
            number_adjust_temperature = climate.number_adjust_temperature
            current_adjust_temperature = number_adjust_temperature.native_value
            if current_adjust_temperature is None:
                # should be transitory - just a safety check
                return
            with self.exception_warning("_track", timeout=900):
                tracked_temperature = float(tracked_state.state)
                # ensure tracked_temperature is °C
                tracked_temperature_unit = tracked_state.attributes.get(
                    hac.ATTR_UNIT_OF_MEASUREMENT
                )
                if not tracked_temperature_unit:
                    raise ValueError("tracked entity has no unit of measure")
                if tracked_temperature_unit != climate.temperature_unit:
                    tracked_temperature = TemperatureConverter.convert(
                        tracked_temperature,
                        tracked_temperature_unit,
                        climate.temperature_unit,
                    )
                error_temperature = tracked_temperature - current_temperature
                native_error_temperature = round(
                    error_temperature * climate.temperature_scale
                )
                if not native_error_temperature:
                    # tracking error within device resolution limits..we're ok
                    self.log(
                        self.DEBUG,
                        "Skipping %s calibration (no tracking error)",
                        climate.entity_id,
                    )
                    return
                adjust_temperature = current_adjust_temperature + error_temperature
                # check if our correction is within the native adjust limits
                # and avoid sending (useless) adjust commands
                if adjust_temperature > number_adjust_temperature.native_max_value:
                    if (
                        current_adjust_temperature
                        >= number_adjust_temperature.native_max_value
                    ):
                        self.log(
                            self.DEBUG,
                            "Skipping %s calibration (%s [%s] beyond %s limit)",
                            climate.entity_id,
                            current_adjust_temperature,
                            climate.temperature_unit,
                            number_adjust_temperature.native_max_value,
                        )
                        return
                    adjust_temperature = number_adjust_temperature.native_max_value
                elif adjust_temperature < number_adjust_temperature.native_min_value:
                    if (
                        current_adjust_temperature
                        <= number_adjust_temperature.native_min_value
                    ):
                        self.log(
                            self.DEBUG,
                            "Skipping %s calibration (%s [%s] below %s limit)",
                            climate.entity_id,
                            current_adjust_temperature,
                            climate.temperature_unit,
                            number_adjust_temperature.native_min_value,
                        )
                        return
                    adjust_temperature = number_adjust_temperature.native_min_value
                self._track_last_epoch = self.time()
                number_adjust_temperature.create_task(
                    number_adjust_temperature.async_set_native_value(
                        adjust_temperature
                    ),
                    f"TrackSensorSelect._track(adjust_temperature={adjust_temperature} [{climate.temperature_unit}])",
                    eager_start=True,
                )
                self.log(
                    self.DEBUG,
                    "Applying %s calibration (%s [%s])",
                    climate.entity_id,
                    adjust_temperature,
                    climate.temperature_unit,
                )

    if TYPE_CHECKING:
        ATTR_HVAC_MODE: Final
        ATTR_TEMPERATURE: Final
        ATTR_TARGET_TEMP_HIGH: Final
        ATTR_TARGET_TEMP_LOW: Final

        temperature_scale: ClassVar[int]

        SCHEDULE_NS: ClassVar[mn.Namespace]
        Schedule: ClassVar[type[MtsSchedule]]
        TARGET_TEMPERATURE_STEP: ClassVar[float]
        MTS_MODE_TO_PRESET_MAP: ClassVar[dict[int | None, str]]
        """Maps device 'mode' value to the HA climate.preset_mode"""
        MTS_MODE_TO_TEMPERATUREKEY_MAP: ClassVar[dict[int | None, str]]
        """Maps the current mts mode to the name of a temperature setpoint key.
        Used also to setup SetPointNumber entities (when empty -> no setpoints)."""
        SETPOINT_ICON_MAP: Final[dict[Preset, str]]
        """Simple map to configure SetPointNumber icons."""
        SET_TEMP_FORCE_MANUAL_MODE: Final[bool]
        """Determines the behavior of async_set_temperature."""

        channel: Final[ChannelType]  # type: ignore[override]
        number_adjust_temperature: Final[NumberParser]
        number_preset_temperature: Final[set[SetPointNumber]]
        schedule: Final[MtsSchedule]
        select_track_sensor: Final[TrackSensorSelect]
        sensor_current_temperature: Final[SensorParser]
        _mts_active: bool | int
        _mts_mode: int
        _mts_onoff: int

        # HA core entity attributes override:
        _attr_preset_modes: ClassVar[list[str]]
        _attr_supported_features: ClassVar[climate.ClimateEntityFeature]

        current_humidity: float | None
        current_temperature: float | None
        hvac_action: climate.HVACAction | None
        hvac_mode: climate.HVACMode | None
        max_temp: float
        min_temp: float
        preset_mode: str | None
        preset_modes: list[str]
        supported_features: climate.ClimateEntityFeature
        target_temperature: float | None
        target_temperature_step: float
        temperature_unit: Final[str]

    PLATFORM = climate.DOMAIN

    ATTR_HVAC_MODE = climate.ATTR_HVAC_MODE
    ATTR_TEMPERATURE = climate.ATTR_TEMPERATURE
    ATTR_TARGET_TEMP_HIGH = climate.ATTR_TARGET_TEMP_HIGH
    ATTR_TARGET_TEMP_LOW = climate.ATTR_TARGET_TEMP_LOW

    ClimateEntityFeature = climate.ClimateEntityFeature
    HVACAction = climate.HVACAction
    HVACMode = climate.HVACMode

    temperature_scale = 1

    TARGET_TEMPERATURE_STEP = 0.5
    MTS_MODE_TO_TEMPERATUREKEY_MAP = {}
    SETPOINT_ICON_MAP = {
        Preset.COMFORT: "mdi:sun-thermometer",
        Preset.SLEEP: "mdi:power-sleep",
        Preset.AWAY: "mdi:bag-checked",
    }
    SET_TEMP_FORCE_MANUAL_MODE = True
    """Determines the behavior of async_set_temperature."""
    # HA core entity attributes:
    _attr_hvac_modes = [HVACMode.OFF, HVACMode.HEAT]
    _attr_preset_modes = list(Preset)
    _attr_supported_features = (
        climate.ClimateEntityFeature.PRESET_MODE
        | climate.ClimateEntityFeature.TARGET_TEMPERATURE
        | getattr(climate.ClimateEntityFeature, "TURN_OFF", 0)
        | getattr(climate.ClimateEntityFeature, "TURN_ON", 0)
    )
    _enable_turn_on_off_backwards_compatibility = False

    _attr_translation_key = "mts_climate"

    __slots__ = (
        "current_humidity",
        "current_temperature",
        "hvac_action",
        "hvac_mode",
        "hvac_modes",
        "max_temp",
        "min_temp",
        "preset_mode",
        "preset_modes",
        "supported_features",
        "target_temperature",
        "target_temperature_step",
        "temperature_unit",
        "_mts_active",
        "_mts_mode",
        "_mts_onoff",
        "number_adjust_temperature",
        "number_preset_temperature",
        "schedule",
        "select_track_sensor",
        "sensor_current_temperature",
    )

    def __init__(self, channel: "ChannelType", parent: "Device", /, **kwargs):
        self.current_humidity = None
        self.current_temperature = None
        self.hvac_action = None
        self.hvac_mode = None
        self.hvac_modes = self._attr_hvac_modes
        self.max_temp = 35
        self.min_temp = 5
        self.preset_mode = None
        self.preset_modes = self._attr_preset_modes
        self.supported_features = self._attr_supported_features
        self.target_temperature = None
        self.target_temperature_step = self.TARGET_TEMPERATURE_STEP
        self.temperature_unit = hac.UnitOfTemperature.CELSIUS
        self._mts_active = False
        self._mts_mode = 0
        self._mts_onoff = 0
        super().__init__(channel, parent, **kwargs)

        cls = self.__class__
        self.number_adjust_temperature = cls.AdjustNumber(
            channel, parent, ns=cls.AdjustNumber.init_ns
        )

        if cls.MTS_MODE_TO_TEMPERATUREKEY_MAP:
            self.number_preset_temperature = set(
                cls.SetPointNumber(
                    channel,
                    parent,
                    climate=self,
                    entity_key=f"config_temperature_{key_value}",
                    ns=self.ns,
                    key_value=key_value,
                    device_scale=self.temperature_scale,
                    native_max_value=self.max_temp,
                    native_min_value=self.min_temp,
                    native_step=self.target_temperature_step,
                    name=f"{preset} temperature",
                    icon=cls.SETPOINT_ICON_MAP[preset],
                )
                for preset, key_value in {
                    preset: cls.MTS_MODE_TO_TEMPERATUREKEY_MAP[
                        reverse_lookup(cls.MTS_MODE_TO_PRESET_MAP, preset)
                    ]
                    for preset in cls.SETPOINT_ICON_MAP
                }.items()
            )

        schedule_ns = cls.SCHEDULE_NS
        self.schedule = cls.Schedule(
            channel, parent, climate=self, ns=schedule_ns, entity_key=schedule_ns.key
        )
        parent.enable_check_device_time()  # useful for schedule entity times

        self.select_track_sensor = cls.TrackSensorSelect(channel, parent, climate=self)
        self.sensor_current_temperature = SensorParser.Temperature(
            channel, parent, entity_registry_enabled_default=False
        )
        for _entity in (self.number_adjust_temperature, self.schedule):
            parent.get_handler(_entity.ns).register_parser(_entity)

    def shutdown(self):
        super().shutdown()
        del self.sensor_current_temperature  # type: ignore
        del self.select_track_sensor  # type: ignore
        del self.schedule  # type: ignore
        del self.number_adjust_temperature  # type: ignore
        try:
            del self.number_preset_temperature  # type: ignore
        except AttributeError:
            pass

    def set_unavailable(self):
        self.current_humidity = None
        self.current_temperature = None
        self.preset_mode = None
        self.hvac_action = None
        self.hvac_mode = None
        super().set_unavailable()

    def flush_state(self):
        super().flush_state()
        self.schedule.flush_state()

    # interface: ClimateEntity
    async def async_turn_on(self):
        await self.async_request_onoff(1)

    async def async_turn_off(self):
        await self.async_request_onoff(0)

    async def async_set_hvac_mode(self, hvac_mode: climate.HVACMode):
        if hvac_mode == MtsClimate.HVACMode.OFF:
            await self.async_request_onoff(0)
            return
        await self.async_request_onoff(1)

    async def async_set_preset_mode(self, preset_mode: str):
        mode = reverse_lookup(self.MTS_MODE_TO_PRESET_MAP, preset_mode)
        if mode is not None:
            await self.async_request_preset(mode)

    async def async_set_temperature(self, **kwargs):
        raise NotImplementedError()

    # interface: self
    async def async_request_preset(self, mode: int, /):
        """Implements the protocol to set the Meross thermostat mode"""
        raise NotImplementedError()

    async def async_request_onoff(self, onoff: int, /):
        """Implements the protocol to turn on the thermostat"""
        raise NotImplementedError()

    def is_mts_scheduled(self, /):
        raise NotImplementedError()

    def _update_current_temperature(self, current_temperature: float | int, /):
        """
        Common handler for incoming room temperature value
        """
        current_temperature = current_temperature / self.temperature_scale
        if self.current_temperature != current_temperature:
            self.current_temperature = current_temperature
            self.sensor_current_temperature.update_native_value(current_temperature)
            self.select_track_sensor.check_tracking()
            # temp change might be an indication of a calibration so
            # we'll speed up polling for the adjust/calibration ns
            try:
                handler = self.number_adjust_temperature.handler_ns
                if handler.next_poll_epoch > (handler.parent.last_rx_epoch + 30):
                    handler.next_poll_epoch = 0.0
            except:
                # in case the ns is not available for this device
                pass

    async def async_request_parse_ex(self, payload: dict, /):
        """
        Issues a command to the main NS for this climate entity.
        This is typically the NS controlling the setpoints/modes.
        """
        await self.handler_ns.async_set_parse_ex(
            payload,
            self,
            self.ns_payload,
        )


async_setup_entry = MtsClimate.platform_setup_entry
