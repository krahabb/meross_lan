"""
One-way sync of a HomeAssistant Schedule helper to the weekly schedule of Meross
thermostats (MtsClimate): the helper select (sync controller), its status sensor
and the pull/push buttons.
"""

import asyncio
import inspect
from typing import TYPE_CHECKING, override

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import callback
from homeassistant.exceptions import (
    HomeAssistantError,
    ServiceValidationError,
    Unauthorized,
    UnknownUser,
)
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.event import (
    async_track_state_added_domain,
    async_track_state_change_event,
    async_track_state_removed_domain,
)
from homeassistant.helpers.restore_state import RestoredExtraData
from homeassistant.helpers.start import async_at_started

from .button import Button
from .const import hac
from .helpers.schedule import (
    KEY_FROM,
    KEY_TO,
    MTS_SCHEDULE_WEEKDAY,
    ScheduleValidationError,
    compile_schedule,
    decompile_schedule,
    native_equal,
    native_same,
)
from .merossclient.protocol import const as mc, namespaces as mn
from .select import SelectEntity
from .sensor import EnumSensorEntity

if TYPE_CHECKING:
    from typing import Any, Callable, Iterable, Mapping

    from homeassistant.core import Context, Event, HomeAssistant
    from homeassistant.helpers.collection import DictStorageCollection

    from .climate import MtsClimate

SCHEDULE_DOMAIN = "schedule"
SCHEDULE_SERVICE_GET = "get_schedule"
READBACK_DELAY = 1
"""Delay (s) before querying a stale schedule again (see async_write_schedule)."""

_controllers: "set[ScheduleHelperSelect]" = set()
"""The ScheduleHelperSelect(s) added to HA (see _check_group)."""


class ScheduleWriteError(HomeAssistantError):
    """The schedule write request failed (not a device rejection): retried."""


class ScheduleSyncSensor(EnumSensorEntity):
    """Diagnostic sensor reporting the state of the schedule helper -> device sync."""

    if TYPE_CHECKING:
        native_value: str

    STATE_OFF = "off"
    STATE_PENDING = "pending"
    STATE_SYNCED = "synced"
    STATE_DRIFT = "drift"
    STATE_ERROR = "error"

    ATTR_HELPER = "helper"
    ATTR_REASON = "reason"
    ATTR_WARNINGS = "warnings"

    _INIT_entity_key = "schedule_sync"
    _INIT_native_value = STATE_OFF
    _INIT_extra_state_attributes = dict

    # HA core entity attributes:
    _attr_available = True
    _attr_entity_category = EnumSensorEntity.EntityCategory.DIAGNOSTIC
    _attr_icon = "mdi:calendar-sync"
    _unrecorded_attributes = frozenset((ATTR_WARNINGS, ATTR_REASON))

    options: list[str] = [
        STATE_OFF,
        STATE_PENDING,
        STATE_SYNCED,
        STATE_DRIFT,
        STATE_ERROR,
    ]

    _AUTO_INIT = ("extra_state_attributes",)
    __slots__ = _AUTO_INIT

    # interface: self
    def update_status(
        self,
        state: str,
        /,
        *,
        reason: str | None = None,
        warnings: "Iterable[str]" = (),
        helper: str | None = None,
    ):
        """Sets the sync status (empty attributes are removed) and flushes it when
        changed."""
        attrs = {
            key: value
            for key, value in (
                (self.ATTR_HELPER, helper),
                (self.ATTR_REASON, reason),
                (self.ATTR_WARNINGS, list(warnings)),
            )
            if value
        }
        if state != self.native_value or attrs != self.extra_state_attributes:
            self.native_value = state
            self.extra_state_attributes = attrs
            self.flush_state()


