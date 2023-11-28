from __future__ import annotations

import typing

from homeassistant import const as hac
from homeassistant.components import select
from homeassistant.helpers.event import async_track_state_change_event

from .helpers import EntityManager, get_entity_last_state_available
from . import meross_entity as me
from .merossclient import const as mc  # mEROSS cONST

if typing.TYPE_CHECKING:
    from homeassistant.components.sensor import SensorEntity
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_component import EntityComponent
    from homeassistant.helpers.event import EventStateChangedData
    from homeassistant.helpers.typing import EventType

    from .devices.mod100 import DiffuserMixin
    from .climate import MtsClimate
    from .meross_device import MerossDevice, ResponseCallbackType


async def async_setup_entry(
    hass: HomeAssistant, config_entry: ConfigEntry, async_add_devices
):
    me.platform_setup_entry(hass, config_entry, async_add_devices, select.DOMAIN)


OPTION_SPRAY_MODE_OFF = "off"
OPTION_SPRAY_MODE_CONTINUOUS = "on"
OPTION_SPRAY_MODE_ECO = "eco"

"""
    This code is an alternative implementation for SPRAY/humidifier
    since the meross SPRAY doesnt support target humidity and
    the 'semantics' for HA humidifier are a bit odd for this device
    Also, bear in mind that, if select is not supported in HA core
    we're basically implementing a SwitchEntity
"""


class MLSpray(me.MerossEntity, select.SelectEntity):
    PLATFORM = select.DOMAIN

    manager: SprayMixin | DiffuserMixin

    _spray_mode_map: dict[object, str]
    """
    a dict containing mappings between meross modes <-> HA select options
    like { mc.SPRAY_MODE_OFF: OPTION_SPRAY_MODE_OFF }
    """

    __slots__ = (
        "_attr_options",
        "_spray_mode_map",
    )

    def __init__(
        self, manager: SprayMixin | DiffuserMixin, channel: object, spraymode_map: dict
    ):
        super().__init__(manager, channel, mc.KEY_SPRAY, mc.KEY_SPRAY)
        # we could use the shared instance but different device firmwares
        # could bring in unwanted global options...
        self._spray_mode_map = dict(spraymode_map)
        self._attr_options = list(self._spray_mode_map.values())

    @property
    def current_option(self):
        """Return the selected entity option to represent the entity state."""
        return self._attr_state

    async def async_select_option(self, option: str):
        # reverse lookup the dict
        for mode, _option in self._spray_mode_map.items():
            if _option == option:
                break
        else:
            raise NotImplementedError()

        def _ack_callback(acknowledge: bool, header: dict, payload: dict):
            if acknowledge:
                self.update_state(option)

        await self.manager.async_request_spray(
            {mc.KEY_CHANNEL: self.channel, mc.KEY_MODE: mode}, _ack_callback
        )

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
            self._attr_options = list(self._spray_mode_map.values())
        # we actually don't care if this is a SwitchEntity
        # this is a bug since state would be wrongly reported
        # when mode != on/off
        self.update_state(option)


class SprayMixin(
    MerossDevice if typing.TYPE_CHECKING else object
):  # pylint: disable=used-before-assignment
    SPRAY_MODE_MAP = {
        mc.SPRAY_MODE_OFF: OPTION_SPRAY_MODE_OFF,
        mc.SPRAY_MODE_INTERMITTENT: OPTION_SPRAY_MODE_ECO,
        mc.SPRAY_MODE_CONTINUOUS: OPTION_SPRAY_MODE_CONTINUOUS,
    }

    def _init_spray(self, payload: dict):
        # spray = [{"channel": 0, "mode": 0, "lmTime": 1629035486, "lastMode": 1, "onoffTime": 1629035486}]
        MLSpray(self, payload.get(mc.KEY_CHANNEL, 0), SprayMixin.SPRAY_MODE_MAP)

    def _handle_Appliance_Control_Spray(self, header: dict, payload: dict):
        self._parse_spray(payload.get(mc.KEY_SPRAY))

    def _parse_spray(self, payload):
        self._parse__generic(mc.KEY_SPRAY, payload, mc.KEY_SPRAY)

    async def async_request_spray(self, payload, callback: ResponseCallbackType):
        await self.async_request(
            mc.NS_APPLIANCE_CONTROL_SPRAY,
            mc.METHOD_SET,
            {mc.KEY_SPRAY: payload},
            callback,
        )


