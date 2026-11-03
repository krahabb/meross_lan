"""Schedule mode must remain an HVAC mode after commands and device refreshes."""

from copy import deepcopy
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, call, patch

from homeassistant.components.climate import (
    ClimateEntityFeature,
    HVACAction,
    HVACMode,
)
import pytest

from custom_components.meross_lan.climate import MtsClimate
from custom_components.meross_lan.devices.hub.mts import (
    mts100 as Mts100Climate,
    mts150 as Mts150Climate,
)
from custom_components.meross_lan.devices.thermostat import MtsSummerMode
from custom_components.meross_lan.devices.thermostat.mts200 import Mts200Climate
from custom_components.meross_lan.devices.thermostat.mts300 import Mts300Climate
from custom_components.meross_lan.devices.thermostat.mts960 import Mts960Climate
from custom_components.meross_lan.merossclient.device.handler import NamespaceHandler
from custom_components.meross_lan.merossclient.exceptions import MerossError
from custom_components.meross_lan.merossclient.protocol import (
    const as mc,
    namespaces as mn,
)
from custom_components.meross_lan.merossclient.protocol.namespaces import (
    hub as mn_h,
    thermostat as mn_t,
)
from custom_components.meross_lan.switch import SwitchParser

HUB_CLIMATE_TYPES = (Mts100Climate, Mts150Climate)
CLIMATE_TYPES = (*HUB_CLIMATE_TYPES, Mts200Climate, Mts300Climate, Mts960Climate)


def refresh(climate, **changes):
    """Feed reports through the v6 climate parser and the hub mode routing."""
    payload = deepcopy(climate.ns_value)
    if isinstance(climate, Mts100Climate):
        climate._parse_mode({"state": changes.pop("mode", climate._mts_mode)})
        climate._parse_togglex({"onoff": changes.pop("onoff", climate._mts_onoff)})
        payload.update(changes)
        payload["room"] = payload.get("room", 200) + 1
    else:
        payload.update(changes)
        payload["currentTemp"] = payload.get("currentTemp", 2000) + 1
    climate(payload)


def summermode_switch(climate, mode):
    """Use the actual separate v6 switch to select the physical direction."""
    switch = MtsSummerMode.__new__(MtsSummerMode)
    switch.parent = climate.parent
    switch.index = climate.index
    switch.ns = mn_t.Appliance_Control_Thermostat_SummerMode
    switch.id = "summer-mode"
    switch.logtag = "summer-mode"
    switch.key_value = switch._INIT_key_value
    switch.ns_value = None
    switch.value_on = mc.MTS200_SUMMERMODE_COOL
    switch.value_off = mc.MTS200_SUMMERMODE_HEAT
    switch.available = True
    switch.is_on = None
    with patch.object(SwitchParser, "flush_state"):
        switch({"mode": mode})
    return switch


