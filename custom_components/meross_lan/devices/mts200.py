from __future__ import annotations

import typing

from ..binary_sensor import MLBinarySensor
from ..climate import MtsClimate, MtsSetPointNumber
from ..helpers import SmartPollingStrategy, reverse_lookup
from ..meross_entity import EntityCategory
from ..merossclient import const as mc
from ..number import MLConfigNumber
from ..sensor import MLSensor
from ..switch import MLSwitch

if typing.TYPE_CHECKING:
    from ..meross_device import MerossDevice


class Mts200SetPointNumber(MtsSetPointNumber):
    """
    customize MtsSetPointNumber to interact with Mts200 family valves
    """

    namespace = mc.NS_APPLIANCE_CONTROL_THERMOSTAT_MODE
    key_namespace = mc.KEY_MODE


class Mts200OverheatThresholdNumber(MLConfigNumber):
    """
    customize MLConfigNumber to interact with overheat protection value
    """

    _attr_name = "Overheat threshold"

    namespace = mc.NS_APPLIANCE_CONTROL_THERMOSTAT_OVERHEAT
    key_namespace = mc.KEY_OVERHEAT
    key_value = mc.KEY_VALUE

    def __init__(self, manager: ThermostatMixin, channel: object | None):
        self._attr_native_max_value = 70
        self._attr_native_min_value = 20
        super().__init__(
            manager,
            channel,
            "overheat threshold",
            MLConfigNumber.DeviceClass.TEMPERATURE,
        )

    @property
    def native_step(self):
        return 0.5

    @property
    def native_unit_of_measurement(self):
        return MtsClimate.TEMP_CELSIUS

    @property
    def ml_multiplier(self):
        return 10


class Mts200ConfigSwitch(MLSwitch):
    namespace: str

    def __init__(self, climate: Mts200Climate, entitykey: str, namespace: str):
        self._attr_name = entitykey
        super().__init__(
            climate.manager,
            climate.channel,
            entitykey,
            MLSwitch.DeviceClass.SWITCH,
            namespace,
        )

    @property
    def entity_category(self):
        return EntityCategory.CONFIG

    async def async_request_onoff(self, onoff: int):
        def _ack_callback(acknowledge: bool, header: dict, payload: dict):
            if acknowledge:
                self.update_onoff(onoff)

        await self.manager.async_request(
            self.namespace,
            mc.METHOD_SET,
            {
                self.key_namespace: [
                    {
                        self.key_channel: self.channel,
                        self.key_onoff: onoff,
                    }
                ]
            },
            _ack_callback,
        )


