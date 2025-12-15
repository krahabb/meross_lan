import enum
from time import time
from typing import TYPE_CHECKING

from homeassistant.components import climate, sensor
from homeassistant.core import CoreState, callback
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.util.unit_conversion import TemperatureConverter

from .helpers import entity as me, reverse_lookup
from .merossclient.protocol import const as mc
from .number import MLConfigNumber
from .select import MLSelect
from .sensor import MLTemperatureSensor

if TYPE_CHECKING:
    from typing import ClassVar, Final, Unpack

    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import Event, HomeAssistant, State
    from homeassistant.helpers.event import EventStateChangedData

    from .calendar import MtsSchedule
    from .helpers.device import BaseDevice, Device
    from .helpers.namespaces import NamespaceHandler


async def async_setup_entry(
    hass: "HomeAssistant", config_entry: "ConfigEntry", async_add_devices
):
    me.platform_setup_entry(hass, config_entry, async_add_devices, climate.DOMAIN)


class MtsClimate(me.MLEntity, climate.ClimateEntity):

    class Preset(enum.StrEnum):
        CUSTOM = "custom"
        COMFORT = "comfort"
        SLEEP = "sleep"
        AWAY = "away"
        AUTO = "auto"

    class TrackSensorSelect(me.MEAlwaysAvailableMixin, MLSelect):
        """
        TODO: move to climate.py ?
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
            # HA core entity attributes:
            current_option: str

        TRACKING_DELAY = 5
        TRACKING_DEADTIME = 60

        # HA core entity attributes:
        entity_registry_enabled_default = False

        __slots__ = (
            "climate",
            "_tracking_state",
            "_tracking_state_change_unsub",
            "_track_last_epoch",
            "_track_unsub",
        )

        def __init__(
            self,
            climate: "MtsClimate",
        ):
            self.current_option = MLSelect.hac.STATE_OFF
            self.options = []
            self.climate = climate
            self._tracking_state = None
            self._tracking_state_change_unsub = None
            self._track_last_epoch = 0
            self._track_unsub = None
            super().__init__(climate.manager, climate.channel, "tracked_sensor")

        # interface: MLEntity
        async def async_shutdown(self):
            self._tracking_stop()
            await super().async_shutdown()
            self.climate = None  # type: ignore

        def set_unavailable(self):
            if self._track_unsub:
                self._track_unsub.cancel()
                self._track_unsub = None

        async def async_added_to_hass(self):
            hass = self.hass

            if self.current_option is MLSelect.hac.STATE_OFF:
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
                self.options = [self.current_option]
                hass.bus.async_listen_once(
                    MLSelect.hac.EVENT_HOMEASSISTANT_STARTED,
                    self._setup_tracking_entities,
                )

            # call super after (eventually) calling _setup_tracking_entities since it
            # could flush the new state (it should only when called by the hass bus)
            await super().async_added_to_hass()

        async def async_will_remove_from_hass(self):
            self._tracking_stop()
            await super().async_will_remove_from_hass()

        # interface: SelectEntity
        async def async_select_option(self, option: str):
            self.update_option(option)
            self._tracking_start()

        # interface: self
        def check_tracking(self):
            """
            called when either the climate or the tracked_entity has a new
            temperature reading in order to see if the climate needs to be adjusted
            """
            if self._track_unsub:
                self._track_unsub.cancel()
                self._track_unsub = None

            if not self.manager.online or not self._tracking_state_change_unsub:
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
                MLSelect.hac.STATE_UNAVAILABLE,
                MLSelect.hac.STATE_UNKNOWN,
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
            delay = time() - self._track_last_epoch
            self._track_unsub = self.manager.schedule_callback(
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
                MLSelect.hac.UnitOfTemperature.CELSIUS,
                MLSelect.hac.UnitOfTemperature.FAHRENHEIT,
            )
            self.options = [
                entity.entity_id
                for entity in self.hass.data[sensor.DATA_COMPONENT].entities
                if getattr(entity, "native_unit_of_measurement", None) in _units
            ]
            self.options.append(MLSelect.hac.STATE_OFF)
            if self.current_option not in self.options:
                # this might happen when restoring a not anymore valid entity
                self.current_option = MLSelect.hac.STATE_OFF

            self.flush_state()
            self._tracking_start()

        def _tracking_start(self):
            self._tracking_stop()
            entity_id = self.current_option
            if entity_id and entity_id not in (
                MLSelect.hac.STATE_OFF,
                MLSelect.hac.STATE_UNKNOWN,
                MLSelect.hac.STATE_UNAVAILABLE,
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
            if self._track_unsub:
                self._track_unsub.cancel()
                self._track_unsub = None

        def _tracking_state_change(self, tracked_state: "State | None"):
            self._tracking_state = tracked_state
            self.check_tracking()

        def _track(self, tracked_state: "State"):
            """This is only called internally after a timeout when tracking needs to be updated
            due to state changes in either tracked entity or climate."""
            self._track_unsub = None
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
                    MtsClimate.hac.ATTR_UNIT_OF_MEASUREMENT
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
                    error_temperature * climate.device_scale
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
                self._track_last_epoch = time()
                self.manager.async_create_task(
                    number_adjust_temperature.async_set_native_value(
                        adjust_temperature
                    ),
                    f"MtsTrackedSensor._track(adjust_temperature={adjust_temperature} [{climate.temperature_unit}])",
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

        device_scale: ClassVar[float]
        AdjustNumber: ClassVar[type["MLConfigNumber"]]
        """The specific Adjust/Calibrate number class to instantiate."""
        SetPointNumber: ClassVar[type["MtsSetPointNumber"] | None]
        """The (optional) class for setting up a group of preset setpoints."""
        Schedule: ClassVar[type[MtsSchedule]]
        """The specific Schedule/Calendar class to instantiate."""

        MTS_MODE_TO_PRESET_MAP: ClassVar[dict[int | None, str]]
        """maps device 'mode' value to the HA climate.preset_mode"""
        MTS_MODE_TO_TEMPERATUREKEY_MAP: ClassVar[dict[int | None, str]]
        """maps the current mts mode to the name of temperature setpoint key"""
        PRESET_TO_ICON_MAP: Final[dict[Preset, str]]
        """Used in Number entities for temperatues setpoint."""
        SET_TEMP_FORCE_MANUAL_MODE: Final[bool]
        """Determines the behavior of async_set_temperature."""
        manager: BaseDevice
        number_adjust_temperature: Final["MLConfigNumber"]
        number_preset_temperature: dict[str, "MtsSetPointNumber"]
        schedule: Final[MtsSchedule]
        select_track_sensor: Final[TrackSensorSelect]
        sensor_current_temperature: Final[MLTemperatureSensor]
        _mts_active: bool
        _mts_mode: int
        _mts_onoff: int
        _mts_payload: dict

        # HA core entity attributes override:
        _attr_preset_modes: list[str]
        _attr_supported_features: climate.ClimateEntityFeature
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
        translation_key: Final[str]

    PLATFORM = climate.DOMAIN

    ATTR_HVAC_MODE = climate.ATTR_HVAC_MODE
    ATTR_TEMPERATURE = climate.ATTR_TEMPERATURE
    ATTR_TARGET_TEMP_HIGH = climate.ATTR_TARGET_TEMP_HIGH
    ATTR_TARGET_TEMP_LOW = climate.ATTR_TARGET_TEMP_LOW

    ClimateEntityFeature = climate.ClimateEntityFeature
    HVACAction = climate.HVACAction
    HVACMode = climate.HVACMode

    device_scale = 1

    SetPointNumber = None

    PRESET_TO_ICON_MAP = {
        Preset.COMFORT: "mdi:sun-thermometer",
        Preset.SLEEP: "mdi:power-sleep",
        Preset.AWAY: "mdi:bag-checked",
    }
    """lookups used in MtsSetpointNumber to map a pretty icon to the setpoint entity"""

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

    translation_key = "mts_climate"

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
        "_mts_payload",
        "number_adjust_temperature",
        "number_preset_temperature",
        "schedule",
        "select_track_sensor",
        "sensor_current_temperature",
    )

    def __init__(self, manager: "BaseDevice", channel: object, /):
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
        self.target_temperature_step = 0.5
        self.temperature_unit = self.hac.UnitOfTemperature.CELSIUS
        self._mts_active = False
        self._mts_mode = 0
        self._mts_onoff = 0
        self._mts_payload = {}
        super().__init__(manager, channel)
        self.number_adjust_temperature = self.__class__.AdjustNumber(self)  # type: ignore
        self.number_preset_temperature = {}
        if preset_number_class := self.__class__.SetPointNumber:
            for preset in MtsClimate.PRESET_TO_ICON_MAP.keys():
                number_preset_temperature = preset_number_class(self, preset)
                self.number_preset_temperature[number_preset_temperature.key_value] = (
                    number_preset_temperature
                )
        self.schedule = self.__class__.Schedule(self)
        self.select_track_sensor = MtsClimate.TrackSensorSelect(self)
        self.sensor_current_temperature = MLTemperatureSensor(manager, channel)
        self.sensor_current_temperature.entity_registry_enabled_default = False

    # interface: MLEntity
    async def async_shutdown(self):
        await super().async_shutdown()
        self.sensor_current_temperature = None  # type: ignore
        self.select_track_sensor = None  # type: ignore
        self.schedule = None  # type: ignore
        self.number_adjust_temperature = None  # type: ignore
        self.number_preset_temperature = None  # type: ignore

    def set_unavailable(self):
        self._mts_payload.clear()
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
        raise NotImplementedError()

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

    def get_ns_adjust(self, /) -> "NamespaceHandler":
        """
        Returns the correct ns handler for the adjust namespace.
        Used to trigger a poll and the ns which is by default polled
        on a long timeout.
        """
        raise NotImplementedError()

    def _update_current_temperature(self, current_temperature: float | int, /):
        """
        Common handler for incoming room temperature value
        """
        current_temperature = current_temperature / self.device_scale
        if self.current_temperature != current_temperature:
            self.current_temperature = current_temperature
            self.sensor_current_temperature.update_native_value(current_temperature)
            self.select_track_sensor.check_tracking()
            # temp change might be an indication of a calibration so
            # we'll speed up polling for the adjust/calibration ns
            try:
                ns_adjust = self.get_ns_adjust()
                if ns_adjust.polling_epoch_next > (ns_adjust.device.lastresponse + 30):
                    ns_adjust.polling_epoch_next = 0.0
            except:
                # in case the ns is not available for this device
                pass


class MtsSetPointNumber(MLConfigNumber):
    """
    Helper entity to configure MTS100/150/200 setpoints
    AKA: Heat(comfort) - Cool(sleep) - Eco(away)
    """

    __slots__ = (
        "climate",
        "icon",
        "key_value",
    )

    def __init__(self, climate: "MtsClimate", preset_mode: "MtsClimate.Preset", /):
        self.climate = climate
        self.icon = climate.PRESET_TO_ICON_MAP[preset_mode]
        self.key_value = climate.MTS_MODE_TO_TEMPERATUREKEY_MAP[
            reverse_lookup(climate.MTS_MODE_TO_PRESET_MAP, preset_mode)
        ]
        super().__init__(
            climate.manager,
            climate.channel,
            f"config_temperature_{self.key_value}",
            MLConfigNumber.DeviceClass.TEMPERATURE,
            name=f"{preset_mode} temperature",
            device_scale=climate.device_scale,
        )

    @property
    def native_max_value(self):
        return self.climate.max_temp

    @property
    def native_min_value(self):
        return self.climate.min_temp

    @property
    def native_step(self):
        return self.climate.target_temperature_step

    async def async_request_value(self, device_value, /):
        if response := await super().async_request_value(device_value):
            # mts100(s) reply to the setack with the 'full' (or anyway richer) payload
            # so we'll use the _parse_temperature logic (a bit overkill sometimes) to
            # make sure the climate state is consistent and all the correct roundings
            # are processed when changing any of the presets
            # not sure about mts200 replies..but we're optimist
            ns_slug_end = self.ns.slug_end
            payload = response[mc.KEY_PAYLOAD]
            if ns_slug_end in payload:
                # by design ns_slug is either "temperature" (mts100) or "mode" (mts200)
                getattr(self.climate, f"_parse_{ns_slug_end}")(payload[ns_slug_end][0])

        return response
