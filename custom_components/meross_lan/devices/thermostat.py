from __future__ import annotations

import typing

from ..binary_sensor import MLBinarySensor
from ..helpers.namespaces import PollingStrategy, SmartPollingStrategy
from ..merossclient import const as mc
from ..number import MtsTemperatureNumber
from ..sensor import MLEnumSensor, MLNumericSensor, MLTemperatureSensor
from ..switch import MtsConfigSwitch
from .mts200 import Mts200Climate
from .mts960 import Mts960Climate

if typing.TYPE_CHECKING:
    from typing import ClassVar, Final

    from ..meross_device import MerossDevice

    MtsThermostatClimate = Mts200Climate | Mts960Climate


class MtsRichTemperatureNumber(MtsTemperatureNumber):
    """
    Slightly enriched MtsTemperatureNumber to generalize  a lot of Thermostat namespaces
    which usually carry a temperature value together with some added entities (typically a switch
    to enable the feature and a 'warning sensor')
    typical examples are :
    "calibration": {"channel": 0, "value": 0, "min": -80, "max": 80, "lmTime": 1697010767}
    "deadZone":
    "frost": {"channel": 0, "onoff": 1, "value": 500, "min": 500, "max": 1500, "warning": 0}
    "overheat": {"warning": 0, "value": 335, "onoff": 1, "min": 200, "max": 700,
        "lmTime": 1674121910, "currentTemp": 355, "channel": 0}
    """

    entitykey: str
    key_value: Final = mc.KEY_VALUE

    __slots__ = (
        "sensor_warning",
        "switch",
        "native_max_value",
        "native_min_value",
        "native_step",
    )

    def __init__(self, climate: MtsThermostatClimate, entitykey: str):
        super().__init__(climate, entitykey)
        manager = self.manager
        # preset entity platforms since these might be instantiated later
        manager.platforms.setdefault(MtsConfigSwitch.PLATFORM)
        manager.platforms.setdefault(MLNumericSensor.PLATFORM)
        self.sensor_warning = None
        self.switch = None
        manager.register_parser(self.namespace, self)

    async def async_shutdown(self):
        self.switch = None
        self.sensor_warning = None
        await super().async_shutdown()

    def _parse(self, payload: dict):
        """
        {"channel": 0, "value": 0, "min": -80, "max": 80, "lmTime": 1697010767}
        """
        if mc.KEY_MIN in payload:
            self.native_min_value = payload[mc.KEY_MIN] / self.device_scale
        if mc.KEY_MAX in payload:
            self.native_max_value = payload[mc.KEY_MAX] / self.device_scale
        self.update_device_value(payload[self.key_value])
        if mc.KEY_ONOFF in payload:
            # on demand instance
            try:
                self.switch.update_onoff(payload[mc.KEY_ONOFF])  # type: ignore
            except AttributeError:
                self.switch = MtsConfigSwitch(
                    self.climate,
                    f"{self.entitykey}_switch",
                    onoff=payload[mc.KEY_ONOFF],
                    namespace=self.namespace,
                )
        if mc.KEY_WARNING in payload:
            # on demand instance
            try:
                self.sensor_warning.update_native_value(payload[mc.KEY_WARNING])  # type: ignore
            except AttributeError:
                self.sensor_warning = sensor_warning = MLEnumSensor(
                    self.manager,
                    self.channel,
                    f"{self.entitykey}_warning",
                    native_value=payload[mc.KEY_WARNING],
                )
                sensor_warning.translation_key = f"mts_{sensor_warning.entitykey}"


class MtsCalibrationNumber(MtsRichTemperatureNumber):
    """
    Adjust temperature readings for mts200 and mts960.
    Manages Appliance.Control.Thermostat.Calibration:
    {"channel": 0, "value": 0, "min": -80, "max": 80, "lmTime": 1697010767}
    """

    namespace = mc.NS_APPLIANCE_CONTROL_THERMOSTAT_CALIBRATION
    key_namespace = mc.KEY_CALIBRATION

    def __init__(self, climate: MtsThermostatClimate):
        self.name = "Calibration"
        self.native_max_value = 8
        self.native_min_value = -8
        self.native_step = 0.1
        super().__init__(climate, mc.KEY_CALIBRATION)