class MtsTrackedSensor(me.MerossEntity, select.SelectEntity):

    """
    A select entity used to select among all temperature sensors in HA
    an entity to track so that the thermostat regulates T against
    that other sensor. The idea is to track changes in
    the tracked entitites and adjust the MTS temp correction on the fly
    """

    PLATFORM = select.DOMAIN

    ATTR_TRACKED_STATE: typing.Final = "tracked_state"

    climate: MtsClimate
    _attr_entity_category = me.EntityCategory.CONFIG
    _attr_state: str | None

    __slots__ = (
        "climate",
        "_attr_options",
        "_tracked_state",
        "_untrack_state_callback",
    )

    def __init__(
        self,
        manager: EntityManager,
        channel: object | None,
        climate: MtsClimate,
    ):
        # BEWARE! the climate entity is not initialized so don't use it here
        self.climate = climate
        self._attr_options = []
        self._tracked_state = None
        self._untrack_state_callback = None
        super().__init__(manager, channel, "tracked_sensor", None)

    # interface: MerossEntity
    async def async_shutdown(self):
        self._tracked_state = None
        self.climate = None  # type: ignore
        await super().async_shutdown()

    @property
    def available(self):
        return True

    @property
    def extra_state_attributes(self):
        return {self.ATTR_TRACKED_STATE: self._tracked_state}

    def set_unavailable(self):
        pass

    async def async_added_to_hass(self):
        await super().async_added_to_hass()

        self._attr_options = [hac.STATE_UNKNOWN]
        component: EntityComponent[SensorEntity] = self.hass.data["sensor"]
        entities = list(component.entities)
        for entity in entities:
            um = entity.native_unit_of_measurement
            if um in (hac.TEMP_CELSIUS, hac.TEMP_FAHRENHEIT):
                self._attr_options.append(entity.entity_id)

        if self._attr_state is None:
            with self.exception_warning("restoring previous state"):
                if last_state := await get_entity_last_state_available(
                    self.hass, self.entity_id
                ):
                    self._attr_state = last_state.state

        self._start_tracking()

    async def async_will_remove_from_hass(self):
        self._stop_tracking()
        await super().async_will_remove_from_hass()

    # interface: SelectEntity
    @property
    def current_option(self):
        """Return the selected entity option to represent the entity state."""
        return self._attr_state

    async def async_select_option(self, option: str):
        self._stop_tracking()
        self.update_state(option)
        self._start_tracking()

    # interface: self
    @property
    def is_tracking(self):
        return bool(self._untrack_state_callback)

    def check_tracking(self):
        """
        called when either the climate or the tracked_entity has a new
        temperature reading in order to see if the climate needs to be adjusted
        """
        if not self._tracked_state or self._tracked_state.state in (
            hac.STATE_UNAVAILABLE,
            hac.STATE_UNKNOWN,
        ):
            return
        if not self.manager.online:
            return
        climate = self.climate
        with self.exception_warning("check_tracking"):
            tracked_temperature = float(self._tracked_state.state)
            """TODO: ensure tracked temperature is °C"""
            error_temperature: float = tracked_temperature - climate.current_temperature  # type: ignore
            native_error_temperature = error_temperature * mc.MTS_TEMP_SCALE
            if native_error_temperature:
                number_adjust_temperature = climate.number_adjust_temperature
                current_adjust_temperature = number_adjust_temperature.native_value
                if current_adjust_temperature is not None:
                    adjust_temperature = current_adjust_temperature + error_temperature
                    self.hass.async_create_task(
                        number_adjust_temperature.async_set_native_value(
                            adjust_temperature
                        )
                    )

    def _start_tracking(self):
        self._stop_tracking()
        entity_id = self._attr_state
        if entity_id and entity_id != hac.STATE_UNKNOWN:

            def _callback(event: EventType[EventStateChangedData]):
                with self.exception_warning("processing state update event"):
                    self._tracked_state = event.data.get("new_state")
                    self.check_tracking()

            self._untrack_state_callback = async_track_state_change_event(
                self.hass, entity_id, _callback
            )
            self._tracked_state = self.hass.states.get(entity_id)
            self.check_tracking()

    def _stop_tracking(self):
        if self._untrack_state_callback:
            self._untrack_state_callback()
            self._untrack_state_callback = None
            self._tracked_state = None