@pytest.fixture
def make_climate():
    """Keep v6 SETACK merging and parser/state logic; replace transport/publishing."""

    def make(climate_type):
        climate = climate_type.__new__(climate_type)
        hub_climate = climate_type in HUB_CLIMATE_TYPES
        climate.id = "valve" if hub_climate else "0"
        climate.logger = logging.getLogger(__name__)
        climate.logtag = "test climate"
        climate.index = (
            mn.IndexType.id.get("valve") if hub_climate else mn.IndexType.channel.get(0)
        )
        climate.ns = {
            Mts100Climate: mn_h.Appliance_Hub_Mts100_Temperature,
            Mts150Climate: mn_h.Appliance_Hub_Mts100_Temperature,
            Mts200Climate: mn_t.Appliance_Control_Thermostat_Mode,
            Mts300Climate: mn_t.Appliance_Control_Thermostat_ModeC,
            Mts960Climate: mn_t.Appliance_Control_Thermostat_ModeB,
        }[climate_type]

        async def acknowledge(namespace, method, payload):
            if namespace == mn_h.Appliance_Hub_Mts100_Temperature:
                temperature = payload["temperature"][0]
                state = dict(climate.ns_value) | temperature
                state["currentSet"] = temperature["custom"]
                return SimpleNamespace(payload={"temperature": [state]})
            return SimpleNamespace(payload={})

        climate.parent = SimpleNamespace(
            id="device-uuid",
            log=Mock(),
            async_request=AsyncMock(side_effect=acknowledge),
            create_diagnostic_entities=False,
            ns_handlers={},
        )
        handler = NamespaceHandler.__new__(NamespaceHandler)
        handler.logtag = "test handler"
        handler.id = climate.ns
        handler.parent = climate.parent
        handler.parsers = {climate.index: climate}
        climate.parent.ns_handlers[climate.ns] = handler
        climate._mts_mode = 0
        climate._mts_onoff = 1
        climate._mts_active = False
        climate.ns_value = {"id": "valve"} if hub_climate else {}
        climate.temperature_scale = (
            10 if hub_climate or climate_type is Mts200Climate else 100
        )
        climate.native_current_temperature = None
        climate.native_target_temperature = None
        climate.preset_mode = None
        climate.hvac_mode = None
        climate.hvac_action = None
        climate.hvac_modes = climate._attr_hvac_modes
        climate.preset_modes = climate._attr_preset_modes
        climate.supported_features = climate._attr_supported_features
        climate.min_temp = 5
        climate.max_temp = 35
        climate.number_preset_temperature = ()
        climate.sensor_current_temperature = Mock()
        climate.select_track_sensor = Mock()
        if hub_climate:
            climate.switch_patch_hvacaction = SimpleNamespace(is_on=False)
            climate.binary_sensor_window = Mock()
            if climate_type is Mts150Climate:
                climate.sensor_motor_position = None
                climate.sensor_motor_start_count = None
            refresh(climate, mode=0, currentSet=210)
        elif climate_type is Mts200Climate:
            refresh(climate, mode=4, onoff=1, targetTemp=210, manualTemp=210)
        elif climate_type is Mts300Climate:
            climate._mts_work = None
            climate.sensor_current_humidity = Mock(native_value=50)
            climate.number_fan_hold = Mock()
            climate.switch_fan_hold = Mock()
            for key in ("hdStatus", "hStatus", "cStatus", "fStatus", "aStatus"):
                setattr(climate, f"sensor_{key}", Mock())
            refresh(
                climate,
                channel=0,
                mode=1,
                work=1,
                sensorTemp=2000,
                targetTemp={"heat": 2100, "cold": 2500},
                more={
                    "humi": 500,
                    "hdStatus": 0,
                    "hStatus": 0,
                    "cStatus": 0,
                    "fStatus": 0,
                    "aStatus": 0,
                },
                fan={"speed": 0, "hTime": mc.MTS300_FAN_HOLD_DISABLED},
            )
        else:
            climate._mts_working = 1
            climate._mts_timer_mode = None
            climate.binary_sensor_plug_state = Mock()
            refresh(climate, mode=1, working=1, onoff=1, targetTemp=2100)
        return climate

    with patch.object(MtsClimate, "flush_state"):
        yield make


@pytest.mark.parametrize("climate_type", CLIMATE_TYPES)
def test_schedule_is_advertised_as_hvac_mode(make_climate, climate_type):
    climate = make_climate(climate_type)
    assert HVACMode.AUTO in climate.hvac_modes
    assert "auto" not in climate.preset_modes
    expected_presets = (
        set()
        if climate_type is Mts300Climate
        else (
            {
                "heating",
                "cooling",
                "schedule_heating",
                "schedule_cooling",
                "timer_cycle",
                "timer_countdown_on",
                "timer_countdown_off",
            }
            if climate_type is Mts960Climate
            else {"none", "comfort", "sleep", "away"}
        )
    )
    assert set(climate.preset_modes) == expected_presets
    assert bool(expected_presets) == (
        ClimateEntityFeature.PRESET_MODE in climate.supported_features
    )


