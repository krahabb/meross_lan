"""
Entity services exposing the on-device weekly schedule of Meross thermostats
(MtsClimate) in the HomeAssistant Schedule helper format:
- meross_lan.get_schedule: returns the device schedule (helper format) and its limits
- meross_lan.set_schedule: compiles a helper-formatted schedule, writes it to the device
  and returns the compile warnings
- meross_lan.sync_schedule: (re)syncs the linked Schedule helper now (see schedule_sync.py)

They target the climate entities of this integration and are registered (once)
by ComponentApi. HA skips unavailable (offline) entities: set_schedule and
sync_schedule do nothing on them while get_schedule fails (no response).
"""

from typing import TYPE_CHECKING

from homeassistant.const import Platform
from homeassistant.core import SupportsResponse
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.util.unit_conversion import TemperatureConverter

try:  # HA core 2025.10
    from homeassistant.helpers.service import async_register_platform_entity_service
except ImportError:
    async_register_platform_entity_service = None

try:
    # HA core 2026.9 introduced probatio in place of voluptuous
    import probatio as vol
except ImportError:
    import voluptuous as vol

from . import const as mlc
from .const import hac
from .helpers.schedule import (
    HA_SCHEDULE_WEEKDAY,
    KEY_DATA,
    KEY_FROM,
    KEY_TO,
    ScheduleValidationError,
    compile_schedule,
)
from .schedule_sync import helper_schedule, schedule_compile_kwargs

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant, ServiceCall, ServiceResponse

    from .climate import MtsClimate
    from .helpers.component_api import ComponentApi


ATTR_SCHEDULE = "schedule"

_BLOCK_SCHEMA = {
    vol.Required(KEY_FROM): cv.string,
    vol.Required(KEY_TO): cv.string,
    vol.Optional(KEY_DATA): dict,
}
# a missing day is off (see compile_schedule)
SET_SCHEDULE_SCHEMA = {
    vol.Required(ATTR_SCHEDULE): {
        vol.Optional(day): [_BLOCK_SCHEMA] for day in HA_SCHEDULE_WEEKDAY
    }
}


async def async_service_get_schedule(
    entity: "MtsClimate", call: "ServiceCall", /
) -> "ServiceResponse":
    if not entity.schedule_limits_known():  # e.g. the device unit
        raise HomeAssistantError("Schedule not available yet")
    kwargs = schedule_compile_kwargs(entity, entity.hass)
    celsius = hac.UnitOfTemperature.CELSIUS
    fahrenheit = hac.UnitOfTemperature.FAHRENHEIT
    ha_unit = fahrenheit if kwargs["fahrenheit"] else celsius
    limits = {
        # in the HA unit (as the schedule values)
        "min_temp": round(
            TemperatureConverter.convert(kwargs["min_temp"], celsius, ha_unit), 2
        ),
        "max_temp": round(
            TemperatureConverter.convert(kwargs["max_temp"], celsius, ha_unit), 2
        ),
        # the device grid the values are rounded to
        "device_unit": fahrenheit if kwargs["device_fahrenheit"] else celsius,
        "keys": list(kwargs["keys"]),
        "unit_time": kwargs["unit_time"],
        "entry_count_max": kwargs["entry_count_max"],
    }
    select = entity.select_schedule_helper
    return {
        ATTR_SCHEDULE: helper_schedule(entity, kwargs),  # type: ignore[dict-item]
        "limits": limits,
        # a select removed from HA (e.g. disabled) keeps its option but doesn't sync
        "linked_helper": select.linked_helper if select.hass_connected else None,
    }


async def async_service_set_schedule(
    entity: "MtsClimate", call: "ServiceCall", /
) -> "ServiceResponse":
    if not entity.schedule_limits_known():
        raise HomeAssistantError("Schedule not available yet")
    try:
        result = compile_schedule(
            call.data[ATTR_SCHEDULE], **schedule_compile_kwargs(entity, entity.hass)
        )
    except ScheduleValidationError as error:
        raise ServiceValidationError(str(error)) from error
    await entity.select_schedule_helper.async_write(result.native)
    return {"warnings": list(result.warnings)}


async def async_service_sync_schedule(entity: "MtsClimate", call: "ServiceCall", /):
    await entity.select_schedule_helper.async_push()


SERVICES = (
    (
        mlc.SERVICE_GET_SCHEDULE,
        async_service_get_schedule,
        None,
        SupportsResponse.ONLY,
    ),
    (
        mlc.SERVICE_SET_SCHEDULE,
        async_service_set_schedule,
        SET_SCHEDULE_SCHEMA,
        SupportsResponse.OPTIONAL,
    ),
    (
        mlc.SERVICE_SYNC_SCHEDULE,
        async_service_sync_schedule,
        None,
        SupportsResponse.NONE,
    ),
)


def async_register(api: "ComponentApi", /):
    """Registers the schedule services targeting the meross_lan climate entities."""
    if not async_register_platform_entity_service:
        api.log(api.DEBUG, "Schedule services need HA core 2025.10: not registered")
        return
    for name, func, schema, supports_response in SERVICES:
        async_register_platform_entity_service(
            api.hass,
            mlc.DOMAIN,
            name,
            entity_domain=Platform.CLIMATE,
            func=func,
            schema=schema,
            supports_response=supports_response,
        )


def async_unregister(hass: "HomeAssistant", /):
    if async_register_platform_entity_service:
        for name, *_ in SERVICES:
            hass.services.async_remove(mlc.DOMAIN, name)
