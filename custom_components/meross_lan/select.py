from time import time
from typing import TYPE_CHECKING

from homeassistant import const as hac
from homeassistant.components import select, sensor
from homeassistant.core import CoreState, callback
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.util.unit_conversion import TemperatureConverter

from .helpers import entity as me, reverse_lookup

if TYPE_CHECKING:
    from typing import Any, ClassVar, Unpack

    from homeassistant.components.sensor import SensorEntity
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import Event, HomeAssistant, State
    from homeassistant.helpers.entity_component import EntityComponent
    from homeassistant.helpers.event import EventStateChangedData

    from .climate import MtsClimate
    from .helpers.device import BaseDevice


async def async_setup_entry(
    hass: "HomeAssistant", config_entry: "ConfigEntry", async_add_devices
):
    me.platform_setup_entry(hass, config_entry, async_add_devices, select.DOMAIN)


class MLSelect(me.MLEntity, select.SelectEntity):
    """Base 'abstract' class for both select entities representing a
    device config/option value (through MLConfigSelect) and
    emulated entities used to configure meross_lan (i.e. MtsTrackedSensor).
    Be sure to correctly init current_option and options in any derived class."""

    PLATFORM = select.DOMAIN

    if TYPE_CHECKING:
        # HA core entity attributes:
        current_option: str | None
        options: list[str]

    entity_category = me.MLEntity.EntityCategory.CONFIG

    __slots__ = (
        "current_option",
        "options",
    )

    def set_unavailable(self):
        self.current_option = None
        super().set_unavailable()

    def update_option(self, option: str):
        if self.current_option != option:
            self.current_option = option
            self.flush_state()


class MLConfigSelect(MLSelect):
    """
    Base class for any configurable 'list-like' parameter in the device.
    This works much-like MLConfigNumber but does not provide a default
    async_request_value so this needs to be defined in actual implementations.
    The mapping between HA entity select.options (string representation) and
    the native (likely int) device value is carried in a dedicated map
    (which also auto-updates should the device provide an unmapped value).
    """

    if TYPE_CHECKING:
        OPTIONS_MAP: ClassVar[dict[Any, str]]
        options_map: dict[Any, str]

    # configure initial options(map) through a class default
    OPTIONS_MAP = {}

    __slots__ = ("options_map",)

    def __init__(
        self,
        manager: "BaseDevice",
        channel: object | None,
        entitykey: str | None = None,
        **kwargs: "Unpack[MLSelect.Args]",
    ):
        self.current_option = None
        self.options_map = dict(
            self.OPTIONS_MAP
        )  # make a copy to not pollute when auto-updating
        self.options = list(self.options_map.values())
        super().__init__(manager, channel, entitykey, None, **kwargs)

    def update_device_value(self, device_value):
        try:
            self.update_option(self.options_map[device_value])
        except KeyError:
            self.options_map[device_value] = option = str(device_value)
            self.options.append(option)
            self.update_option(option)

    # interface: select.SelectEntity
    async def async_select_option(self, option: str):
        if await self.async_request_value(reverse_lookup(self.options_map, option)):
            self.update_option(option)


