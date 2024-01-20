from __future__ import annotations

import typing

from ..binary_sensor import MLBinarySensor
from ..climate import MtsClimate
from ..helpers import PollingStrategy, SmartPollingStrategy
from ..merossclient import const as mc
from ..number import MtsRichTemperatureNumber
from ..sensor import MLSensor
from .mts200 import Mts200Climate
from .mts960 import Mts960Climate

if typing.TYPE_CHECKING:
    from ..meross_device import MerossDevice


class MtsDeadZoneNumber(MtsRichTemperatureNumber):
    """
    adjust "dead zone" i.e. the threshold for the temperature control
    for mts200 and mts960 or whatever carries the Appliance.Control.Thermostat.DeadZone
    The min/max values are different between the two devices but the deadZone
    payload will carry the values and so set them
    """

    namespace = mc.NS_APPLIANCE_CONTROL_THERMOSTAT_DEADZONE
    key_namespace = mc.KEY_DEADZONE
    key_value = mc.KEY_VALUE

    def __init__(self, climate: MtsClimate):
        self._attr_native_max_value = 3.5
        self._attr_native_min_value = 0.5
        super().__init__(climate, self.key_namespace)

    @property
    def native_step(self):
        return 0.1

    def _parse_deadZone(self, payload: dict):
        self._parse_value(payload)


class MtsFrostNumber(MtsRichTemperatureNumber):
    """
    adjust "frost": dunno what it is for
    """

    namespace = mc.NS_APPLIANCE_CONTROL_THERMOSTAT_FROST
    key_namespace = mc.KEY_FROST
    key_value = mc.KEY_VALUE

    def __init__(self, climate: MtsClimate):
        self._attr_native_max_value = 15
        self._attr_native_min_value = 5
        super().__init__(climate, self.key_namespace)

    @property
    def native_step(self):
        return self.climate.target_temperature_step

    def _parse_frost(self, payload: dict):
        """{"channel": 0, "onoff": 1, "value": 500, "min": 500, "max": 1500, "warning": 0}"""
        self._parse_value(payload)


class MtsOverheatNumber(MtsRichTemperatureNumber):
    """Configure overheat protection value"""

    _attr_name = "Overheat threshold"

    namespace = mc.NS_APPLIANCE_CONTROL_THERMOSTAT_OVERHEAT
    key_namespace = mc.KEY_OVERHEAT
    key_value = mc.KEY_VALUE

    __slots__ = ("sensor_external_temperature",)

    def __init__(self, climate: MtsClimate):
        self._attr_native_max_value = 70
        self._attr_native_min_value = 20
        super().__init__(climate, self.key_namespace)
        self.sensor_external_temperature = MLSensor(
            self.manager,
            self.channel,
            "external sensor",
            MLSensor.DeviceClass.TEMPERATURE,
        )

    async def async_shutdown(self):
        self.sensor_external_temperature: MLSensor = None  # type: ignore
        return await super().async_shutdown()

    @property
    def native_step(self):
        return 0.5

    def _parse_overheat(self, payload: dict):
        """{"warning": 0, "value": 335, "onoff": 1, "min": 200, "max": 700,
        "lmTime": 1674121910, "currentTemp": 355, "channel": 0}"""
        self._parse_value(payload)
        if mc.KEY_CURRENTTEMP in payload:
            self.sensor_external_temperature.update_state(
                payload[mc.KEY_CURRENTTEMP] / self.device_scale
            )


class MtsWindowOpened(MLBinarySensor):
    """specialized binary sensor for Thermostat.WindowOpened entity used in Mts200-Mts960(maybe)."""

    def __init__(self, climate: MtsClimate):
        super().__init__(
            climate.manager,
            climate.channel,
            mc.KEY_WINDOWOPENED,
            MLBinarySensor.DeviceClass.WINDOW,
        )

    def _parse_windowOpened(self, payload: dict):
        """{ "channel": 0, "status": 0, "detect": 1, "lmTime": 1642425303 }"""
        self.update_onoff(payload[mc.KEY_STATUS])