class ScheduleHelperSelect(SelectEntity.RestoreEntity, SelectEntity):
    """
    Lists the HA Schedule helpers to link one to the thermostat and syncs it: the
    linked helper is compiled and written to the device when selected and when it
    changes. A device schedule changed elsewhere is flagged (drift), not enforced.
    """

    if TYPE_CHECKING:
        climate: "MtsClimate"
        _sync_lock: asyncio.Lock
        _unsubs: list[Callable[[], None]]
        _helper_unsubs: list[Callable[[], None]]
        _sync_gen: int
        """Bumped when the link is stopped/restarted: a run bails out after any
        await when it changed (superseded)."""
        _last_helper_native: dict[str, Any] | None
        """The compiled helper last written (and confirmed) or adopted: the drift
        check reference (restored with the link). None until synced and while
        writing."""
        _last_warnings: tuple[str, ...]
        _ha_fahrenheit: bool | None
        """The HA unit (°F) of the last compile: helper values carry no unit."""
        _unit_hold: Mapping[str, Any] | None
        """The helper held after an HA unit change: synced once edited (or pushed)."""
        _last_inputs: tuple | None
        _adopt: bool
        """The next compiled helper becomes the reference without being written."""
        _retry_reason: str | None
        """Why the run was deferred to the retry timer (REASON_* or write error)."""
        _force_pending: bool
        _status: tuple[str, str | None]

    _INIT_entity_key = "schedule_helper"
    SYNC_DELAY = 2
    """Debounce delay before syncing after a helper change/(re)selection."""
    RETRY_DELAY = 60
    """Retry delay of a deferred run (see _retry_reason)."""
    REASON_OFFLINE = "device offline"
    REASON_UNKNOWN = "device schedule not yet known"
    REASON_WRITING = "writing the schedule to the device"
    REASON_CONFIRM = "waiting for device confirmation"
    REASON_DRIFT = "device schedule differs from the linked helper"
    REASON_HA_UNIT = (
        "Home Assistant unit system changed: edit the helper temperatures, or press "
        "'Sync schedule to device' to write them as they are"
    )

    # HA core entity attributes:
    _attr_available = True
    _attr_icon = "mdi:calendar-sync"

    _INIT__sync_gen = 0
    _INIT__last_warnings = ()
    _INIT__adopt = False
    _INIT__force_pending = False
    _INIT__status = (ScheduleSyncSensor.STATE_OFF, None)
    _AUTO_INIT = (
        "_sync_gen",
        "_last_helper_native",
        "_last_warnings",
        "_last_inputs",
        "_ha_fahrenheit",
        "_unit_hold",
        "_adopt",
        "_retry_reason",
        "_force_pending",
        "_status",
    )
    _AUTO_DESTROY = ("climate",)
    __slots__ = (
        "_sync_lock",
        "_unsubs",
        "_helper_unsubs",
        *_AUTO_INIT,
        *_AUTO_DESTROY,
    )

    def __init__(self, climate: "MtsClimate", /):
        self.climate = climate
        self._sync_lock = asyncio.Lock()
        self._unsubs = []
        self._helper_unsubs = []
        super().__init__(climate)
        # cleaned up on the schedule shutdown: _on_schedule_state guards itself
        climate.schedule.register_state_callback(self._on_schedule_state)

    @override
    async def async_shutdown(self):
        self._stop()
        await super().async_shutdown()

    @override
    async def async_added_to_hass(self):
        if not self.current_option and (
            restore_data := self._async_get_restored_data()
        ):
            self.current_option = restore_data.state.state
            if restore_data.extra_data:
                # the reference of the restored link (see _sync_start)
                data = restore_data.extra_data.as_dict()
                if isinstance(reference := data.get("reference"), dict):
                    self._last_helper_native = reference
                if isinstance(inputs := data.get("inputs"), (list, tuple)):
                    self._last_inputs = tuple(inputs)
                if isinstance(fahrenheit := data.get("fahrenheit"), bool):
                    self._ha_fahrenheit = fahrenheit
        # _setup_options runs now (HA running) or once HA started: before super
        # since it flushes the state (only wanted when called by the hass bus)
        self._unsubs.append(async_at_started(self.hass, self._setup_options))
        _controllers.add(self)
        await super().async_added_to_hass()

    @override
    async def async_will_remove_from_hass(self):
        self._stop()
        if self.parent.config_entry.state is ConfigEntryState.LOADED:
            # removed (e.g. disabled), not unloaded: no sync anymore
            self.climate.sensor_schedule_sync.update_status(
                ScheduleSyncSensor.STATE_OFF
            )
        await super().async_will_remove_from_hass()

    @property
    @override
    def extra_restore_state_data(self):
        return RestoredExtraData(
            {
                "reference": self._last_helper_native,
                "inputs": self._last_inputs,
                "fahrenheit": self._ha_fahrenheit,
            }
        )

    # interface: SelectEntity
    @override
    async def async_select_option(self, option: str):
        self.update_option(option)
        self._sync_start(True)

    # interface: self
    @property
    def linked_helper(self) -> str | None:
        """The entity_id of the linked schedule helper (None when not linked)."""
        option = self.current_option
        if option in (
            None,
            hac.STATE_OFF,
            hac.STATE_UNKNOWN,
            hac.STATE_UNAVAILABLE,
        ):
            return None
        return option

    async def async_sync(self, force: bool = False) -> tuple[str, str | None]:
        """Runs a (serialized) sync pass: the compiled linked helper is written when
        changed or, with force, anyway (overwriting drift). Returns the resulting
        status (state, reason)."""
        async with self._sync_lock:
            # a pending (debounced/retry) run is superseded by this one
            self.cancel_callback(self.async_sync)
            force = force or self._force_pending
            self._retry_reason = None
            self._force_pending = False
            gen = self._sync_gen
            try:
                await self._async_sync(gen, force)
                return self._status
            except (ScheduleValidationError, HomeAssistantError) as error:
                reason = str(error)
            except Exception as error:
                # programming error: logged, the status only points to the log
                self.log_exception(self.WARNING, error, "schedule sync")
                reason = "unexpected error (see log)"
            if not self._stale(gen):
                # (the log dedupe is by message and args: name the thermostat)
                self.log(
                    self.WARNING,
                    "Schedule sync failed for %s: %s",
                    self.climate.entity_id,
                    reason,
                    timeout=14400,
                )
                self._update_status(ScheduleSyncSensor.STATE_ERROR, reason=reason)
            return self._status

    async def async_write(self, native: "Mapping[str, Any]", /):
        """Writes a compiled schedule to the device (set_schedule service) serialized
        with the sync runs. set_schedule wins: the linked helper is then adopted
        (replacing any pending run) so that only its later edits are written."""
        async with self._sync_lock:
            gen = self._sync_gen
            await async_write_schedule(self.climate, native)
            if self.linked_helper and not self._stale(gen):
                self._force_pending = False
                self._adopt = True
                self.schedule_callback(0, self.async_sync)

    async def async_push(self):
        """'Sync schedule to device' (button, sync_schedule): writes the linked helper
        now, even when unchanged. Raises when not linked, not written (a deferred run
        is retried) or not kept."""
        if not self.hass_connected:
            raise HomeAssistantError("The schedule helper select is disabled")
        if not self.linked_helper:
            raise ServiceValidationError("No schedule helper linked to this thermostat")
        state, reason = await self.async_sync(force=True)
        if state == ScheduleSyncSensor.STATE_ERROR:
            raise HomeAssistantError(reason or "Schedule sync failed")
        if state == ScheduleSyncSensor.STATE_DRIFT:
            raise HomeAssistantError("The device did not keep the written schedule")
        if (
            state == ScheduleSyncSensor.STATE_PENDING
            and self._retry_reason != self.REASON_CONFIRM
        ):
            # deferred or superseded (link changed or entity removed meanwhile)
            raise HomeAssistantError(
                f"{reason or 'Schedule sync superseded'}, sync will be retried"
            )

    async def async_pull(self, context: "Context | None" = None, /):
        """'Sync schedule from device' (button): copies the device schedule into the
        linked helper (keeping its name and icon) or into a new one, then linked. Like
        helper editing in HA, reserved to admin users (or no user, e.g. automations).
        Raises HomeAssistantError."""
        if not self.hass_connected:
            raise HomeAssistantError("The schedule helper select is disabled")
        hass = self.hass
        if context and context.user_id:
            user = await hass.auth.async_get_user(context.user_id)
            if user is None:
                raise UnknownUser(context=context)
            if not user.is_admin:
                raise Unauthorized(context=context)
        storage = _helper_storage(hass)
        climate = self.climate
        schedule = climate.schedule
        async with self._sync_lock:
            if not climate.is_device_connected():
                raise HomeAssistantError("Device offline")
            try:
                await schedule.handler_ns.async_get(schedule.index)
            except Exception as error:
                raise HomeAssistantError(
                    f"unable to read the device schedule: {error}"
                ) from error
            # the helper storage format ("HH:MM:SS")
            days = {
                day: [
                    block
                    | {KEY_FROM: f"{block[KEY_FROM]}:00", KEY_TO: f"{block[KEY_TO]}:00"}
                    for block in blocks
                ]
                for day, blocks in helper_schedule(
                    climate, schedule_compile_kwargs(climate, hass)
                ).items()
            }
            if entity_id := self.linked_helper:
                entry = er.async_get(hass).async_get(entity_id)
                if not (entry and (item := storage.data.get(entry.unique_id))):
                    raise HomeAssistantError(
                        f"{entity_id} is not a helper created in the UI: "
                        "it can't be written"
                    )
                try:
                    self._check_group(entity_id, days)
                except ScheduleValidationError as error:
                    raise HomeAssistantError(str(error)) from error
                # the update replaces the whole item
                keep = {k: item[k] for k in (hac.CONF_NAME, hac.CONF_ICON) if k in item}
                await storage.async_update_item(item[hac.CONF_ID], keep | days)
                return
            device = self.device_entry
            name = f"{device.name_by_user or device.name} schedule"  # type: ignore
            item = await storage.async_create_item({hac.CONF_NAME: name, **days})
        # the helper entity is registered by now (the collection awaits its listeners)
        entity_id = er.async_get(hass).async_get_entity_id(
            SCHEDULE_DOMAIN, SCHEDULE_DOMAIN, item[hac.CONF_ID]
        )
        if not entity_id:
            raise HomeAssistantError(f"helper {item[hac.CONF_NAME]} not found")
        if self.hass_connected:  # not removed (e.g. disabled) meanwhile
            await self.async_select_option(entity_id)

    async def _async_read_helper(self, entity_id: str, /) -> "Mapping[str, Any]":
        """The helper schedule (schedule.get_schedule). Raises HomeAssistantError."""
        try:
            response = await self.hass.services.async_call(
                SCHEDULE_DOMAIN,
                SCHEDULE_SERVICE_GET,
                {hac.ATTR_ENTITY_ID: entity_id},
                blocking=True,
                return_response=True,
            )
            return response[entity_id]  # type: ignore[index]
        except (HomeAssistantError, KeyError, TypeError) as error:
            raise HomeAssistantError(f"unable to read {entity_id}: {error}") from error

    async def _async_sync(self, gen: int, force: bool, /):
        if self._stale(gen):
            return
        if not (entity_id := self.linked_helper):
            self._update_status(ScheduleSyncSensor.STATE_OFF)
            return
        # read first: helper errors are reported also while the device is offline
        ha_schedule = await self._async_read_helper(entity_id)
        if self._stale(gen):
            return
        climate = self.climate
        if not climate.is_device_connected():
            return self._retry(self.REASON_OFFLINE, force)
        if not climate.schedule_limits_known():
            return self._retry(self.REASON_UNKNOWN, force)

        kwargs = schedule_compile_kwargs(climate, self.hass)
        inputs = _compile_inputs(climate)
        if self._last_inputs and self._last_inputs[0] != inputs[0]:
            # device unit switched: every value moves to the new grid
            kwargs["keep"] = ()
        self._last_inputs = inputs
        fahrenheit = kwargs["fahrenheit"]
        if self._ha_fahrenheit not in (None, fahrenheit) and not force:
            # the helper values now read in the other unit: hold until edited
            if self._unit_hold in (None, ha_schedule):
                self._unit_hold = ha_schedule
                raise HomeAssistantError(self.REASON_HA_UNIT)
        self._ha_fahrenheit = fahrenheit
        self._unit_hold = None
        adopt, self._adopt = self._adopt, False
        result = compile_schedule(ha_schedule, **kwargs)
        native = result.native
        warnings = tuple(result.warnings)
        # on every pass: all the thermostats sharing the helper show the error
        self._check_group(entity_id, ha_schedule)

        reference = self._last_helper_native
        if not force and (
            adopt
            or native_same(native, climate.schedule.ns_value)
            or (reference and native_same(native, reference))
        ):
            # adopted, already on the device (entry by entry: moved borders are
            # written) or unchanged since the last sync: no write
            self._last_helper_native = native
            self._last_warnings = warnings
            self._check_device_state()
            return

        self._last_helper_native = None  # no drift checks while writing
        self._update_status(
            ScheduleSyncSensor.STATE_PENDING,
            reason=self.REASON_WRITING,
            warnings=warnings,
        )
        try:
            confirmed = await async_write_schedule(climate, native)
        except ScheduleWriteError as error:
            if not self._stale(gen):
                self._retry(str(error), force)
            return
        if self._stale(gen):
            return
        if not confirmed:
            # the retry writes again unless the device schedule matches by then
            return self._retry(self.REASON_CONFIRM)
        self._last_helper_native = native
        self._last_warnings = warnings
        self._check_device_state()

    def _check_group(self, entity_id: str, ha_schedule: "Mapping[str, Any]", /):
        """Shared helpers are all or nothing: raises ScheduleValidationError when the
        helper doesn't compile for another thermostat linked to it (those with unknown
        limits are skipped: they check it when they report)."""
        hass = self.hass
        for peer in _controllers:
            if peer is self or peer.linked_helper != entity_id:
                continue
            climate = peer.climate
            if not climate.schedule_limits_known():
                continue
            try:
                compile_schedule(ha_schedule, **schedule_compile_kwargs(climate, hass))
            except ScheduleValidationError as error:
                raise ScheduleValidationError(
                    error.code, f"does not fit {climate.entity_id}: {error}"
                ) from error

    def _stale(self, gen: int, /) -> bool:
        """True when the sync run started at 'gen' was superseded (link stopped or
        changed) or the entity was removed: the run must not touch any state."""
        return gen != self._sync_gen or not self.hass_connected

    def _retry(self, reason: str, force: bool = False, /):
        """Defers the run to the retry timer (status pending)."""
        self._retry_reason = reason
        self._force_pending = force
        self.schedule_callback(self.RETRY_DELAY, self.async_sync)
        self._update_status(ScheduleSyncSensor.STATE_PENDING, reason=reason)

    def _check_device_state(self):
        """Sets the status to synced or drift comparing (native_equal) the device
        schedule with the reference, when both are known."""
        reference = self._last_helper_native
        ns_value = self.climate.schedule.ns_value
        if not (reference and ns_value):
            return
        if native_equal(ns_value, reference):
            self._update_status(
                ScheduleSyncSensor.STATE_SYNCED, warnings=self._last_warnings
            )
        else:
            self._update_status(
                ScheduleSyncSensor.STATE_DRIFT,
                reason=self.REASON_DRIFT,
                warnings=self._last_warnings,
            )

    def _on_schedule_state(self):
        """Called on every schedule or climate flush: keep it cheap."""
        with self.exception_warning("schedule state check"):
            if not (self.hass_connected and self.linked_helper):
                return
            climate = self.climate
            ready = climate.is_device_connected() and climate.schedule_limits_known()
            if self._retry_reason in (self.REASON_OFFLINE, self.REASON_UNKNOWN):
                if ready:
                    self.schedule_callback(0, self.async_sync)
            elif self._retry_reason:
                # write unconfirmed or failed: left to the retry timer (a run now
                # would write again on every climate update)
                pass
            elif ready and self._last_inputs not in (None, _compile_inputs(climate)):
                # the run writes if the compiled helper changed
                self.schedule_callback(0, self.async_sync)
            elif self._status[0] != ScheduleSyncSensor.STATE_ERROR:
                # (an error is only cleared by a sync run)
                self._check_device_state()

    # link management
    def _build_options(self):
        """The schedule helpers in HA: a missing linked helper stays listed (and
        linked) so that the sync reports it and recovers when it comes back."""
        options = [
            hac.STATE_OFF,
            *sorted(self.hass.states.async_entity_ids(SCHEDULE_DOMAIN)),
        ]
        if (linked_helper := self.linked_helper) and (linked_helper not in options):
            options.append(linked_helper)
        return options

    @callback
    def _setup_options(self, *_):
        hass = self.hass
        # keep the options updated when schedule helpers are added/removed
        self._unsubs.append(
            async_track_state_added_domain(
                hass, SCHEDULE_DOMAIN, self._on_helpers_changed
            )
        )
        self._unsubs.append(
            async_track_state_removed_domain(
                hass, SCHEDULE_DOMAIN, self._on_helpers_changed
            )
        )
        self.options = self._build_options()
        if not self.linked_helper:
            self.current_option = hac.STATE_OFF
        self.flush_state()
        # restoring the link: keep the restored reference
        self._sync_start(False)

    @callback
    def _on_helpers_changed(self, event: "Event[Any]", /):
        """A schedule helper was added to/removed from HA: refresh the options."""
        if (options := self._build_options()) != self.options:
            self.options = options
            self.flush_state()

    def _update_status(self, state: str, /, **kwargs: "Any"):
        """Forwards a status update to the sync sensor adding the linked helper."""
        self._status = (state, kwargs.get("reason"))
        kwargs.setdefault("helper", self.linked_helper)
        self.climate.sensor_schedule_sync.update_status(state, **kwargs)

    def _sync_start(self, write: bool, /):
        """(Re)starts the sync of the linked helper. With write=False (link restored)
        a helper still compiling to the restored reference is not written; without a
        reference it is adopted."""
        self._sync_stop()
        if write:
            self._last_helper_native = None
            self._ha_fahrenheit = None
        self._unit_hold = None
        self._last_warnings = ()
        self._adopt = not (write or self._last_helper_native)
        if not (entity_id := self.linked_helper):
            self._update_status(ScheduleSyncSensor.STATE_OFF)
            return
        # HA core 2024.8 (the sync needs 2025.3 for schedule.get_schedule)
        from homeassistant.helpers.event import async_track_state_report_event

        self._helper_unsubs = [
            async_track_state_change_event(self.hass, entity_id, self._on_helper_event),
            async_track_state_report_event(self.hass, entity_id, self._on_helper_event),
        ]
        self._update_status(ScheduleSyncSensor.STATE_PENDING)
        self.schedule_callback(self.SYNC_DELAY, self.async_sync)

    def _sync_cancel(self):
        """Cancels the pending run and supersedes the one in progress (if any)."""
        self._sync_gen += 1
        self._retry_reason = None
        self._force_pending = False
        self.cancel_callback(self.async_sync)

    def _sync_stop(self):
        """Stops the sync controller: runs and helper subscriptions."""
        self._sync_cancel()
        for unsub in self._helper_unsubs:
            unsub()
        self._helper_unsubs = []

    def _stop(self):
        """Stops everything (entity removal/shutdown)."""
        _controllers.discard(self)
        self._sync_stop()
        for unsub in self._unsubs:
            unsub()
        self._unsubs = []

    @callback
    def _on_helper_event(self, event: "Event[Any]", /):
        if event.data["new_state"] is None:
            # keep the subscriptions: the helper might come back (e.g. YAML reload)
            self._sync_cancel()
            self._update_status(
                ScheduleSyncSensor.STATE_ERROR,
                reason=f"{self.linked_helper} was removed from Home Assistant",
            )
            return
        # debounced: every helper edit (and the helper own state transitions
        # at block boundaries) lands here
        self.schedule_callback(self.SYNC_DELAY, self.async_sync)


