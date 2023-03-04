from __future__ import annotations
import typing

from ..climate import (
    MtsClimate, MtsSetPointNumber,
    PRESET_OFF, PRESET_CUSTOM, PRESET_COMFORT, PRESET_SLEEP, PRESET_AWAY, PRESET_AUTO,
    ATTR_TEMPERATURE, TEMP_CELSIUS
)
from ..number import MLConfigNumber
from ..sensor import MLSensor
from ..binary_sensor import MLBinarySensor
from ..switch import MLSwitch
from ..meross_entity import EntityCategory
from ..merossclient import const as mc

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
    _attr_name = 'Overheat threshold'
    _attr_native_max_value = 70
    _attr_native_min_value = 20
    _attr_native_step = 0.5
    _attr_native_unit_of_measurement = TEMP_CELSIUS

    multiplier = 10
    namespace = mc.NS_APPLIANCE_CONTROL_THERMOSTAT_OVERHEAT
    key_namespace = mc.KEY_OVERHEAT
    key_value = mc.KEY_VALUE


class Mts200ConfigSwitch(MLSwitch):

    _attr_entity_category = EntityCategory.CONFIG

    namespace: str

    def __init__(self, climate: Mts200Climate, entitykey: str, namespace: str):
        self._attr_name = entitykey
        super().__init__(climate.device, climate.channel, entitykey, self.DeviceClass.SWITCH, None, namespace)

    async def async_request_onoff(self, onoff: int):

        def _ack_callback(acknowledge: bool, header: dict, payload: dict):
            if acknowledge:
                self.update_onoff(onoff)

        await self.device.async_request(
            self.namespace,
            mc.METHOD_SET,
            {
                self.key_namespace: [{
                    self.key_channel: self.channel,
                    self.key_onoff: onoff,
                }]
            },
            _ack_callback,
        )


