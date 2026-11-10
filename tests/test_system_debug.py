"""Test the Appliance.System.Debug diagnostic entities (all disabled by default)."""

from typing import TYPE_CHECKING

from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util

from custom_components.meross_lan import const as mlc
from custom_components.meross_lan.devices.system_debug import (
    SystemDebugCloudConnection,
    SystemDebugNamespaceHandler,
    UptimeSensor,
)
from custom_components.meross_lan.merossclient.protocol import (
    const as mc,
    namespaces as mn,
)

from tests import helpers

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

ENTITY_KEYS = (
    "uptime",
    "wifi_rssi",
    "wifi_channel",
    "wifi_disconnects",
    "cloud_connection",
    "cloud_disconnects",
    "memory_free",
)


def _get_entity(device, entity_key: str):
    for entity in device.entities.values():
        if entity.entity_key == entity_key:
            return entity
    return None


def test_uptime_parse():
    assert UptimeSensor.parse_uptime("15h56m35s") == 15 * 3600 + 56 * 60 + 35
    assert UptimeSensor.parse_uptime("1d2h3m4s") == 86400 + 2 * 3600 + 3 * 60 + 4
    assert UptimeSensor.parse_uptime("42s") == 42
    assert UptimeSensor.parse_uptime("") is None


async def test_system_debug_entities(request, hass: "HomeAssistant"):
    async with helpers.DeviceContext(
        request, hass, mc.TYPE_MTS200, auto_poll=True
    ) as device_context:
        device = device_context.device
        handler = device.ns_handlers[mn.Appliance_System_Debug]
        assert isinstance(handler, SystemDebugNamespaceHandler)

        # all disabled by default: no polling
        ent_reg = er.async_get(hass)
        entity_ids = {}
        for entity_key in ENTITY_KEYS:
            entity = _get_entity(device, entity_key)
            assert entity, entity_key
            assert entity.entity_category is entity.EntityCategory.DIAGNOSTIC
            entity_id = ent_reg.async_get_entity_id(
                entity.PLATFORM, mlc.DOMAIN, entity.unique_id
            )
            assert entity_id, entity_key
            assert (registry_entry := ent_reg.async_get(entity_id))
            assert registry_entry.disabled_by is er.RegistryEntryDisabler.INTEGRATION
            entity_ids[entity_key] = entity_id
        assert handler.polling_strategy is None
        assert not handler.last_poll_epoch

        # enabling any of the entities enables the polling
        device = await device_context.async_enable_entity(entity_ids["wifi_rssi"])
        handler = device.ns_handlers[mn.Appliance_System_Debug]
        assert handler.polling_strategy is not None
        assert handler.last_poll_epoch

        state = device_context.get_hass_state(entity_ids["wifi_rssi"])
        assert state and float(state.state) == -65
        # the other (disabled) entities are parsed too
        assert _get_entity(device, "wifi_channel").native_value == 11
        assert _get_entity(device, "wifi_disconnects").native_value == 0
        assert _get_entity(device, "cloud_disconnects").native_value == 2
        assert _get_entity(device, "memory_free").native_value == 3562976
        cloud_connection = _get_entity(device, "cloud_connection")
        assert isinstance(cloud_connection, SystemDebugCloudConnection)
        assert cloud_connection.is_on is True
        cloud_connection.update_device_value("disconnected")
        assert cloud_connection.is_on is False

        # uptime: boot time computed against the device clock ('UTC') in the payload
        sensor_uptime = _get_entity(device, "uptime")
        assert sensor_uptime.native_value == dt_util.utc_from_timestamp(
            device_context.emulator.get_debug_boot_epoch()
        )
        boot_time = sensor_uptime.native_value
        await device_context.async_poll_timeout(handler.polling_period * 2)
        assert sensor_uptime.native_value == boot_time
        # device clock not synced (i.e. 1970): fallback to the local clock
        sensor_uptime({mc.KEY_SYSTEM: {"sysUpTime": "1d1h", "UTC": 3600}})
        assert sensor_uptime.native_value == dt_util.utc_from_timestamp(
            int(device.time()) - 90000
        )
        # missing keys are just ignored
        _get_entity(device, "wifi_rssi")({mc.KEY_SYSTEM: {}})

        # disabling the (last) enabled entity stops polling
        for entity in tuple(handler.enabled_entities):
            handler.entity_disabled(entity)
        assert handler.polling_strategy is None
