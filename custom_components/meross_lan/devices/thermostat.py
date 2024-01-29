from __future__ import annotations

import typing

from ..binary_sensor import MLBinarySensor
from ..climate import MtsClimate
from ..helpers.namespaces import PollingStrategy, SmartPollingStrategy
from ..merossclient import KEY_TO_NAMESPACE, const as mc
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


class MtsFrostNumber(MtsRichTemperatureNumber):
    """
    Manages Appliance.Control.Thermostat.Frost:
    {"channel": 0, "onoff": 1, "value": 500, "min": 500, "max": 1500, "warning": 0}
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


class MtsOverheatNumber(MtsRichTemperatureNumber):
    """
    Configure overheat protection.
    Manages Appliance.Control.Thermostat.Overheat:
    {"warning": 0, "value": 335, "onoff": 1, "min": 200, "max": 700,
        "lmTime": 1674121910, "currentTemp": 355, "channel": 0}
    """

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

    def _parse(self, payload: dict):
        """{"warning": 0, "value": 335, "onoff": 1, "min": 200, "max": 700,
        "lmTime": 1674121910, "currentTemp": 355, "channel": 0}"""
        super()._parse(payload)
        if mc.KEY_CURRENTTEMP in payload:
            self.sensor_external_temperature.update_state(
                payload[mc.KEY_CURRENTTEMP] / self.device_scale
            )


class MtsWindowOpened(MLBinarySensor):
    """specialized binary sensor for Thermostat.WindowOpened entity used in Mts200-Mts960(maybe)."""

    manager: ThermostatMixin

    def __init__(self, climate: MtsClimate):
        super().__init__(
            climate.manager,
            climate.channel,
            mc.KEY_WINDOWOPENED,
            MLBinarySensor.DeviceClass.WINDOW,
        )
        self.manager.register_parser(
            mc.NS_APPLIANCE_CONTROL_THERMOSTAT_WINDOWOPENED,
            self,
            self._parse_windowOpened,
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

    CLIMATE_INITIALIZERS: dict[str, type[Mts200Climate | Mts960Climate]] = {
        mc.KEY_MODE: Mts200Climate,
        mc.KEY_MODEB: Mts960Climate,
    }
    """Core (climate) entities to initialize in _init_thermostat"""

    OPTIONAL_NAMESPACES_INITIALIZERS = {
        mc.NS_APPLIANCE_CONTROL_THERMOSTAT_HOLDACTION,
        mc.NS_APPLIANCE_CONTROL_THERMOSTAT_SENSOR,
        mc.NS_APPLIANCE_CONTROL_THERMOSTAT_SUMMERMODE,
    }
    """These namespaces handlers will forward message parsing to the climate entity"""

    OPTIONAL_ENTITIES_INITIALIZERS: dict[
        str, typing.Callable[[MtsClimate], typing.Any]
    ] = {
        mc.NS_APPLIANCE_CONTROL_THERMOSTAT_DEADZONE: MtsDeadZoneNumber,
        mc.NS_APPLIANCE_CONTROL_THERMOSTAT_FROST: MtsFrostNumber,
        mc.NS_APPLIANCE_CONTROL_THERMOSTAT_OVERHEAT: MtsOverheatNumber,
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
        # we (might) have an issue here since the entities need to be initialized after
        # the (eventual) namespace polling strategy has been set in order for
        # those entities to correctly register the _parse callback.
        # So we're initializing the polling without knowing the actual channel number
        # Anyway, since we're passing in the ref to self._polling_payload it will
        # correctly be updated while creating the entities for the channel. At the
        # moment the only 'bug' could be the wrong item_count (and so the estimated
        # response payload size used to actually determine how to pack requests)
        # It shouldn't really be criticcal anyway since there are a lot of protections
        channel_count = 1  # TODO: update item_count in polling strategy
        self._polling_payload = []
        for ns, polling_strategy_class in self.POLLING_STRATEGY_INITIALIZERS.items():
            if ns in ability:
                polling_strategy_class(
                    self,
                    ns,
                    payload=self._polling_payload,
                    item_count=channel_count,
                )

        for ns_key, ns_digest in digest.items():
            if climate_class := self.CLIMATE_INITIALIZERS.get(ns_key):
                for channel_digest in ns_digest:
                    channel = channel_digest[mc.KEY_CHANNEL]
                    climate = climate_class(self, channel)
                    self.register_parser(
                        climate.namespace,
                        climate,
                        climate._parse,
                    )
                    schedule = climate.schedule
                    # TODO: the scheduleB parsing might be different than 'classic' schedule
                    self.register_parser(
                        schedule.namespace,
                        schedule,
                        schedule._parse,
                    )
                    for ns in self.OPTIONAL_NAMESPACES_INITIALIZERS:
                        if ns in ability:
                            self.register_parser(
                                ns,
                                climate,
                            )

                    for ns, entity_class in self.OPTIONAL_ENTITIES_INITIALIZERS.items():
                        if ns in ability:
                            entity_class(climate)

                    self._polling_payload.append({mc.KEY_CHANNEL: channel})

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
            self.namespace_handlers[KEY_TO_NAMESPACE[ns_key]]._parse_list(ns_digest)