class SchedulePullButton(Button):
    """'Sync schedule from device' (see ScheduleHelperSelect.async_pull)."""

    _attr_entity_category = Button.EntityCategory.CONFIG
    _attr_icon = "mdi:calendar-import"

    __slots__ = ()

    def __init__(self, select: ScheduleHelperSelect, /):
        async def _async_press():
            # the press context (who pressed it) for the admin check
            await select.async_pull(self._context)

        super().__init__(
            select.climate, entity_key="schedule_pull", async_press=_async_press
        )


class SchedulePushButton(Button):
    """'Sync schedule to device' (see ScheduleHelperSelect.async_push)."""

    _attr_entity_category = Button.EntityCategory.CONFIG
    _attr_icon = "mdi:calendar-export"

    __slots__ = ()

    def __init__(self, select: ScheduleHelperSelect, /):
        super().__init__(
            select.climate, entity_key="schedule_push", async_press=select.async_push
        )


def schedule_device_fahrenheit(climate: "MtsClimate", /) -> bool | None:
    """True when the thermostat displays °F (Appliance.Control.TempUnit), None
    while not known. Devices without it (e.g. hub valves) are °C only."""
    try:
        handler = climate.parent.ns_handlers[mn.Appliance_Control_TempUnit]
        unit = handler.parsers[climate.index].ns_value  # type: ignore
    except (KeyError, AttributeError):
        return False
    return None if unit is None else unit == mc.TEMPUNIT_FAHRENHEIT


