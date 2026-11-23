"""Test mts215b specific thermostat features:
- humidity calibration (Thermostat.Calibration 'humiValue')
- open window detection enable/disable (Thermostat.WindowOpened 'detect')
- streamlined standby view (Screen.Brightness 'standbyView')
"""

from typing import TYPE_CHECKING

from custom_components.meross_lan.merossclient.protocol import (
    const as mc,
    namespaces as mn,
)
from custom_components.meross_lan.merossclient.protocol.namespaces import (
    thermostat as mn_t,
)

from tests import helpers

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

KEY_CALIBRATION_HUMIDITY = "calibration_humidity"
KEY_WINDOWOPENED_DETECT = "windowopened_detect"
KEY_STANDBYVIEW = "screenbrightness_standbyview"


def _get_entity(device, entity_key: str):
    for entity in device.entities.values():
        if entity.entity_key == entity_key:
            return entity
    return None


def _emulator_state(device_context, ns: "mn.Namespace", key: str):
    return device_context.emulator.namespaces[ns][ns.key][0][key]


async def test_mts215b_entities(request, hass: "HomeAssistant"):
    """The new entities are created with the device values from the trace."""
    async with helpers.DeviceContext(
        request, hass, mc.TYPE_MTS215B, auto_poll=True
    ) as device_context:
        device = device_context.device

        number_humi = _get_entity(device, KEY_CALIBRATION_HUMIDITY)
        switch_detect = _get_entity(device, KEY_WINDOWOPENED_DETECT)
        switch_standbyview = _get_entity(device, KEY_STANDBYVIEW)
        assert number_humi and switch_detect and switch_standbyview

        humi_value = _emulator_state(
            device_context, mn_t.Appliance_Control_Thermostat_Calibration, "humiValue"
        )
        assert number_humi.native_value == humi_value / 10
        assert number_humi.translation_key == "calibration_humidity"

        detect = _emulator_state(
            device_context, mn_t.Appliance_Control_Thermostat_WindowOpened, "detect"
        )
        assert switch_detect.is_on is (detect == 1)

        standby_view = _emulator_state(
            device_context, mn.Appliance_Control_Screen_Brightness, "standbyView"
        )
        assert switch_standbyview.is_on is (standby_view == 1)

        for entity in (number_humi, switch_detect, switch_standbyview):
            state = device_context.get_hass_state(entity.entity_id)
            assert state and state.state not in ("unavailable", "unknown")


async def test_mts215b_controls(request, hass: "HomeAssistant"):
    """The new entities can be controlled and the device state is updated."""
    async with helpers.DeviceContext(
        request, hass, mc.TYPE_MTS215B, auto_poll=True
    ) as device_context:
        device = device_context.device

        # humidity calibration
        number_humi = _get_entity(device, KEY_CALIBRATION_HUMIDITY)
        assert number_humi.native_step == 1  # whole percent steps as in the app
        await number_humi.async_set_native_value(-7)
        # the request is debounced
        await device_context.time_mock.async_tick(number_humi.DEBOUNCE_DELAY)
        assert (
            _emulator_state(
                device_context,
                mn_t.Appliance_Control_Thermostat_Calibration,
                "humiValue",
            )
            == -70
        )
        assert number_humi.native_value == -7
        # temperature calibration must not be affected
        number_temp = _get_entity(device, "calibration")
        assert number_temp and number_temp.native_value == 0

        # open window detection
        switch_detect = _get_entity(device, KEY_WINDOWOPENED_DETECT)
        await switch_detect.async_turn_off()
        assert (
            _emulator_state(
                device_context, mn_t.Appliance_Control_Thermostat_WindowOpened, "detect"
            )
            == 0
        )
        assert switch_detect.is_on is False
        await switch_detect.async_turn_on()
        assert (
            _emulator_state(
                device_context, mn_t.Appliance_Control_Thermostat_WindowOpened, "detect"
            )
            == 1
        )
        assert switch_detect.is_on is True

        # streamlined standby view: 1 = on, 2 = off
        switch_standbyview = _get_entity(device, KEY_STANDBYVIEW)
        await switch_standbyview.async_turn_on()
        assert (
            _emulator_state(
                device_context, mn.Appliance_Control_Screen_Brightness, "standbyView"
            )
            == 1
        )
        assert switch_standbyview.is_on is True
        await switch_standbyview.async_turn_off()
        assert (
            _emulator_state(
                device_context, mn.Appliance_Control_Screen_Brightness, "standbyView"
            )
            == 2
        )
        assert switch_standbyview.is_on is False
        # brightness levels must not be affected
        assert (
            _emulator_state(
                device_context, mn.Appliance_Control_Screen_Brightness, "operation"
            )
            == _get_entity(device, "screenbrightness_operation").native_value
        )


async def test_mts200_without_new_features(request, hass: "HomeAssistant"):
    """Devices not carrying the new keys don't get (dead) entities."""
    async with helpers.DeviceContext(
        request, hass, mc.TYPE_MTS200, auto_poll=True
    ) as device_context:
        device = device_context.device
        assert _get_entity(device, KEY_CALIBRATION_HUMIDITY) is None
        assert _get_entity(device, KEY_STANDBYVIEW) is None