class MtsDeadZoneNumber(MtsRichTemperatureNumber):
    """
    adjust "dead zone" i.e. the threshold for the temperature control
    for mts200 and mts960 or whatever carries the Appliance.Control.Thermostat.DeadZone
    The min/max values are different between the two devices but the deadZone
    payload will carry the values and so set them
    """

    namespace = mc.NS_APPLIANCE_CONTROL_THERMOSTAT_DEADZONE
    key_namespace = mc.KEY_DEADZONE

    def __init__(self, climate: MtsThermostatClimate):
        self.native_max_value = 3.5
        self.native_min_value = 0.5
        self.native_step = 0.1
        super().__init__(climate, self.key_namespace)


class MtsFrostNumber(MtsRichTemperatureNumber):
    """
    Manages Appliance.Control.Thermostat.Frost:
    {"channel": 0, "onoff": 1, "value": 500, "min": 500, "max": 1500, "warning": 0}
    """

    namespace = mc.NS_APPLIANCE_CONTROL_THERMOSTAT_FROST
    key_namespace = mc.KEY_FROST

    def __init__(self, climate: MtsThermostatClimate):
        self.native_max_value = 15
        self.native_min_value = 5
        self.native_step = climate.target_temperature_step
        super().__init__(climate, self.key_namespace)


class MtsOverheatNumber(MtsRichTemperatureNumber):
    """
    Configure overheat protection.
    Manages Appliance.Control.Thermostat.Overheat:
    {"warning": 0, "value": 335, "onoff": 1, "min": 200, "max": 700,
        "lmTime": 1674121910, "currentTemp": 355, "channel": 0}
    """

    namespace = mc.NS_APPLIANCE_CONTROL_THERMOSTAT_OVERHEAT
    key_namespace = mc.KEY_OVERHEAT

    __slots__ = ("sensor_external_temperature",)

    def __init__(self, climate: MtsThermostatClimate):
        self.name = "Overheat threshold"
        self.native_max_value = 70
        self.native_min_value = 20
        self.native_step = climate.target_temperature_step
        super().__init__(climate, self.key_namespace)
        self.sensor_external_temperature = MLTemperatureSensor(
            self.manager, self.channel, "external sensor"
        )

    async def async_shutdown(self):
        self.sensor_external_temperature: MLNumericSensor = None  # type: ignore
        return await super().async_shutdown()

    def _parse(self, payload: dict):
        """{"warning": 0, "value": 335, "onoff": 1, "min": 200, "max": 700,
        "lmTime": 1674121910, "currentTemp": 355, "channel": 0}"""
        super()._parse(payload)
        if mc.KEY_CURRENTTEMP in payload:
            self.sensor_external_temperature.update_native_value(
                payload[mc.KEY_CURRENTTEMP] / self.device_scale
            )


class MtsWindowOpened(MLBinarySensor):
    """specialized binary sensor for Thermostat.WindowOpened entity used in Mts200-Mts960(maybe)."""

    key_value = mc.KEY_STATUS

    def __init__(self, climate: MtsThermostatClimate):
        super().__init__(
            climate.manager,
            climate.channel,
            mc.KEY_WINDOWOPENED,
            MLBinarySensor.DeviceClass.WINDOW,
        )
        climate.manager.register_parser(
            mc.NS_APPLIANCE_CONTROL_THERMOSTAT_WINDOWOPENED,
            self,
        )

    def _parse(self, payload: dict):
        """{ "channel": 0, "status": 0, "detect": 1, "lmTime": 1642425303 }"""
        self.update_onoff(payload[mc.KEY_STATUS])