class Mts200Climate(MtsClimate):

    MTS_MODE_AUTO = mc.MTS200_MODE_AUTO
    MTS_MODE_TO_PRESET_MAP = {
        mc.MTS200_MODE_CUSTOM: PRESET_CUSTOM,
        mc.MTS200_MODE_HEAT: PRESET_COMFORT,
        mc.MTS200_MODE_COOL: PRESET_SLEEP,
        mc.MTS200_MODE_ECO: PRESET_AWAY,
        mc.MTS200_MODE_AUTO: PRESET_AUTO
    }
    # reverse map
    PRESET_TO_MTS_MODE_MAP = {
        PRESET_CUSTOM: mc.MTS200_MODE_CUSTOM,
        PRESET_COMFORT: mc.MTS200_MODE_HEAT,
        PRESET_SLEEP: mc.MTS200_MODE_COOL,
        PRESET_AWAY: mc.MTS200_MODE_ECO,
        PRESET_AUTO: mc.MTS200_MODE_AUTO
    }
    # when setting target temp we'll set an appropriate payload key
    # for the mts100 depending on current 'preset' mode.
    # if mts100 is in any of 'off', 'auto' we just set the 'custom'
    # target temp but of course the valve will not follow
    # this temp since it's mode is not set to follow a manual set
    PRESET_TO_TEMPERATUREKEY_MAP = {
        PRESET_OFF: mc.KEY_MANUALTEMP,
        PRESET_CUSTOM: mc.KEY_MANUALTEMP,
        PRESET_COMFORT: mc.KEY_HEATTEMP,
        PRESET_SLEEP: mc.KEY_COOLTEMP,
        PRESET_AWAY: mc.KEY_ECOTEMP,
        PRESET_AUTO: mc.KEY_MANUALTEMP
    }

    def __init__(self, device: 'MerossDevice', channel: object):
        super().__init__(device, channel, None, None, None)
        self._comfort_temperature_number = Mts200SetPointNumber(self, PRESET_COMFORT)
        self._sleep_temperature_number = Mts200SetPointNumber(self, PRESET_SLEEP)
        self._away_temperature_number = Mts200SetPointNumber(self, PRESET_AWAY)
        self._windowOpened_binary_sensor = MLBinarySensor(
            device, channel, mc.KEY_WINDOWOPENED, MLBinarySensor.DeviceClass.WINDOW)
        # sensor mode: use internal(0) vs external(1) sensor as temperature loopback
        self._sensorMode_switch = Mts200ConfigSwitch(
            self, 'external sensor mode', mc.NS_APPLIANCE_CONTROL_THERMOSTAT_SENSOR)
        self._sensorMode_switch.key_onoff = mc.KEY_MODE
        # overheat protection
        self._overheatonoff_switch = Mts200ConfigSwitch(
            self, 'overheat protection', mc.NS_APPLIANCE_CONTROL_THERMOSTAT_OVERHEAT)
        self._overheatwarning_binary_sensor = MLBinarySensor(
            device, channel, 'overheat warning', MLBinarySensor.DeviceClass.PROBLEM)
        self._overheatvalue_number = Mts200OverheatThresholdNumber(
            device, channel, 'overheat threshold', MLConfigNumber.DeviceClass.TEMPERATURE)
        self._externalsensor_temperature_sensor = MLSensor(
            device, channel, 'external sensor', MLSensor.DeviceClass.TEMPERATURE, None)

    async def async_set_preset_mode(self, preset_mode: str):
        if preset_mode == PRESET_OFF:
            await self.async_request_onoff(0)
        else:
            mode = self.PRESET_TO_MTS_MODE_MAP.get(preset_mode)
            if mode is not None:

                def _ack_callback(acknowledge: bool, header: dict, payload: dict):
                    if acknowledge:
                        self._mts_mode = mode
                        self.update_modes()

                await self.device.async_request(
                    mc.NS_APPLIANCE_CONTROL_THERMOSTAT_MODE,
                    mc.METHOD_SET,
                    {mc.KEY_MODE: [{mc.KEY_CHANNEL: self.channel, mc.KEY_MODE: mode}]},
                    _ack_callback
                )

                if not self._mts_onoff:
                    await self.async_request_onoff(1)

    async def async_set_temperature(self, **kwargs):
        t = kwargs[ATTR_TEMPERATURE]
        key = self.PRESET_TO_TEMPERATUREKEY_MAP[self._attr_preset_mode or PRESET_CUSTOM]

        def _ack_callback(acknowledge: bool, header: dict, payload: dict):
            if acknowledge:
                self._attr_target_temperature = t
                self.update_modes()

        await self.device.async_request(
            mc.NS_APPLIANCE_CONTROL_THERMOSTAT_MODE,
            mc.METHOD_SET,
            {mc.KEY_MODE: [{mc.KEY_CHANNEL: self.channel, key: int(t * 10)}]},
            _ack_callback
        )

    async def async_request_onoff(self, onoff: int):

        def _ack_callback(acknowledge: bool, header: dict, payload: dict):
            if acknowledge:
                self._mts_onoff = onoff
                self.update_modes()

        await self.device.async_request(
            mc.NS_APPLIANCE_CONTROL_THERMOSTAT_MODE,
            mc.METHOD_SET,
            {mc.KEY_MODE: [{mc.KEY_CHANNEL: self.channel, mc.KEY_ONOFF: onoff}]},
            _ack_callback
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
            self._comfort_temperature_number.update_native_value(_t)
        if isinstance(_t := payload.get(mc.KEY_COOLTEMP), int):
            self._sleep_temperature_number.update_native_value(_t)
        if isinstance(_t := payload.get(mc.KEY_ECOTEMP), int):
            self._away_temperature_number.update_native_value(_t)
        self.update_modes()

    def _parse_windowOpened(self, payload: dict):
        """{ "channel": 0, "status": 0, "lmTime": 1642425303 }"""
        self._windowOpened_binary_sensor.update_onoff(payload.get(mc.KEY_STATUS))

    def _parse_sensor(self, payload: dict):
        """{ "channel": 0, "mode": 0 }"""
        self._sensorMode_switch.update_onoff(payload.get(mc.KEY_MODE))

    def _parse_overheat(self, payload: dict):
        """{"warning": 0, "value": 335, "onoff": 1, "min": 200, "max": 700,
            "lmTime": 1674121910, "currentTemp": 355, "channel": 0}"""
        if mc.KEY_ONOFF in payload:
            self._overheatonoff_switch.update_onoff(payload[mc.KEY_ONOFF])
        if mc.KEY_WARNING in payload:
            self._overheatwarning_binary_sensor.update_onoff(payload[mc.KEY_WARNING])
        if mc.KEY_MIN in payload:
            self._overheatvalue_number._attr_native_min_value = payload[mc.KEY_MIN] / 10
        if mc.KEY_MAX in payload:
            self._overheatvalue_number._attr_native_max_value = payload[mc.KEY_MAX] / 10
        if mc.KEY_VALUE in payload:
            self._overheatvalue_number.update_native_value(payload[mc.KEY_VALUE])
        if mc.KEY_CURRENTTEMP in payload:
            self._externalsensor_temperature_sensor.update_state(payload[mc.KEY_CURRENTTEMP] / 10)


class ThermostatMixin(MerossDevice if typing.TYPE_CHECKING else object): # pylint: disable=used-before-assignment

    _polling_payload: list

    def _init_thermostat(self, payload: dict):
        self._polling_payload = []
        mode = payload.get(mc.KEY_MODE)
        if isinstance(mode, list):
            for m in mode:
                Mts200Climate(self, m[mc.KEY_CHANNEL])
                self._polling_payload.append({ mc.KEY_CHANNEL: m[mc.KEY_CHANNEL] })
        if self._polling_payload:
            if mc.NS_APPLIANCE_CONTROL_THERMOSTAT_SENSOR in self.descriptor.ability:
                self.polling_dictionary[mc.NS_APPLIANCE_CONTROL_THERMOSTAT_SENSOR] = \
                    { mc.KEY_SENSOR: self._polling_payload }
            if mc.NS_APPLIANCE_CONTROL_THERMOSTAT_OVERHEAT in self.descriptor.ability:
                self.polling_dictionary[mc.NS_APPLIANCE_CONTROL_THERMOSTAT_OVERHEAT] = \
                    { mc.KEY_OVERHEAT: self._polling_payload }

    def _handle_Appliance_Control_Thermostat_Mode(self, header: dict, payload: dict):
        self._parse__generic_array(mc.KEY_MODE, payload.get(mc.KEY_MODE))

    def _handle_Appliance_Control_Thermostat_Calibration(self, header: dict, payload: dict):
        self._parse__generic_array(mc.KEY_CALIBRATION, payload.get(mc.KEY_CALIBRATION))

    def _handle_Appliance_Control_Thermostat_DeadZone(self, header: dict, payload: dict):
        self._parse__generic_array(mc.KEY_DEADZONE, payload.get(mc.KEY_DEADZONE))

    def _handle_Appliance_Control_Thermostat_Frost(self, header: dict, payload: dict):
        self._parse__generic_array(mc.KEY_FROST, payload.get(mc.KEY_FROST))

    def _handle_Appliance_Control_Thermostat_Overheat(self, header: dict, payload: dict):
        self._parse__generic_array(mc.KEY_OVERHEAT, payload.get(mc.KEY_OVERHEAT))

    def _handle_Appliance_Control_Thermostat_windowOpened(self, header: dict, payload: dict):
        self._parse__generic_array(mc.KEY_WINDOWOPENED, payload.get(mc.KEY_WINDOWOPENED))

    def _handle_Appliance_Control_Thermostat_Schedule(self, header: dict, payload: dict):
        self._parse__generic_array(mc.KEY_SCHEDULE, payload.get(mc.KEY_SCHEDULE))

    def _handle_Appliance_Control_Thermostat_HoldAction(self, header: dict, payload: dict):
        self._parse__generic_array(mc.KEY_HOLDACTION, payload.get(mc.KEY_HOLDACTION))

    def _handle_Appliance_Control_Thermostat_Sensor(self, header: dict, payload: dict):
        self._parse__generic_array(mc.KEY_SENSOR, payload.get(mc.KEY_SENSOR))

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