@pytest.mark.parametrize("climate_type", CLIMATE_TYPES)
@pytest.mark.parametrize("start_off", [False, True], ids=["manual", "off"])
async def test_auto_enables_schedule_and_survives_refresh(
    make_climate, climate_type, start_off
):
    climate = make_climate(climate_type)
    if start_off:
        await climate.async_set_hvac_mode(HVACMode.OFF)
    climate.parent.async_request.reset_mock()

    await climate.async_set_hvac_mode(HVACMode.AUTO)

    if climate_type in HUB_CLIMATE_TYPES:
        expected_calls = [
            call(
                "Appliance.Hub.Mts100.Mode",
                "SET",
                {"mode": [{"id": "valve", "state": 3}]},
            )
        ]
        if start_off:
            expected_calls.append(
                call(
                    "Appliance.Hub.ToggleX",
                    "SET",
                    {"togglex": [{"id": "valve", "onoff": 1}]},
                )
            )
    elif climate_type is Mts200Climate:
        expected_calls = [
            call(
                "Appliance.Control.Thermostat.Mode",
                "SET",
                {"mode": [{"channel": 0, "mode": 3, "onoff": 1}]},
            )
        ]
    elif climate_type is Mts300Climate:
        expected_calls = [
            call(
                "Appliance.Control.Thermostat.ModeC",
                "SET",
                {"control": [{"channel": 0, "mode": 1, "work": 2}]},
            )
        ]
    else:
        expected_calls = [
            call(
                "Appliance.Control.Thermostat.ModeB",
                "SET",
                {"modeB": [{"channel": 0, "mode": 2, "onoff": 1}]},
            )
        ]
    assert climate.parent.async_request.await_args_list == expected_calls
    assert climate.hvac_mode == HVACMode.AUTO
    assert climate.is_mts_scheduled()
    refresh(climate)
    assert climate.hvac_mode == HVACMode.AUTO
    assert climate.preset_mode == {
        Mts300Climate: None,
        Mts960Climate: "schedule_heating",
    }.get(climate_type, "none")


@pytest.mark.parametrize("climate_type", CLIMATE_TYPES)
async def test_off_wins_over_remembered_schedule(make_climate, climate_type):
    climate = make_climate(climate_type)
    await climate.async_set_hvac_mode(HVACMode.AUTO)
    await climate.async_set_hvac_mode(HVACMode.OFF)
    refresh(climate)
    assert climate.hvac_mode == HVACMode.OFF
    assert climate.hvac_action == HVACAction.OFF
    assert not climate.is_mts_scheduled()


@pytest.mark.parametrize(
    "climate_type, hvac_mode, expected_payload",
    [
        (Mts100Climate, HVACMode.HEAT, {"mode": [{"id": "valve", "state": 0}]}),
        (Mts150Climate, HVACMode.HEAT, {"mode": [{"id": "valve", "state": 0}]}),
        (
            Mts200Climate,
            HVACMode.HEAT,
            {"mode": [{"channel": 0, "mode": 4, "onoff": 1}]},
        ),
        (
            Mts200Climate,
            HVACMode.COOL,
            {"mode": [{"channel": 0, "mode": 4, "onoff": 1}]},
        ),
        (
            Mts300Climate,
            HVACMode.HEAT,
            {"control": [{"channel": 0, "mode": 1, "work": 1}]},
        ),
        (
            Mts300Climate,
            HVACMode.COOL,
            {"control": [{"channel": 0, "mode": 2, "work": 1}]},
        ),
        (
            Mts300Climate,
            HVACMode.HEAT_COOL,
            {"control": [{"channel": 0, "mode": 3, "work": 1}]},
        ),
        (
            Mts960Climate,
            HVACMode.HEAT,
            {"modeB": [{"channel": 0, "mode": 1, "onoff": 1, "working": 1}]},
        ),
        (
            Mts960Climate,
            HVACMode.COOL,
            {"modeB": [{"channel": 0, "mode": 1, "onoff": 1, "working": 2}]},
        ),
    ],
)
async def test_manual_hvac_mode_exits_schedule(
    make_climate, climate_type, hvac_mode, expected_payload
):
    climate = make_climate(climate_type)
    if climate_type is Mts200Climate:
        summermode_switch(climate, 2 if hvac_mode == HVACMode.COOL else 1)
    await climate.async_set_hvac_mode(HVACMode.AUTO)
    climate.parent.async_request.reset_mock()

    await climate.async_set_hvac_mode(hvac_mode)

    assert climate.parent.async_request.await_args_list == [
        call(
            (
                "Appliance.Hub.Mts100.Mode"
                if climate_type in HUB_CLIMATE_TYPES
                else climate.ns
            ),
            "SET",
            expected_payload,
        )
    ]
    assert climate.hvac_mode == hvac_mode
    assert not climate.is_mts_scheduled()
    refresh(climate)
    assert climate.hvac_mode == hvac_mode


