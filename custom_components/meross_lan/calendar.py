from __future__ import annotations

import typing

from . import meross_entity as me
from .helpers import LOGGER

if typing.TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

try:  # to look for calendar platform in HA core
    from homeassistant.components.calendar import (
        DOMAIN as PLATFORM_CALENDAR,
        EVENT_DESCRIPTION,
        EVENT_END,
        EVENT_RECURRENCE_ID,
        EVENT_RECURRENCE_RANGE,
        EVENT_RRULE,
        EVENT_START,
        EVENT_SUMMARY,
        EVENT_UID,
        CalendarEntity,
    )
    from homeassistant.components.calendar import CalendarEntityFeature  # type: ignore
    from homeassistant.components.calendar import CalendarEvent  # type: ignore

    async def async_setup_entry(
        hass: 'HomeAssistant', config_entry: 'ConfigEntry', async_add_devices
    ):
        me.platform_setup_entry(hass, config_entry, async_add_devices, PLATFORM_CALENDAR)

except:  # implement a fallback by using a sensor
    LOGGER.warning(
        "Missing 'calendar' entity type. Please update HA to latest version"
        " to fully support thermostat schedule feature"
    )
    # we just mock some placeholder symbols hoping for the best...
    from enum import IntEnum

    from homeassistant.components.sensor import (
        DOMAIN as PLATFORM_CALENDAR,
        SensorEntity as CalendarEntity,
    )

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
