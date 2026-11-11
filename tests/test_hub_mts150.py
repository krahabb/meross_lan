"""
Test the mts150 valve motor sensors parsed from the hub digest (#688)
"""

from typing import TYPE_CHECKING

from homeassistant.helpers import entity_registry as er

from custom_components.meross_lan.climate import MtsClimate
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
            for subdevice in device.subdevices.values()
            if isinstance(subdevice, mts150)
        ]
        assert valves and len(valves) == len(p_mts150_digests)

        entity_registry = er.async_get(hass)
        for valve in valves:
            p_digest = p_mts150_digests[valve.id]
            position = valve.sensor_motor_cur_location
            start_count = valve.sensor_motor_start_ctr
            assert position and start_count
            assert (mc.KEY_MOTORCURLOCATION in p_digest) and (
                position.native_value == p_digest[mc.KEY_MOTORCURLOCATION]
            )
            assert (mc.KEY_MOTORSTARTCTR in p_digest) and (
                start_count.native_value == p_digest[mc.KEY_MOTORSTARTCTR]
            )
            assert start_count.state_class is SensorEntity.StateClass.TOTAL_INCREASING
            # the valves in the trace are on and open while reporting 'heating': 0
            assert valve.hvac_action is MtsClimate.HVACAction.HEATING
            for sensor in (position, start_count):
                registry_entry = entity_registry.async_get(sensor.entity_id)
                assert registry_entry
                assert (
                    registry_entry.disabled_by is er.RegistryEntryDisabler.INTEGRATION
                )

        valve = valves[0]
        p_digest = p_mts150_digests[valve.id]

        async def _async_poll_digest():
            # Appliance.System.All (carrying the hub digest) is polled every HEARTBEAT_TIMEOUT over HTTP
            await context.time_mock.async_tick(device.HEARTBEAT_TIMEOUT)
            await context.time_mock.async_tick(device.polling_period)

        # the valve opens fully
        p_digest[mc.KEY_MOTORCURLOCATION] = 1504
        assert mc.KEY_MOTORSTARTCTR in p_digest
        p_digest[mc.KEY_MOTORSTARTCTR] += 1
        assert mc.KEY_MOTORTOTALPATH in p_digest
        p_digest[mc.KEY_MOTORTOTALPATH] += 1000
        await _async_poll_digest()
        assert valve.sensor_motor_cur_location.native_value == 1504
        assert (
            valve.sensor_motor_start_ctr.native_value == p_digest[mc.KEY_MOTORSTARTCTR]
        )
        assert valve.hvac_action is MtsClimate.HVACAction.HEATING
        # a temperature update carrying 'heating': 0 doesn't override the motor state
        p_temperature = valve.ns_value.copy()
        p_temperature[mc.KEY_ROOM] += 5
        p_temperature[mc.KEY_HEATING] = 0
        valve(p_temperature)
        assert valve.hvac_action is MtsClimate.HVACAction.HEATING

        # the valve closes
        p_digest[mc.KEY_MOTORCURLOCATION] = 0
        p_digest[mc.KEY_MOTORSTARTCTR] += 1
        p_digest[mc.KEY_MOTORTOTALPATH] += 1504
        await _async_poll_digest()
        assert valve.sensor_motor_cur_location.native_value == 0
        assert valve.hvac_action is MtsClimate.HVACAction.IDLE

        # after a hub reboot the digest reports 0 for every motor key
        p_digest[mc.KEY_MOTORCURLOCATION] = 0
        p_digest[mc.KEY_MOTORSTARTCTR] = 0
        p_digest[mc.KEY_MOTORTOTALPATH] = 0
        await _async_poll_digest()
        assert valve.sensor_motor_cur_location.native_value is None
        assert valve.sensor_motor_start_ctr.native_value is None
        assert valve.hvac_action is MtsClimate.HVACAction.IDLE