class MtsTrackedSensor(me.MEAlwaysAvailableMixin, MLSelect):
    """
    A select entity used to select among all temperature sensors in HA
    an entity to track so that the thermostat regulates T against
    that other sensor. The idea is to track changes in
    the tracked entitites and adjust the MTS temp correction on the fly
    """

    TRACKING_DEADTIME = 60
    """minimum delay (dead-time) between trying to adjust the climate entity"""

    climate: "MtsClimate"

    # HA core entity attributes:
    current_option: str
    entity_registry_enabled_default = False

    __slots__ = (
        "climate",
        "_delayed_tracking_timestamp",
        "_delayed_tracking_unsub",
        "_tracking_state",
        "_tracking_unsub",
    )

    def __init__(
        self,
        climate: "MtsClimate",
    ):
        self.current_option = hac.STATE_OFF
        self.options = []
        self.climate = climate
        self._delayed_tracking_timestamp = 0
        self._delayed_tracking_unsub = None
        self._tracking_state = None
        self._tracking_unsub = None
        super().__init__(climate.manager, climate.channel, "tracked_sensor")

    # interface: MLEntity
    async def async_shutdown(self):
        self._tracking_stop()
        await super().async_shutdown()
        self.climate = None  # type: ignore

    def set_unavailable(self):
        # reset the timeout and the eventual callback when the device
        # offlines so we promptly re-track when the device onlines again
        self._delayed_tracking_reset(0)

    async def async_added_to_hass(self):
        hass = self.hass

        if self.current_option is hac.STATE_OFF:
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
    async def async_select_option(self, option: str):
        self.update_option(option)
        self._tracking_start()

    # interface: self
    def check_tracking(self):
        """
        called when either the climate or the tracked_entity has a new
        temperature reading in order to see if the climate needs to be adjusted
        """
        if not self.manager.online or not self._tracking_unsub:
            return
        tracked_state = self._tracking_state
        if not tracked_state:
            # we've setup tracking but the entity doesn't exist in the
            # state machine...was it removed from HA ?
            self.log(
                self.WARNING,
                "Tracked entity state is missing: was it removed from HomeAssistant ?",
                timeout=14400,
            )
            return
        if tracked_state.state in (
            hac.STATE_UNAVAILABLE,
            hac.STATE_UNKNOWN,
        ):
            # might be transient so we don't take any action or log
            return
        epoch = time()
        delay = self._delayed_tracking_timestamp - epoch
        if delay > 0:
            # last tracking was too recent so we delay this a bit
            if not self._delayed_tracking_unsub:
                self._delayed_tracking_unsub = self.manager.schedule_callback(
                    delay, self._delayed_tracking_callback
                )
            return
        climate = self.climate
        with self.exception_warning("check_tracking", timeout=900):
            current_temperature = climate.current_temperature
            if not current_temperature:
                # should be transitory - just a safety check
                return
            number_adjust_temperature = climate.number_adjust_temperature
            current_adjust_temperature = number_adjust_temperature.native_value
            if current_adjust_temperature is None:
                # adjust entity not available (yet?) should be transitory - just a safety check
                return
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
            error_temperature: float = tracked_temperature - current_temperature
            native_error_temperature = round(error_temperature * climate.device_scale)
            if not native_error_temperature:
                # tracking error within device resolution limits..we're ok
                return
            adjust_temperature = current_adjust_temperature + error_temperature
            # check if our correction is within the native adjust limits
            # and avoid sending (useless) adjust commands
            if adjust_temperature > number_adjust_temperature.native_max_value:
                if (
                    current_adjust_temperature
                    >= number_adjust_temperature.native_max_value
                ):
                    return
                adjust_temperature = number_adjust_temperature.native_max_value
            elif adjust_temperature < number_adjust_temperature.native_min_value:
                if (
                    current_adjust_temperature
                    <= number_adjust_temperature.native_min_value
                ):
                    return
                adjust_temperature = number_adjust_temperature.native_min_value
            self._delayed_tracking_reset(epoch + self.TRACKING_DEADTIME)
            self.manager.async_create_task(
                number_adjust_temperature.async_set_native_value(adjust_temperature),
                f".async_set_native_value({adjust_temperature})",
            )
            self.log(
                self.DEBUG,
                "Applying correction of %s %s to %s",
                adjust_temperature,
                climate.temperature_unit,
                climate.entity_id,
            )

    @callback
    def _setup_tracking_entities(self, *_):
        _units = (hac.UnitOfTemperature.CELSIUS, hac.UnitOfTemperature.FAHRENHEIT)
        self.options = [
            entity.entity_id
            for entity in self.hass.data[sensor.DATA_COMPONENT].entities
            if getattr(entity, "native_unit_of_measurement", None) in _units
        ]
        self.options.append(hac.STATE_OFF)
        if self.current_option not in self.options:
            # this might happen when restoring a not anymore valid entity
            self.current_option = hac.STATE_OFF

        self.flush_state()
        self._tracking_start()

    def _tracking_start(self):
        self._tracking_stop()
        entity_id = self.current_option
        if entity_id and entity_id not in (
            hac.STATE_OFF,
            hac.STATE_UNKNOWN,
            hac.STATE_UNAVAILABLE,
        ):

            @callback
            def _tracking_callback(event: "Event[EventStateChangedData]"):
                with self.exception_warning("processing state update event"):
                    self._tracking_update(event.data.get("new_state"))

            self._tracking_unsub = async_track_state_change_event(
                self.hass, entity_id, _tracking_callback
            )
            self._tracking_update(self.hass.states.get(entity_id))

    def _tracking_stop(self):
        if self._tracking_unsub:
            self._tracking_unsub()
            self._tracking_unsub = None
            self._tracking_state = None
            self._delayed_tracking_reset(0)

    def _tracking_update(self, tracked_state: "State | None"):
        self._tracking_state = tracked_state
        self.check_tracking()

    @callback
    def _delayed_tracking_callback(self):
        self._delayed_tracking_unsub = None
        self.check_tracking()

    def _delayed_tracking_reset(self, delayed_tracking_timestamp):
        """
        cancels the delayed callback (if pending). This is called when either
        the tracking is fired (and a new deadtime is set) or when tracking
        is disabled for whatever reason (offlining, config change, ...)
        and prepares the state for eventually rescheduling the callback
        """
        self._delayed_tracking_timestamp = delayed_tracking_timestamp
        if self._delayed_tracking_unsub:
            self._delayed_tracking_unsub.cancel()
            self._delayed_tracking_unsub = None