class ThermostatMixin(
    MerossDevice if typing.TYPE_CHECKING else object
):  # pylint: disable=used-before-assignment
    """
    ThermostatMixin was historically used for mts200 (and the likes) and
    most of its logic were so implemented in Mts200Climate. We now have a new
    device (mts960) implementing this ns. The first observed difference lies in
    the "mode" key (together with "summerMode"-"windowOpened") which is substituted
    with "modeB" to carry the new device layout. We'll so try to generalize some
    of the namespace handling to this mixin (which is what it's for) while not
    breaking the mts200
    """

    CLIMATE_INITIALIZERS = {mc.KEY_MODE: Mts200Climate, mc.KEY_MODEB: Mts960Climate}

    # interface: self
    def _init_thermostat(self, payload: dict):
        self._polling_payload = []
        ability = self.descriptor.ability

        for ns_key, ns_payload in payload.items():
            if climate_class := self.CLIMATE_INITIALIZERS.get(ns_key):
                for channel_payload in ns_payload:
                    channel = channel_payload[mc.KEY_CHANNEL]
                    climate = climate_class(self, channel)
                    if mc.NS_APPLIANCE_CONTROL_THERMOSTAT_DEADZONE in ability:
                        MtsDeadZoneNumber(climate)
                    if mc.NS_APPLIANCE_CONTROL_THERMOSTAT_FROST in ability:
                        MtsFrostNumber(climate)
                    if mc.NS_APPLIANCE_CONTROL_THERMOSTAT_OVERHEAT in ability:
                        MtsOverheatNumber(climate)
                    if mc.NS_APPLIANCE_CONTROL_THERMOSTAT_WINDOWOPENED in ability:
                        MtsWindowOpened(climate)
                    self._polling_payload.append({mc.KEY_CHANNEL: channel})

        if channel_count := len(self._polling_payload):
            """
            "Mode", "ModeB","SummerMode","WindowOpened" are carried in digest so we don't poll them
            We're using PollingStrategy for namespaces actually confirmed (by trace/diagnstics)
            to be PUSHED when over MQTT. The rest are either 'never seen' or 'not pushed'
            """
            if mc.NS_APPLIANCE_CONTROL_THERMOSTAT_CALIBRATION in ability:
                SmartPollingStrategy(
                    self,
                    mc.NS_APPLIANCE_CONTROL_THERMOSTAT_CALIBRATION,
                    payload={mc.KEY_CALIBRATION: self._polling_payload},
                    item_count=channel_count,
                )
            if mc.NS_APPLIANCE_CONTROL_THERMOSTAT_DEADZONE in ability:
                SmartPollingStrategy(
                    self,
                    mc.NS_APPLIANCE_CONTROL_THERMOSTAT_DEADZONE,
                    payload={mc.KEY_DEADZONE: self._polling_payload},
                    item_count=channel_count,
                )
            if mc.NS_APPLIANCE_CONTROL_THERMOSTAT_FROST in ability:
                SmartPollingStrategy(
                    self,
                    mc.NS_APPLIANCE_CONTROL_THERMOSTAT_FROST,
                    payload={mc.KEY_FROST: self._polling_payload},
                    item_count=channel_count,
                )
            if mc.NS_APPLIANCE_CONTROL_THERMOSTAT_OVERHEAT in ability:
                PollingStrategy(
                    self,
                    mc.NS_APPLIANCE_CONTROL_THERMOSTAT_OVERHEAT,
                    payload={mc.KEY_OVERHEAT: self._polling_payload},
                    item_count=channel_count,
                )
            if mc.NS_APPLIANCE_CONTROL_THERMOSTAT_SCHEDULE in ability:
                PollingStrategy(
                    self,
                    mc.NS_APPLIANCE_CONTROL_THERMOSTAT_SCHEDULE,
                    payload={mc.KEY_SCHEDULE: self._polling_payload},
                    item_count=channel_count,
                )
            if mc.NS_APPLIANCE_CONTROL_THERMOSTAT_SCHEDULEB in ability:
                PollingStrategy(
                    self,
                    mc.NS_APPLIANCE_CONTROL_THERMOSTAT_SCHEDULEB,
                    payload={mc.KEY_SCHEDULEB: self._polling_payload},
                    item_count=channel_count,
                )
            if mc.NS_APPLIANCE_CONTROL_THERMOSTAT_SENSOR in ability:
                PollingStrategy(
                    self,
                    mc.NS_APPLIANCE_CONTROL_THERMOSTAT_SENSOR,
                    payload={mc.KEY_SENSOR: self._polling_payload},
                    item_count=channel_count,
                )

    def _handle_Appliance_Control_Thermostat_Calibration(self, header, payload):
        self._parse__array_key(
            mc.KEY_CALIBRATION, payload[mc.KEY_CALIBRATION], mc.KEY_CALIBRATION
        )

    def _handle_Appliance_Control_Thermostat_DeadZone(self, header, payload):
        self._parse__array_key(
            mc.KEY_DEADZONE, payload[mc.KEY_DEADZONE], mc.KEY_DEADZONE
        )

    def _handle_Appliance_Control_Thermostat_Frost(self, header, payload):
        self._parse__array_key(mc.KEY_FROST, payload[mc.KEY_FROST], mc.KEY_FROST)

    def _handle_Appliance_Control_Thermostat_HoldAction(self, header, payload):
        self._parse__array(mc.KEY_HOLDACTION, payload[mc.KEY_HOLDACTION])

    def _handle_Appliance_Control_Thermostat_Mode(self, header, payload):
        self._parse__array(mc.KEY_MODE, payload[mc.KEY_MODE])

    def _handle_Appliance_Control_Thermostat_ModeB(self, header, payload):
        self._parse__array(mc.KEY_MODEB, payload[mc.KEY_MODEB])

    def _handle_Appliance_Control_Thermostat_Overheat(self, header, payload):
        self._parse__array_key(
            mc.KEY_OVERHEAT, payload[mc.KEY_OVERHEAT], mc.KEY_OVERHEAT
        )

    def _handle_Appliance_Control_Thermostat_Schedule(self, header, payload):
        self._parse__array_key(
            mc.KEY_SCHEDULE, payload[mc.KEY_SCHEDULE], mc.KEY_SCHEDULE
        )

    def _handle_Appliance_Control_Thermostat_ScheduleB(self, header, payload):
        self._parse__array_key(
            mc.KEY_SCHEDULEB, payload[mc.KEY_SCHEDULEB], mc.KEY_SCHEDULEB
        )

    def _handle_Appliance_Control_Thermostat_Sensor(self, header, payload):
        self._parse__array(mc.KEY_SENSOR, payload[mc.KEY_SENSOR])

    def _handle_Appliance_Control_Thermostat_SummerMode(self, header, payload):
        self._parse__array(mc.KEY_SUMMERMODE, payload[mc.KEY_SUMMERMODE])

    def _handle_Appliance_Control_Thermostat_WindowOpened(self, header, payload):
        self._parse__array_key(
            mc.KEY_WINDOWOPENED, payload[mc.KEY_WINDOWOPENED], mc.KEY_WINDOWOPENED
        )

    def _parse_thermostat(self, payload: dict):
        """
        Parser for thermostat digest in NS_ALL
        MTS200 typically carries:
        "thermostat": {
            "mode": [...],
            "summerMode": [],
            "windowOpened": []
        }
        MTS960 typically carries:
        "thermostat": {
            "modeB": [...]
        }
        """
        for key, value in payload.items():
            match key:
                case "windowOpened":
                    self._parse__array_key(key, value, key)
                case _:
                    self._parse__array(key, value)