@pytest.mark.parametrize("climate_type", [*HUB_CLIMATE_TYPES, Mts200Climate])
@pytest.mark.parametrize(
    "preset, device_mode", [("comfort", 1), ("sleep", 2), ("away", 4)]
)
async def test_other_presets_keep_their_meaning(
    make_climate, climate_type, preset, device_mode
):
    climate = make_climate(climate_type)
    await climate.async_set_hvac_mode(HVACMode.AUTO)
    climate.parent.async_request.reset_mock()
    await climate.async_set_preset_mode(preset)
    if climate_type in HUB_CLIMATE_TYPES:
        expected = call(
            "Appliance.Hub.Mts100.Mode",
            "SET",
            {"mode": [{"id": "valve", "state": device_mode}]},
        )
    else:
        expected = call(
            "Appliance.Control.Thermostat.Mode",
            "SET",
            {
                "mode": [
                    {
                        "channel": 0,
                        "mode": {"comfort": 0, "sleep": 1, "away": 2}[preset],
                        "onoff": 1,
                    }
                ]
            },
        )
    assert climate.parent.async_request.await_args_list == [expected]
    refresh(climate)
    assert climate.preset_mode == preset
    assert climate.hvac_mode == HVACMode.HEAT


@pytest.mark.parametrize("climate_type", [*HUB_CLIMATE_TYPES, Mts200Climate])
@pytest.mark.parametrize("start_off", [False, True], ids=["on", "off"])
async def test_none_preset_keeps_schedule(make_climate, climate_type, start_off):
    climate = make_climate(climate_type)
    await climate.async_set_hvac_mode(HVACMode.AUTO)
    if start_off:
        await climate.async_set_hvac_mode(HVACMode.OFF)
    assert climate.preset_mode == "none"
    climate.parent.async_request.reset_mock()

    await climate.async_set_preset_mode("none")

    if climate_type in HUB_CLIMATE_TYPES:
        expected_calls = [
            call(
                "Appliance.Hub.Mts100.Mode",
                "SET",
                {"mode": [{"id": "valve", "state": 3}]},
            )
        ]
        if start_off:
            expected_calls.append(
                call(
                    "Appliance.Hub.ToggleX",
                    "SET",
                    {"togglex": [{"id": "valve", "onoff": 1}]},
                )
            )
    else:
        expected_calls = [
            call(
                "Appliance.Control.Thermostat.Mode",
                "SET",
                {"mode": [{"channel": 0, "mode": 3, "onoff": 1}]},
            )
        ]
    assert climate.parent.async_request.await_args_list == expected_calls
    refresh(climate)
    assert climate.hvac_mode == HVACMode.AUTO
    assert climate.preset_mode == "none"
    assert climate.is_mts_scheduled()


@pytest.mark.parametrize("climate_type", [*HUB_CLIMATE_TYPES, Mts200Climate])
async def test_none_preset_leaves_setpoint_preset_for_manual(
    make_climate, climate_type
):
    climate = make_climate(climate_type)
    assert climate.preset_mode == "none"
    await climate.async_set_preset_mode("comfort")
    climate.parent.async_request.reset_mock()

    await climate.async_set_preset_mode("none")

    if climate_type in HUB_CLIMATE_TYPES:
        expected = call(
            "Appliance.Hub.Mts100.Mode",
            "SET",
            {"mode": [{"id": "valve", "state": 0}]},
        )
    else:
        expected = call(
            "Appliance.Control.Thermostat.Mode",
            "SET",
            {"mode": [{"channel": 0, "mode": 4, "onoff": 1}]},
        )
    assert climate.parent.async_request.await_args_list == [expected]
    refresh(climate)
    assert climate.hvac_mode == HVACMode.HEAT
    assert climate.preset_mode == "none"


