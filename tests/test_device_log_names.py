"""Test device names in HTTP warning logs."""

import logging
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from custom_components.meross_lan.merossclient.protocol import const as mc
from custom_components.meross_lan.merossclient import logging as ml_logging
from tests import helpers


@pytest.fixture
async def device_context(request, hass):
    """Set up a device using the upstream HTTP emulator and HA registry."""
    async with helpers.DeviceContext(
        request, hass, helpers.build_emulator(mc.TYPE_MSS310), auto_poll=True
    ) as context:
        context.device.obfuscate = False
        yield context


def device_messages(caplog, device):
    """Return formatted messages emitted by this device's logger."""
    return [
        record.getMessage()
        for record in caplog.records
        if record.name == device.logger.name
    ]


async def test_http_exception_names_and_rename(device_context, caplog):
    """Use the current registry name, including after a user rename."""
    device = device_context.device
    http = device.http
    assert http is not None
    registry = device.device_registry
    entry_id = device.device_entry.id
    default_name = registry.async_get(entry_id).name
    assert default_name
    caplog.set_level(logging.DEBUG, logger=device.logger.name)
    caplog.clear()

    http.log_exception(logging.WARNING, ConnectionError("offline"), "async_connect")
    assert device_messages(caplog, device)[-1] == (
        f"HTTP: ConnectionError(offline) [{default_name}] in async_connect"
    )
    registry.async_update_device(entry_id, name_by_user="Kitchen 100%")
    http.log_exception(logging.WARNING, TimeoutError("timed out"), "async_request")
    assert device_messages(caplog, device)[-1] == (
        "HTTP: TimeoutError(timed out) [Kitchen 100%] in async_request"
    )
    http.log(logging.WARNING, "Progress %s%%", 50)
    assert device_messages(caplog, device)[-1] == "HTTP: Progress 50% [Kitchen 100%]"
    registry.async_update_device(entry_id, name_by_user="New name")
    http.log(logging.WARNING, "Progress %s%%", 50)
    assert device_messages(caplog, device)[-1] == "HTTP: Progress 50% [New name]"
    registry.async_update_device(entry_id, name_by_user=None)
    http.log(logging.WARNING, "Default name")
    assert device_messages(caplog, device)[-1] == f"HTTP: Default name [{default_name}]"


async def test_obfuscation_omits_the_device_name(device_context, caplog):
    device = device_context.device
    device.obfuscate = True
    caplog.set_level(logging.WARNING, logger=device.logger.name)
    caplog.clear()
    device.http.log_exception(
        logging.WARNING, ConnectionError("offline"), "async_connect"
    )
    assert device_messages(caplog, device)[-1] == (
        "HTTP: ConnectionError(offline) in async_connect"
    )


async def test_missing_registry_entry_and_non_http_logs(device_context, caplog):
    """Absent entries and non-warning or non-HTTP messages keep their format."""
    device = device_context.device
    http = device.http
    assert http is not None
    caplog.set_level(logging.DEBUG, logger=device.logger.name)
    caplog.clear()

    with patch.object(type(device.device_registry), "async_get", return_value=None):
        http.log(logging.WARNING, "No registry entry")
    with patch.object(
        type(device.device_registry),
        "async_get",
        return_value=SimpleNamespace(name_by_user=None, name=None),
    ):
        http.log(logging.WARNING, "No registry name")
    http.log(logging.DEBUG, "HTTP debug %s", "message")
    device.log(logging.WARNING, "MQTT: Warning %s", "message")
    assert device_messages(caplog, device) == [
        "HTTP: No registry entry",
        f"HTTP: No registry name [{device.descriptor.productname}]",
        "HTTP: HTTP debug message",
        "MQTT: Warning message",
    ]


async def test_http_warning_with_active_trace(device_context, caplog):
    """The logger metadata must not become a trace-formatting argument."""
    device = device_context.device
    caplog.set_level(logging.WARNING, logger=device.logger.name)
    caplog.clear()
    device._trace_data = [["header"]]
    try:
        device.http.log(logging.WARNING, "Trace %s%%", 50)
        assert device_messages(caplog, device)[-1].endswith(
            f"Trace 50% [{device.device_entry.name}]"
        )
        assert device._trace_data[-1][-1] == "HTTP: Trace 50%"
    finally:
        device._trace_data = None


def test_suppression_and_filters_are_per_device(caplog):
    """Identical warnings on different device loggers are independently logged."""
    first = ml_logging.getLogger("meross_test.name_first")
    second = ml_logging.getLogger("meross_test.name_second")
    caplog.set_level(logging.WARNING, logger="meross_test")
    with patch.dict(ml_logging._Logger._LOGGER_TIMEOUTS, {}, clear=True):
        first.warning(
            "HTTP: timeout in async_connect",
            device_name="Same",
            obfuscate=False,
            timeout=60,
        )
        first.warning(
            "HTTP: timeout in async_connect",
            device_name="Renamed",
            obfuscate=False,
            timeout=60,
        )
        second.warning(
            "HTTP: timeout in async_connect",
            device_name="Same",
            obfuscate=False,
            timeout=60,
        )
        assert [(record.name, record.getMessage()) for record in caplog.records] == [
            (first.name, "HTTP: timeout [Same] in async_connect"),
            (second.name, "HTTP: timeout [Same] in async_connect"),
        ]
        caplog.clear()
        reject = lambda record: False
        first.addFilter(reject)
        try:
            first.warning("HTTP: filtered", device_name="Same", obfuscate=False)
            second.warning("HTTP: filtered", device_name="Same", obfuscate=False)
        finally:
            first.removeFilter(reject)
        assert [(record.name, record.getMessage()) for record in caplog.records] == [
            (second.name, "HTTP: filtered [Same]")
        ]