class MtsExternalSensorSwitch(MtsConfigSwitch):
    """sensor mode: use internal(0) vs external(1) sensor as temperature loopback."""

    key_value = mc.KEY_MODE

    def __init__(self, climate: MtsThermostatClimate):
        super().__init__(
            climate,
            "external sensor mode",
            namespace=mc.NS_APPLIANCE_CONTROL_THERMOSTAT_SENSOR,
        )
        climate.manager.register_parser(mc.NS_APPLIANCE_CONTROL_THERMOSTAT_SENSOR, self)


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

    AdjustNumberClass = MtsCalibrationNumber

    CLIMATE_INITIALIZERS: ClassVar[dict[str, type[MtsThermostatClimate]]] = {
        mc.KEY_MODE: Mts200Climate,
        mc.KEY_MODEB: Mts960Climate,
    }
    """Core (climate) entities to initialize in _init_thermostat"""

    DIGEST_KEY_TO_NAMESPACE: ClassVar[dict[str, str]] = {
        mc.KEY_MODE: mc.NS_APPLIANCE_CONTROL_THERMOSTAT_MODE,
        mc.KEY_MODEB: mc.NS_APPLIANCE_CONTROL_THERMOSTAT_MODEB,
        mc.KEY_SUMMERMODE: mc.NS_APPLIANCE_CONTROL_THERMOSTAT_SUMMERMODE,
        mc.KEY_WINDOWOPENED: mc.NS_APPLIANCE_CONTROL_THERMOSTAT_WINDOWOPENED,
    }
    """Maps the digest key to the associated namespace handler (used in _parse_thermostat)"""

    OPTIONAL_NAMESPACES_INITIALIZERS = {
        mc.NS_APPLIANCE_CONTROL_THERMOSTAT_HOLDACTION,
        mc.NS_APPLIANCE_CONTROL_THERMOSTAT_SUMMERMODE,
    }
    """These namespaces handlers will forward message parsing to the climate entity"""

    OPTIONAL_ENTITIES_INITIALIZERS: dict[
        str, typing.Callable[[MtsThermostatClimate], typing.Any]
    ] = {
        mc.NS_APPLIANCE_CONTROL_THERMOSTAT_DEADZONE: MtsDeadZoneNumber,
        mc.NS_APPLIANCE_CONTROL_THERMOSTAT_FROST: MtsFrostNumber,
        mc.NS_APPLIANCE_CONTROL_THERMOSTAT_OVERHEAT: MtsOverheatNumber,
        mc.NS_APPLIANCE_CONTROL_THERMOSTAT_SENSOR: MtsExternalSensorSwitch,
        mc.NS_APPLIANCE_CONTROL_THERMOSTAT_WINDOWOPENED: MtsWindowOpened,
    }
    """Additional entities (linked to the climate one) in case their ns is supported/available"""

    POLLING_STRATEGY_INITIALIZERS = {
        mc.NS_APPLIANCE_CONTROL_THERMOSTAT_CALIBRATION: SmartPollingStrategy,
        mc.NS_APPLIANCE_CONTROL_THERMOSTAT_DEADZONE: SmartPollingStrategy,
        mc.NS_APPLIANCE_CONTROL_THERMOSTAT_FROST: SmartPollingStrategy,
        mc.NS_APPLIANCE_CONTROL_THERMOSTAT_OVERHEAT: PollingStrategy,
        mc.NS_APPLIANCE_CONTROL_THERMOSTAT_SCHEDULE: PollingStrategy,
        mc.NS_APPLIANCE_CONTROL_THERMOSTAT_SCHEDULEB: PollingStrategy,
        mc.NS_APPLIANCE_CONTROL_THERMOSTAT_SENSOR: PollingStrategy,
    }
    """
    "Mode", "ModeB","SummerMode","WindowOpened" are carried in digest so we don't poll them
    We're using PollingStrategy for namespaces actually confirmed (by trace/diagnostics)
    to be PUSHED when over MQTT. The rest are either 'never seen' or 'not pushed'
    """

    # interface: MerossDevice
    def _init_thermostat(self, digest: dict):
        ability = self.descriptor.ability
        self._polling_payload = []

        for ns_key, ns_digest in digest.items():
            if climate_class := self.CLIMATE_INITIALIZERS.get(ns_key):
                for channel_digest in ns_digest:
                    channel = channel_digest[mc.KEY_CHANNEL]
                    climate = climate_class(self, channel)
                    self.register_parser(climate.namespace, climate)
                    schedule = climate.schedule
                    # TODO: the scheduleB parsing might be different than 'classic' schedule
                    self.register_parser(schedule.namespace, schedule)
                    for ns in self.OPTIONAL_NAMESPACES_INITIALIZERS:
                        if ns in ability:
                            self.register_parser(ns, climate)

                    for ns, entity_class in self.OPTIONAL_ENTITIES_INITIALIZERS.items():
                        if ns in ability:
                            entity_class(climate)

                    self._polling_payload.append({mc.KEY_CHANNEL: channel})

        for ns, polling_strategy_class in self.POLLING_STRATEGY_INITIALIZERS.items():
            if ns in ability:
                polling_strategy_class(
                    self,
                    ns,
                    payload=self._polling_payload,
                    item_count=len(self._polling_payload),
                )

    def _parse_thermostat(self, digest: dict):
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
        for ns_key, ns_digest in digest.items():
            self.namespace_handlers[self.DIGEST_KEY_TO_NAMESPACE[ns_key]]._parse_list(
                ns_digest
            )
