"""
Test the mts150 valve motor sensors parsed from the hub digest (#688)
"""

from typing import TYPE_CHECKING

from homeassistant.helpers import entity_registry as er

from custom_components.meross_lan.devices.hub.mts import mts150
from custom_components.meross_lan.merossclient.protocol import const as mc
from custom_components.meross_lan.sensor import SensorEntity
import emulator

from tests import const as tc, helpers

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

TRACE = "U01234567890123456789012345678912-Kpippo-msh300hk-mts150.csv"


async def test_mts150_motor_sensors(request, hass: "HomeAssistant"):
    device_emulator = emulator.build_emulator(
        tc.EMULATOR_TRACES_PATH + TRACE, key=tc.MOCK_KEY, uuid=tc.MOCK_DEVICE_UUID
    )
    async with helpers.DeviceContext(
        request, hass, device_emulator, auto_poll=True
    ) as context:
        device = context.device
        p_mts150_digests = {
            p_subdevice[mc.KEY_ID]: p_subdevice[mc.TYPE_MTS150]
            for p_subdevice in device_emulator.descriptor.digest[mc.KEY_HUB][
                mc.KEY_SUBDEVICE
            ]
            if mc.TYPE_MTS150 in p_subdevice
        }
        valves = [
            subdevice
            for subdevice in device.subdevices.values()  # type: ignore
            if isinstance(subdevice, mts150)
        ]
        assert valves and len(valves) == len(p_mts150_digests)

        entity_registry = er.async_get(hass)
        for valve in valves:
            p_digest = p_mts150_digests[valve.id]
            position = valve.sensor_motor_position
            start_count = valve.sensor_motor_start_count
            assert position and start_count
            assert position.native_value == p_digest[mc.KEY_MOTORCURLOCATION]
            assert start_count.native_value == p_digest[mc.KEY_MOTORSTARTCTR]
            assert start_count.state_class is SensorEntity.StateClass.TOTAL_INCREASING
            for sensor in (position, start_count):
                assert sensor.entity_id
                registry_entry = entity_registry.async_get(sensor.entity_id)
                assert registry_entry
                assert (
                    registry_entry.disabled_by is er.RegistryEntryDisabler.INTEGRATION
                )
                assert registry_entry.entity_category is None

        valve = valves[0]
        p_digest = p_mts150_digests[valve.id]

        async def _async_poll_digest():
            # Appliance.System.All (carrying the hub digest) is polled every HEARTBEAT_TIMEOUT over HTTP
            await context.time_mock.async_tick(device.HEARTBEAT_TIMEOUT)
            await context.time_mock.async_tick(device.polling_period)

        # the valve opens fully
        p_digest[mc.KEY_MOTORCURLOCATION] = 1504
        p_digest[mc.KEY_MOTORSTARTCTR] += 1
        p_digest[mc.KEY_MOTORTOTALPATH] += 1000
        await _async_poll_digest()
        assert valve.sensor_motor_position.native_value == 1504  # type: ignore
        assert (
            valve.sensor_motor_start_count.native_value  # type: ignore
            == p_digest[mc.KEY_MOTORSTARTCTR]
        )

        # after a hub reboot the digest reports 0 for every motor key
        p_digest[mc.KEY_MOTORCURLOCATION] = 0
        p_digest[mc.KEY_MOTORSTARTCTR] = 0
        p_digest[mc.KEY_MOTORTOTALPATH] = 0
        await _async_poll_digest()
        assert valve.sensor_motor_position.native_value is None  # type: ignore
        assert valve.sensor_motor_start_count.native_value is None  # type: ignore