def _compile_inputs(climate: "MtsClimate", /) -> tuple:
    """The device state feeding the compile besides its schedule and limits."""
    return (
        schedule_device_fahrenheit(climate),
        climate.min_temp,
        climate.max_temp,
        *climate.schedule_off_values(),
    )


def schedule_compile_kwargs(
    climate: "MtsClimate", hass: "HomeAssistant", /
) -> "dict[str, Any]":
    """The compile_schedule kwargs of the climate in the HA unit system, keeping
    the values the device holds or held at the last sync."""
    fahrenheit = hass.config.units.temperature_unit == hac.UnitOfTemperature.FAHRENHEIT
    keep = {
        value
        for native in (
            climate.schedule.ns_value,
            climate.select_schedule_helper._last_helper_native or {},
        )
        for day in MTS_SCHEDULE_WEEKDAY
        for entry in native.get(day, ())
        for value in entry[1:]
    }
    return {
        "scale": climate.temperature_scale,
        "min_temp": climate.min_temp,
        "max_temp": climate.max_temp,
        "keys": climate.SCHEDULE_KEYS,
        "fahrenheit": fahrenheit,
        "device_fahrenheit": bool(schedule_device_fahrenheit(climate)),
        "off_values": climate.schedule_off_values(),
        "keep": keep,
        **climate.schedule.limits,
    }


