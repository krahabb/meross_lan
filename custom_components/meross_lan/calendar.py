from __future__ import annotations
import typing

from . import meross_entity as me
from .helpers import LOGGER

if typing.TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from homeassistant.config_entries import ConfigEntry

try:  # to look for calendar platform in HA core
    from homeassistant.components.calendar import (
        DOMAIN as PLATFORM_CALENDAR,
        CalendarEntity,
        CalendarEvent,  # type: ignore
        CalendarEntityFeature,  # type: ignore
        EVENT_DESCRIPTION,
        EVENT_END,
        EVENT_RECURRENCE_ID,
        EVENT_RECURRENCE_RANGE,
        EVENT_RRULE,
        EVENT_START,
        EVENT_SUMMARY,
        EVENT_UID,
    )

    async def async_setup_entry(
        hass: 'HomeAssistant', config_entry: 'ConfigEntry', async_add_devices
    ):
        me.platform_setup_entry(hass, config_entry, async_add_devices, PLATFORM_CALENDAR)

    async def async_unload_entry(hass: 'HomeAssistant', config_entry: 'ConfigEntry'):
        return me.platform_unload_entry(hass, config_entry, PLATFORM_CALENDAR)

except:  # implement a fallback by using a sensor
    LOGGER.warning(
        "Missing 'calendar' entity type. Please update HA to latest version"
        " to fully support thermostat schedule feature"
    )
    # we just mock some placeholder symbols hoping for the best...
    from homeassistant.components.sensor import (
        DOMAIN as PLATFORM_CALENDAR,
        SensorEntity as CalendarEntity,
    )
    from enum import IntEnum

    class CalendarEvent:
        pass

    class CalendarEntityFeature(IntEnum):
        CREATE_EVENT = 1
        DELETE_EVENT = 2

    # rfc5545 fields
    EVENT_UID = "uid"
    EVENT_START = "dtstart"
    EVENT_END = "dtend"
    EVENT_SUMMARY = "summary"
    EVENT_DESCRIPTION = "description"
    EVENT_LOCATION = "location"
    EVENT_RECURRENCE_ID = "recurrence_id"
    EVENT_RECURRENCE_RANGE = "recurrence_range"
    EVENT_RRULE = "rrule"


class MLCalendar(me.MerossEntity, CalendarEntity):  # type: ignore

    PLATFORM = PLATFORM_CALENDAR