class Mts200Climate(MtsClimate):
    """Climate entity for MTS200 devices"""

    MTS_MODE_AUTO = mc.MTS200_MODE_AUTO
    MTS_MODE_TO_PRESET_MAP = {
        mc.MTS200_MODE_CUSTOM: MtsClimate.PRESET_CUSTOM,
        mc.MTS200_MODE_HEAT: MtsClimate.PRESET_COMFORT,
        mc.MTS200_MODE_COOL: MtsClimate.PRESET_SLEEP,
        mc.MTS200_MODE_ECO: MtsClimate.PRESET_AWAY,
        mc.MTS200_MODE_AUTO: MtsClimate.PRESET_AUTO,
    }
    # when setting target temp we'll set an appropriate payload key
    # for the mts100 depending on current 'preset' mode.
    # if mts100 is in any of 'off', 'auto' we just set the 'custom'
    # target temp but of course the valve will not follow
    # this temp since it's mode is not set to follow a manual set
    PRESET_TO_TEMPERATUREKEY_MAP = {
        MtsClimate.PRESET_OFF: mc.KEY_MANUALTEMP,
        MtsClimate.PRESET_CUSTOM: mc.KEY_MANUALTEMP,
        MtsClimate.PRESET_COMFORT: mc.KEY_HEATTEMP,
        MtsClimate.PRESET_SLEEP: mc.KEY_COOLTEMP,
        MtsClimate.PRESET_AWAY: mc.KEY_ECOTEMP,
        MtsClimate.PRESET_AUTO: mc.KEY_MANUALTEMP,
    }

    manager: ThermostatMixin

    __slots__ = (
        "number_comfort_temperature",
        "number_sleep_temperature",
        "number_away_temperature",
        "binary_sensor_windowOpened",
        "switch_sensor_mode",
        "switch_overheat_onoff",
        "sensor_overheat_warning",
        "number_overheat_value",
        "sensor_externalsensor_temperature",
    )

    def __init__(self, manager: ThermostatMixin, channel: object):
        super().__init__(manager, channel)
        # TODO: better cleanup since these circular dependencies
        # prevent proper object release (likely the Mts200SetPointNumber
        # which keeps a reference to this climate)
        self.number_comfort_temperature = Mts200SetPointNumber(
            self, MtsClimate.PRESET_COMFORT
        )
        self.number_sleep_temperature = Mts200SetPointNumber(
            self, MtsClimate.PRESET_SLEEP
        )
        self.number_away_temperature = Mts200SetPointNumber(
            self, MtsClimate.PRESET_AWAY
        )
        self.binary_sensor_windowOpened = MLBinarySensor(
            manager, channel, mc.KEY_WINDOWOPENED, MLBinarySensor.DeviceClass.WINDOW
        )
        # sensor mode: use internal(0) vs external(1) sensor as temperature loopback
        self.switch_sensor_mode = Mts200ConfigSwitch(
            self, "external sensor mode", mc.NS_APPLIANCE_CONTROL_THERMOSTAT_SENSOR
        )
        self.switch_sensor_mode.key_onoff = mc.KEY_MODE
        # overheat protection
        self.switch_overheat_onoff = Mts200ConfigSwitch(
            self, "overheat protection", mc.NS_APPLIANCE_CONTROL_THERMOSTAT_OVERHEAT
        )
        self.sensor_overheat_warning = MLSensor(
            manager, channel, "overheat warning", MLSensor.DeviceClass.ENUM
        )
        self.sensor_overheat_warning._attr_translation_key = "mts200_overheat_warning"
        self.number_overheat_value = Mts200OverheatThresholdNumber(
            manager,
            channel,
        )
        self.sensor_externalsensor_temperature = MLSensor(
            manager, channel, "external sensor", MLSensor.DeviceClass.TEMPERATURE
        )

    async def async_set_preset_mode(self, preset_mode: str):
        if preset_mode == MtsClimate.PRESET_OFF:
            await self.async_request_onoff(0)
        else:
            mode = reverse_lookup(Mts200Climate.MTS_MODE_TO_PRESET_MAP, preset_mode)
            if mode is not None:

                def _ack_callback(acknowledge: bool, header: dict, payload: dict):
                    if acknowledge:
                        self._mts_mode = mode
                        self.update_modes()

                await self.manager.async_request(
                    mc.NS_APPLIANCE_CONTROL_THERMOSTAT_MODE,
                    mc.METHOD_SET,
                    {mc.KEY_MODE: [{mc.KEY_CHANNEL: self.channel, mc.KEY_MODE: mode}]},
                    _ack_callback,
                )

                if not self._mts_onoff:
                    await self.async_request_onoff(1)

    async def async_set_temperature(self, **kwargs):
        t = kwargs[Mts200Climate.ATTR_TEMPERATURE]
        key = Mts200Climate.PRESET_TO_TEMPERATUREKEY_MAP[
            self._attr_preset_mode or Mts200Climate.PRESET_CUSTOM
        ]

        def _ack_callback(acknowledge: bool, header: dict, payload: dict):
            if acknowledge:
                self._attr_target_temperature = t
                self.update_modes()

        await self.manager.async_request(
            mc.NS_APPLIANCE_CONTROL_THERMOSTAT_MODE,
            mc.METHOD_SET,
            {mc.KEY_MODE: [{mc.KEY_CHANNEL: self.channel, key: int(t * 10)}]},
            _ack_callback,
        )

    async def async_request_onoff(self, onoff: int):
        def _ack_callback(acknowledge: bool, header: dict, payload: dict):
            if acknowledge:
                self._mts_onoff = onoff
                self.update_modes()

        await self.manager.async_request(
            mc.NS_APPLIANCE_CONTROL_THERMOSTAT_MODE,
            mc.METHOD_SET,
            {mc.KEY_MODE: [{mc.KEY_CHANNEL: self.channel, mc.KEY_ONOFF: onoff}]},
            _ack_callback,
        )

    def _parse_mode(self, payload: dict):
        """{
            "channel": 0,
            "onoff": 1,
            "mode": 3,
            "state": 0,
            "currentTemp": 210,
            "heatTemp": 240,
            "coolTemp": 210,
            "ecoTemp": 120,
            "manualTemp": 230,
            "warning": 0,
            "targetTemp": 205,
            "min": 50,
            "max": 350,
            "lmTime": 1642425303
        }"""
        if mc.KEY_MODE in payload:
            self._mts_mode = payload[mc.KEY_MODE]
        if mc.KEY_ONOFF in payload:
            self._mts_onoff = payload[mc.KEY_ONOFF]
        if mc.KEY_STATE in payload:
            self._mts_heating = payload[mc.KEY_STATE]
        if isinstance(_t := payload.get(mc.KEY_CURRENTTEMP), int):
            self._attr_current_temperature = _t / 10
        if isinstance(_t := payload.get(mc.KEY_TARGETTEMP), int):
            self._attr_target_temperature = _t / 10
        if isinstance(_t := payload.get(mc.KEY_MIN), int):
            self._attr_min_temp = _t / 10
        if isinstance(_t := payload.get(mc.KEY_MAX), int):
            self._attr_max_temp = _t / 10
        if isinstance(_t := payload.get(mc.KEY_HEATTEMP), int):
            self.number_comfort_temperature.update_native_value(_t)
        if isinstance(_t := payload.get(mc.KEY_COOLTEMP), int):
            self.number_sleep_temperature.update_native_value(_t)
        if isinstance(_t := payload.get(mc.KEY_ECOTEMP), int):
            self.number_away_temperature.update_native_value(_t)
        self.update_modes()

    def _parse_windowOpened(self, payload: dict):
        """{ "channel": 0, "status": 0, "lmTime": 1642425303 }"""
        self.binary_sensor_windowOpened.update_onoff(payload[mc.KEY_STATUS])

    def _parse_sensor(self, payload: dict):
        """{ "channel": 0, "mode": 0 }"""
        self.switch_sensor_mode.update_onoff(payload[mc.KEY_MODE])

    def _parse_overheat(self, payload: dict):
        """{"warning": 0, "value": 335, "onoff": 1, "min": 200, "max": 700,
        "lmTime": 1674121910, "currentTemp": 355, "channel": 0}"""
        if mc.KEY_ONOFF in payload:
            self.switch_overheat_onoff.update_onoff(payload[mc.KEY_ONOFF])
        if mc.KEY_WARNING in payload:
            _warning = payload[mc.KEY_WARNING]
            self.sensor_overheat_warning.update_state(
                mc.MTS200_OVERHEAT_WARNING_MAP.get(_warning, _warning)
            )
        if mc.KEY_MIN in payload:
            self.number_overheat_value._attr_native_min_value = payload[mc.KEY_MIN] / 10
        if mc.KEY_MAX in payload:
            self.number_overheat_value._attr_native_max_value = payload[mc.KEY_MAX] / 10
        if mc.KEY_VALUE in payload:
            self.number_overheat_value.update_native_value(payload[mc.KEY_VALUE])
        if mc.KEY_CURRENTTEMP in payload:
            self.sensor_externalsensor_temperature.update_state(
                payload[mc.KEY_CURRENTTEMP] / 10
            )


