from __future__ import annotations

from time import time
import typing

from homeassistant import const as hac
from homeassistant.components import select
from homeassistant.core import CoreState, callback
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.util.unit_conversion import TemperatureConverter

from . import meross_entity as me
from .helpers import get_entity_last_state_available, schedule_callback
from .merossclient import const as mc  # mEROSS cONST

if typing.TYPE_CHECKING:

    from homeassistant.components.sensor import SensorEntity
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant, State
    from homeassistant.helpers.entity_component import EntityComponent
    from homeassistant.helpers.event import EventStateChangedData
    from homeassistant.helpers.typing import EventType

    from .climate import MtsClimate
    from .devices.mod100 import DiffuserMixin
    from .meross_device import MerossDevice


async def async_setup_entry(
    hass: HomeAssistant, config_entry: ConfigEntry, async_add_devices
):
    me.platform_setup_entry(hass, config_entry, async_add_devices, select.DOMAIN)


OPTION_SPRAY_MODE_OFF = "off"
OPTION_SPRAY_MODE_CONTINUOUS = "on"
OPTION_SPRAY_MODE_ECO = "eco"


class MLSelect(me.MerossEntity, select.SelectEntity):
    PLATFORM = select.DOMAIN

    # HA core entity attributes:
    current_option: str | None
    options: list[str]

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


class MLSpray(MLSelect):

    manager: SprayMixin | DiffuserMixin

    _spray_mode_map: dict[object, str]
    """
    a dict containing mappings between meross modes <-> HA select options
    like { mc.SPRAY_MODE_OFF: OPTION_SPRAY_MODE_OFF }
    """
    # HA core entity attributes:

    __slots__ = ("_spray_mode_map",)

    def __init__(
        self, manager: SprayMixin | DiffuserMixin, channel: object, spraymode_map: dict
    ):
        # we could use the shared instance but different device firmwares
        # could bring in unwanted global options...
        self._spray_mode_map = dict(spraymode_map)
        self.current_option = None
        self.options = list(self._spray_mode_map.values())
        super().__init__(manager, channel, mc.KEY_SPRAY)

    # interface: select.SelectEntity
    async def async_select_option(self, option: str):
        # reverse lookup the dict
        for mode, _option in self._spray_mode_map.items():
            if _option == option:
                break
        else:
            raise NotImplementedError("async_select_option")

        if await self.manager.async_request_spray_ack(
            {mc.KEY_CHANNEL: self.channel, mc.KEY_MODE: mode}
        ):
            self.update_option(option)

    def _parse_spray(self, payload: dict):
        """
        We'll map the mode key to a well-known option for this entity
        but, since there could be some additions from newer spray devices
        we'll also eventually add the unknown mode value as a supported mode
        Keep in mind we're updating a class instance dict so it should affect
        all of the same-class-entities
        """
        mode = payload[mc.KEY_MODE]
        option = self._spray_mode_map.get(mode)
        if option is None:
            # unknown mode value -> auto-learning
            option = "mode_" + str(mode)
            self._spray_mode_map[mode] = option
            self.options = list(self._spray_mode_map.values())
        self.update_option(option)


class SprayMixin(
    MerossDevice if typing.TYPE_CHECKING else object
):  # pylint: disable=used-before-assignment
    SPRAY_MODE_MAP = {
        mc.SPRAY_MODE_OFF: OPTION_SPRAY_MODE_OFF,
        mc.SPRAY_MODE_INTERMITTENT: OPTION_SPRAY_MODE_ECO,
        mc.SPRAY_MODE_CONTINUOUS: OPTION_SPRAY_MODE_CONTINUOUS,
    }

    def _init_spray(self, digest: list):
        # spray = [{"channel": 0, "mode": 0, "lmTime": 1629035486, "lastMode": 1, "onoffTime": 1629035486}]
        for channel_digest in digest:
            spray = MLSpray(self, channel_digest[mc.KEY_CHANNEL], self.SPRAY_MODE_MAP)
            self.register_parser(mc.NS_APPLIANCE_CONTROL_SPRAY, spray)

    def _parse_spray(self, digest: list):
        self.namespace_handlers[mc.NS_APPLIANCE_CONTROL_SPRAY]._parse_list(digest)

    async def async_request_spray_ack(self, payload):
        return await self.async_request_ack(
            mc.NS_APPLIANCE_CONTROL_SPRAY,
            mc.METHOD_SET,
            {mc.KEY_SPRAY: payload},
        )


class MtsTrackedSensor(MLSelect):
    """
    A select entity used to select among all temperature sensors in HA
    an entity to track so that the thermostat regulates T against
    that other sensor. The idea is to track changes in
    the tracked entitites and adjust the MTS temp correction on the fly
    """

    TRACKING_DEADTIME = 60
    """minimum delay (dead-time) between trying to adjust the climate entity"""

    climate: MtsClimate

    # HA core entity attributes:
    _attr_available = True
    current_option: str
    entity_category = me.EntityCategory.CONFIG
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
        climate: MtsClimate,
    ):
        self.current_option = hac.STATE_OFF
        self.options = []
        self.climate = climate
        self._delayed_tracking_timestamp = 0
        self._delayed_tracking_unsub = None
        self._tracking_state = None
        self._tracking_unsub = None
        super().__init__(climate.manager, climate.channel, "tracked_sensor")

    # interface: MerossEntity
    async def async_shutdown(self):
        self._tracking_stop()
        await super().async_shutdown()
        self.climate = None  # type: ignore

    def set_available(self):
        pass

    def set_unavailable(self):
        # reset the timeout and the eventual callback when the device
        # offlines so we promptly re-track when the device onlines again
        self._delayed_tracking_reset(0)

    async def async_added_to_hass(self):
        hass = self.hass

        if self.current_option is hac.STATE_OFF:
            with self.exception_warning("restoring previous state"):
                if last_state := await get_entity_last_state_available(
                    hass, self.entity_id
                ):
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
                self._delayed_tracking_unsub = schedule_callback(
                    self.hass, delay, self._delayed_tracking_callback
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
            if tracked_temperature_unit != climate.TEMP_CELSIUS:
                tracked_temperature = TemperatureConverter.convert(
                    tracked_temperature,
                    tracked_temperature_unit,
                    climate.TEMP_CELSIUS,
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
            self.hass.async_create_task(
                number_adjust_temperature.async_set_native_value(adjust_temperature)
            )
            self.log(
                self.DEBUG,
                "Applying correction of %s %s to %s",
                adjust_temperature,
                climate.TEMP_CELSIUS,
                climate.entity_id,
            )

    @callback
    def _setup_tracking_entities(self, *_):
        self.options = [hac.STATE_OFF]
        component: EntityComponent[SensorEntity] = self.hass.data["sensor"]
        for entity in component.entities:
            um = entity.native_unit_of_measurement
            if um in (hac.UnitOfTemperature.CELSIUS, hac.UnitOfTemperature.FAHRENHEIT):
                self.options.append(entity.entity_id)

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
            def _tracking_callback(event: EventType[EventStateChangedData]):
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

    def _tracking_update(self, tracked_state: State | None):
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