@pytest.mark.parametrize("climate_type", CLIMATE_TYPES)
async def test_setting_temperature_leaves_schedule(make_climate, climate_type):
    climate = make_climate(climate_type)
    await climate.async_set_hvac_mode(HVACMode.AUTO)
    climate.parent.async_request.reset_mock()
    await climate.async_set_temperature(temperature=22.5)
    expected = {
        Mts100Climate: [
            call(
                "Appliance.Hub.Mts100.Mode",
                "SET",
                {"mode": [{"id": "valve", "state": 0}]},
            ),
            call(
                "Appliance.Hub.Mts100.Temperature",
                "SET",
                {"temperature": [{"id": "valve", "custom": 225}]},
            ),
        ],
        Mts200Climate: [
            call(
                "Appliance.Control.Thermostat.Mode",
                "SET",
                {"mode": [{"channel": 0, "mode": 4, "manualTemp": 225}]},
            )
        ],
        Mts300Climate: [
            call(
                "Appliance.Control.Thermostat.ModeC",
                "SET",
                {
                    "control": [
                        {
                            "channel": 0,
                            "mode": 1,
                            "work": 1,
                            "targetTemp": {"heat": 2250},
                        }
                    ]
                },
            )
        ],
        Mts960Climate: [
            call(
                "Appliance.Control.Thermostat.ModeB",
                "SET",
                {
                    "modeB": [
                        {"channel": 0, "mode": 1, "working": 1, "targetTemp": 2250}
                    ]
                },
            )
        ],
    }
    assert (
        climate.parent.async_request.await_args_list
        == expected[
            Mts100Climate if climate_type in HUB_CLIMATE_TYPES else climate_type
        ]
    )
    refresh(climate)
    assert climate.hvac_mode == HVACMode.HEAT
    assert climate.native_target_temperature == 22.5
    assert not climate.is_mts_scheduled()


@pytest.mark.parametrize(
    "summermode, expected_action", [(1, HVACAction.HEATING), (2, HVACAction.COOLING)]
)
async def test_mts200_auto_preserves_physical_heat_cool(
    make_climate, summermode, expected_action
):
    climate = make_climate(Mts200Climate)
    switch = summermode_switch(climate, summermode)
    await climate.async_set_hvac_mode(HVACMode.AUTO)
    climate.parent.async_request.assert_awaited_once_with(
        "Appliance.Control.Thermostat.Mode",
        "SET",
        {"mode": [{"channel": 0, "mode": 3, "onoff": 1}]},
    )
    refresh(climate, state=1)
    assert climate.hvac_mode == HVACMode.AUTO
    assert climate.hvac_action == expected_action
    assert switch.ns_value == summermode


@pytest.mark.parametrize(
    "physical_mode, manual_hvac, action, target",
    [
        (1, HVACMode.HEAT, HVACAction.HEATING, 21),
        (2, HVACMode.COOL, HVACAction.COOLING, 25),
        (3, HVACMode.HEAT_COOL, HVACAction.HEATING, None),
    ],
)
async def test_mts300_schedule_preserves_physical_mode_and_action(
    make_climate, physical_mode, manual_hvac, action, target
):
    climate = make_climate(Mts300Climate)
    more = {
        "humi": 500,
        "hdStatus": 0,
        "hStatus": 1,
        "cStatus": 1,
        "fStatus": 0,
        "aStatus": 0,
    }
    refresh(climate, mode=physical_mode, more=more)
    assert climate.hvac_mode == manual_hvac
    await climate.async_set_hvac_mode(HVACMode.AUTO)
    climate.parent.async_request.assert_awaited_once_with(
        "Appliance.Control.Thermostat.ModeC",
        "SET",
        {"control": [{"channel": 0, "mode": physical_mode, "work": 2}]},
    )
    refresh(climate)
    assert climate.hvac_mode == HVACMode.AUTO
    assert climate.hvac_action == action
    assert climate.native_target_temperature == target