class ThermostatMixin(
    MerossDevice if typing.TYPE_CHECKING else object
):  # pylint: disable=used-before-assignment
    _polling_payload: list

    def _init_thermostat(self, payload: dict):
        self._polling_payload = []
        mode = payload.get(mc.KEY_MODE)
        if isinstance(mode, list):
            for m in mode:
                Mts200Climate(self, m[mc.KEY_CHANNEL])
                self._polling_payload.append({mc.KEY_CHANNEL: m[mc.KEY_CHANNEL]})
        if self._polling_payload:
            if mc.NS_APPLIANCE_CONTROL_THERMOSTAT_SENSOR in self.descriptor.ability:
                self.polling_dictionary[
                    mc.NS_APPLIANCE_CONTROL_THERMOSTAT_SENSOR
                ] = SmartPollingStrategy(
                    mc.NS_APPLIANCE_CONTROL_THERMOSTAT_SENSOR,
                    {mc.KEY_SENSOR: self._polling_payload},
                )
            if mc.NS_APPLIANCE_CONTROL_THERMOSTAT_OVERHEAT in self.descriptor.ability:
                self.polling_dictionary[
                    mc.NS_APPLIANCE_CONTROL_THERMOSTAT_OVERHEAT
                ] = SmartPollingStrategy(
                    mc.NS_APPLIANCE_CONTROL_THERMOSTAT_OVERHEAT,
                    {mc.KEY_OVERHEAT: self._polling_payload},
                )

    def _handle_Appliance_Control_Thermostat_Mode(self, header: dict, payload: dict):
        self._parse__generic_array(mc.KEY_MODE, payload[mc.KEY_MODE])

    def _handle_Appliance_Control_Thermostat_Calibration(
        self, header: dict, payload: dict
    ):
        self._parse__generic_array(mc.KEY_CALIBRATION, payload[mc.KEY_CALIBRATION])

    def _handle_Appliance_Control_Thermostat_DeadZone(
        self, header: dict, payload: dict
    ):
        self._parse__generic_array(mc.KEY_DEADZONE, payload[mc.KEY_DEADZONE])

    def _handle_Appliance_Control_Thermostat_Frost(self, header: dict, payload: dict):
        self._parse__generic_array(mc.KEY_FROST, payload[mc.KEY_FROST])

    def _handle_Appliance_Control_Thermostat_Overheat(
        self, header: dict, payload: dict
    ):
        self._parse__generic_array(mc.KEY_OVERHEAT, payload[mc.KEY_OVERHEAT])

    def _handle_Appliance_Control_Thermostat_windowOpened(
        self, header: dict, payload: dict
    ):
        self._parse__generic_array(mc.KEY_WINDOWOPENED, payload[mc.KEY_WINDOWOPENED])

    def _handle_Appliance_Control_Thermostat_Schedule(
        self, header: dict, payload: dict
    ):
        self._parse__generic_array(mc.KEY_SCHEDULE, payload[mc.KEY_SCHEDULE])

    def _handle_Appliance_Control_Thermostat_HoldAction(
        self, header: dict, payload: dict
    ):
        self._parse__generic_array(mc.KEY_HOLDACTION, payload[mc.KEY_HOLDACTION])

    def _handle_Appliance_Control_Thermostat_Sensor(self, header: dict, payload: dict):
        self._parse__generic_array(mc.KEY_SENSOR, payload[mc.KEY_SENSOR])

    def _parse_thermostat(self, payload: dict):
        """
        "thermostat": {
            "mode": [{
                "channel": 0,
                "onoff": 1,
                "mode": 3,
                "state": 0,
                "currentTemp": 210,
                "heatTemp": 240,
                "coolTemp": 210,
                "ecoTemp": 120,
                "manualTemp": 230,
                "warning": 0,
                "targetTemp": 205,
                "min": 50,
                "max": 350,
                "lmTime": 1642425303
            }],
            "windowOpened": [{
                "channel": 0,
                "status": 0,
                "lmTime": 1642425303
            }]
        }
        """
        for key, value in payload.items():
            self._parse__generic_array(key, value)