def helper_schedule(
    climate: "MtsClimate", kwargs: "Mapping[str, Any]", /
) -> "dict[str, list[dict[str, Any]]]":
    """The device schedule in the helper format, off entries as gaps (it compiles
    back to it). Raises HomeAssistantError when not known or not supported."""
    if not (ns_value := climate.schedule.ns_value):
        raise HomeAssistantError("Schedule not available yet")
    try:
        return decompile_schedule(
            ns_value,
            scale=kwargs["scale"],
            keys=kwargs["keys"],
            fahrenheit=kwargs["fahrenheit"],
            device_fahrenheit=kwargs["device_fahrenheit"],
            off_values=kwargs["off_values"],
        )
    except ScheduleValidationError as error:
        # device data, not user input
        raise HomeAssistantError(f"Unsupported device schedule: {error}") from error


def _helper_storage(hass: "HomeAssistant", /) -> "DictStorageCollection":
    """The storage collection of the UI schedule helpers. HA has no public API to
    write helpers: it is reached through the schedule/create websocket command."""
    try:
        handler = hass.data["websocket_api"]["schedule/create"][0]
        return inspect.unwrap(handler).__self__.storage_collection
    except (KeyError, AttributeError, TypeError) as error:
        raise HomeAssistantError(
            "schedule helpers can't be written (schedule integration not loaded "
            "or unsupported Home Assistant version)"
        ) from error