@pytest.mark.parametrize(
    "preset, working, expected_action",
    [
        ("schedule_heating", 1, HVACAction.HEATING),
        ("schedule_cooling", 2, HVACAction.COOLING),
    ],
)
async def test_mts960_keeps_specialized_schedule_presets(
    make_climate, preset, working, expected_action
):
    climate = make_climate(Mts960Climate)
    await climate.async_set_preset_mode(preset)
    climate.parent.async_request.assert_awaited_once_with(
        "Appliance.Control.Thermostat.ModeB",
        "SET",
        {"modeB": [{"channel": 0, "mode": 2, "onoff": 1, "working": working}]},
    )
    refresh(climate, state=1)
    assert climate.hvac_mode == HVACMode.AUTO
    assert climate.hvac_action == expected_action
    assert climate.preset_mode == preset


@pytest.mark.parametrize("climate_type", CLIMATE_TYPES)
async def test_failed_auto_command_preserves_previous_state(make_climate, climate_type):
    climate = make_climate(climate_type)
    climate.parent.async_request.side_effect = MerossError("transport failed")
    with pytest.raises(MerossError, match="transport failed"):
        await climate.async_set_hvac_mode(HVACMode.AUTO)
    assert climate.hvac_mode == HVACMode.HEAT
    assert not climate.is_mts_scheduled()


async def test_mts300_auto_after_first_off_report_uses_heat_cool(make_climate):
    climate = make_climate(Mts300Climate)
    climate._mts_mode = 0  # No earlier active mode was received from this device.
    refresh(climate, mode=0)
    await climate.async_set_hvac_mode(HVACMode.AUTO)
    climate.parent.async_request.assert_awaited_once_with(
        "Appliance.Control.Thermostat.ModeC",
        "SET",
        {"control": [{"channel": 0, "mode": 3, "work": 2}]},
    )
    assert climate.hvac_mode == HVACMode.AUTO
    assert climate._mts_mode == 3


async def test_mts300_temperature_range_leaves_schedule_in_heat_cool(make_climate):
    climate = make_climate(Mts300Climate)
    refresh(climate, mode=3, work=2)
    await climate.async_set_temperature(target_temp_low=19.5, target_temp_high=24.5)
    climate.parent.async_request.assert_awaited_once_with(
        "Appliance.Control.Thermostat.ModeC",
        "SET",
        {
            "control": [
                {
                    "channel": 0,
                    "mode": 3,
                    "work": 1,
                    "targetTemp": {"heat": 1950, "cold": 2450},
                }
            ]
        },
    )
    refresh(climate)
    assert climate.hvac_mode == HVACMode.HEAT_COOL
    assert climate.native_target_temperature_low == 19.5
    assert climate.native_target_temperature_high == 24.5
    assert not climate.is_mts_scheduled()


async def test_mts300_rejects_single_temperature_for_heat_cool(make_climate):
    climate = make_climate(Mts300Climate)
    refresh(climate, mode=3, work=2)
    with pytest.raises(ValueError, match="single temperature"):
        await climate.async_set_temperature(temperature=22.5)
    climate.parent.async_request.assert_not_awaited()
    assert climate.hvac_mode == HVACMode.AUTO


async def test_mts200_summer_switch_keeps_schedule_and_changes_physical_cooling(
    make_climate,
):
    climate = make_climate(Mts200Climate)
    switch = summermode_switch(climate, mc.MTS200_SUMMERMODE_HEAT)
    await climate.async_set_hvac_mode(HVACMode.AUTO)
    climate.parent.async_request.reset_mock()
    with patch.object(SwitchParser, "flush_state"):
        await switch.async_turn_on()
    climate.parent.async_request.assert_awaited_once_with(
        mn_t.Appliance_Control_Thermostat_SummerMode,
        "SET",
        {"summerMode": [{"channel": 0, "mode": 2}]},
    )
    refresh(climate, state=1)
    assert climate.hvac_modes == [HVACMode.OFF, HVACMode.COOL, HVACMode.AUTO]
    assert climate.hvac_mode == HVACMode.AUTO
    assert climate.hvac_action == HVACAction.COOLING
    await climate.async_set_hvac_mode(HVACMode.COOL)
    assert climate.hvac_mode == HVACMode.COOL
    assert not climate.is_mts_scheduled()
