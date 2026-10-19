"""
Test the climate 'tracked sensor' calibration loop (#684)
"""

from typing import TYPE_CHECKING
from unittest.mock import patch

from homeassistant import const as hac
from homeassistant.components import sensor
from homeassistant.setup import async_setup_component

from custom_components.meross_lan.climate import MtsClimate
from custom_components.meross_lan.merossclient.protocol import const as mc
from custom_components.meross_lan.merossclient.protocol.namespaces import (
    hub as mn_h,
)
from emulator.mixins.hub import HubMixin

from tests import helpers

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

TRACKED_ENTITY_ID = "sensor.tracked_temperature"


async def test_climate_tracking(request, hass: "HomeAssistant"):
    # the tracked sensor select needs the sensor component to build its options
    assert await async_setup_component(hass, sensor.DOMAIN, {})
    with patch.object(
        MtsClimate.TrackSensorSelect, "_attr_entity_registry_enabled_default", True
    ):
        async with helpers.DeviceContext(
            request, hass, mc.TYPE_MSH300, auto_poll=True
        ) as context:
            emulator = context.emulator
            assert isinstance(emulator, HubMixin)
            climate = next(
                subdevice
                for subdevice in context.device.subdevices.values()  # type: ignore
                if isinstance(subdevice, MtsClimate)
            )
            select = climate.select_track_sensor
            number = climate.number_adjust_temperature
            assert select.hass_connected

            def _set_tracked(temperature: float):
                hass.states.async_set(
                    TRACKED_ENTITY_ID,
                    str(temperature),
                    {hac.ATTR_UNIT_OF_MEASUREMENT: hac.UnitOfTemperature.CELSIUS},
                )

            def _emulator_report(temperature: float):
                """Sets the room temperature reported by the (emulated) device."""
                emulator._get_subdevice_namespace(
                    climate.id, mn_h.Appliance_Hub_Mts100_Temperature
                )[mc.KEY_ROOM] = round(temperature * climate.temperature_scale)

            reading = climate.current_temperature
            adjust = number.native_value
            assert reading is not None and adjust is not None

            # first sample: the error (+1.2) is corrected in whole calibration steps (0.5)
            _set_tracked(reading + 1.2)
            await select.async_select_option(TRACKED_ENTITY_ID)
            await context.time_mock.async_tick(select.TRACKING_DELAY)
            await context.time_mock.async_tick(number.DEBOUNCE_DELAY)
            assert number.native_value == adjust + 1.0
            assert emulator._get_subdevice_namespace(
                climate.id, mn_h.Appliance_Hub_Mts100_Adjust
            )[mc.KEY_TEMPERATURE] == round((adjust + 1.0) * number.device_scale)

            # the device doesn't report a new reading (the emulator doesn't apply
            # the calibration) while the tracked sensor changes: the (stale) error
            # must not be applied again (windup)
            _set_tracked(reading + 1.1)
            await context.async_poll_timeout(2 * select.TRACKING_PERIOD)
            assert climate.current_temperature == reading
            assert number.native_value == adjust + 1.0

            # the device reports a reading including the calibration: the residual
            # error (+0.1) is below half calibration step so nothing changes
            _emulator_report(reading + 1.0)
            await context.async_poll_timeout(select.TRACKING_PERIOD)
            assert climate.current_temperature == reading + 1.0
            assert number.native_value == adjust + 1.0

            # the tracked sensor rises: corrected at the next sample
            _set_tracked(reading + 1.6)
            await context.async_poll_timeout(select.TRACKING_PERIOD)
            assert number.native_value == adjust + 1.5

            # tracking off: no more corrections
            await select.async_select_option(hac.STATE_OFF)
            _emulator_report(reading + 1.5)
            _set_tracked(reading + 3.0)
            await context.async_poll_timeout(2 * select.TRACKING_PERIOD)
            assert number.native_value == adjust + 1.5