async def async_write_schedule(
    climate: "MtsClimate", native: "Mapping[str, Any]", /
) -> bool:
    """Writes a compiled schedule and queries it back. Raises ScheduleWriteError
    when the request fails, HomeAssistantError when the device rejects it. Returns
    False when only the confirmation query failed."""
    schedule = climate.schedule
    payload = dict(native)
    if (pre := schedule.ns_value.get("pre")) is not None:
        # mts300: echo its 'pre' block unchanged (unconfirmed whether needed)
        payload["pre"] = pre
    try:
        response = await schedule.async_request_payload(payload)
    except Exception as error:
        raise ScheduleWriteError(f"device write failed: {error}") from error
    if response and (response.method == mc.METHOD_ERROR):
        raise HomeAssistantError(
            f"device rejected the schedule: {response.payload.get(mc.KEY_ERROR)}"
        )
    # Should the device (or the hub for its valves) still report the old schedule,
    # it is queried once more a bit later (else drift until its next update).
    try:
        await schedule.handler_ns.async_get(schedule.index)
        if not native_same(schedule.ns_value, native):
            await asyncio.sleep(READBACK_DELAY)
            await schedule.handler_ns.async_get(schedule.index)
    except Exception as error:
        climate.log(
            climate.WARNING,
            "Unable to query the device schedule after writing it (%s)",
            error,
        )
        return False
    return True
