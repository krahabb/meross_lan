"""
Miscellaneous namespace handlers and devices/entities.
This unit is a collection of rarely used small components where having
a dedicated unit for each of them would increase the number of small modules.
"""

import typing

from ..climate import MtsClimate
from ..helpers.namespaces import NamespaceHandler
from ..meross_device import DeviceType
from ..merossclient import const as mc, namespaces as mn
from ..sensor import (
    MLHumiditySensor,
    MLLightSensor,
    MLNumericSensor,
    MLNumericSensorDef,
    MLTemperatureSensor,
)
from .ms600 import MLPresenceSensor

if typing.TYPE_CHECKING:
    from ..meross_device import MerossDevice


class SensorLatestNamespaceHandler(NamespaceHandler):
    """
    Specialized handler for Appliance.Control.Sensor.Latest actually carried in thermostats
    (seen on an MTS200 so far:2024-06)
    """

    VALUE_KEY_EXCLUDED = (mc.KEY_TIMESTAMP, mc.KEY_TIMESTAMPMS)

    VALUE_KEY_ENTITY_DEF_DEFAULT = MLNumericSensorDef(MLNumericSensor, {})
    VALUE_KEY_ENTITY_DEF_MAP: dict[str, MLNumericSensorDef] = {
        mc.KEY_HUMI: MLNumericSensorDef(
            MLHumiditySensor, {}
        ),  # confirmed in MTS200 trace (2024/06)
        mc.KEY_TEMP: MLNumericSensorDef(
            MLTemperatureSensor, {"device_scale": 100}
        ),  # just guessed (2024/04)
        mc.KEY_LIGHT: MLNumericSensorDef(MLLightSensor, {}),  # just guessed (2024/09)
    }

    def __init__(self, device: "MerossDevice"):
        NamespaceHandler.__init__(
            self,
            device,
            mn.Appliance_Control_Sensor_Latest,
            handler=self._handle_Appliance_Control_Sensor_Latest,
        )
        self.polling_request_add_channel(0)

    def _handle_Appliance_Control_Sensor_Latest(self, header: dict, payload: dict):
        """
        {
            "latest": [
                {
                    "value": [{"humi": 596, "timestamp": 1718302844}],
                    "channel": 0,
                    "capacity": 2,
                }
            ]
        }
        """
        entities = self.device.entities
        for p_channel in payload[mc.KEY_LATEST]:
            channel = p_channel[mc.KEY_CHANNEL]
            for p_value in p_channel[mc.KEY_VALUE]:
                # I guess 'value' carries a list of sensors values
                # carried in a dict like {"humi": 596, "timestamp": 1718302844}
                for key, value in p_value.items():
                    if key in SensorLatestNamespaceHandler.VALUE_KEY_EXCLUDED:
                        continue
                    try:
                        entity: MLNumericSensor = entities[f"{channel}_sensor_{key}"]  # type: ignore
                    except KeyError:
                        entity_def = SensorLatestNamespaceHandler.VALUE_KEY_ENTITY_DEF_MAP.get(
                            key,
                            SensorLatestNamespaceHandler.VALUE_KEY_ENTITY_DEF_DEFAULT,
                        )
                        entity = entity_def.type(
                            self.device,
                            channel,
                            f"sensor_{key}",
                            **entity_def.args,
                        )
                        self.polling_request_add_channel(channel)

                    entity.update_device_value(value)

                    if key == mc.KEY_HUMI:
                        # look for a thermostat and sync the reported humidity
                        climate = entities.get(channel)
                        if isinstance(climate, MtsClimate):
                            if climate.current_humidity != entity.native_value:
                                climate.current_humidity = entity.native_value
                                climate.flush_state()


class SensorLatestXNamespaceHandler(NamespaceHandler):
    """
    Specialized handler for Appliance.Control.Sensor.LatestX. This ns carries
    a variadic payload of sensor values (seen on Hub/ms130 and ms600).
    This specific implementation is for standard MerossDevice(s) while
    Hub(s) have a somewhat different parser.
    """

    VALUE_KEY_ENTITY_DEF_DEFAULT = MLNumericSensorDef(MLNumericSensor, {})
    # many of these defs are guesses
    VALUE_KEY_ENTITY_DEF_MAP: dict[str, MLNumericSensorDef] = {
        mc.KEY_HUMI: MLNumericSensorDef(MLHumiditySensor, {}),
        mc.KEY_LIGHT: MLNumericSensorDef(MLLightSensor, {}),
        mc.KEY_PRESENCE: MLNumericSensorDef(MLPresenceSensor, {}),
        mc.KEY_TEMP: MLNumericSensorDef(MLTemperatureSensor, {"device_scale": 100}),
    }

    __slots__ = ()

    def __init__(self, device: "MerossDevice"):
        NamespaceHandler.__init__(
            self,
            device,
            mn.Appliance_Control_Sensor_LatestX,
            handler=self._handle_Appliance_Control_Sensor_LatestX,
        )
        if device.descriptor.type.startswith(mc.TYPE_MS600):
            MLPresenceSensor(device, 0, f"sensor_{mc.KEY_PRESENCE}")
            MLLightSensor(device, 0, f"sensor_{mc.KEY_LIGHT}")
        self.polling_request_add_channel(0)

    def _handle_Appliance_Control_Sensor_LatestX(self, header: dict, payload: dict):
        """
        {
            "latest": [
                {
                    "channel": 0,
                    "data": {
                        "presence": [
                            {
                                "times": 0,
                                "distance": 760,
                                "value": 2,
                                "timestamp": 1725907895,
                            }
                        ],
                        "light": [
                            {
                                "timestamp": 1725907912,
                                "value": 24,
                            }
                        ],
                    },
                }
            ]
        }
        Example taken from ms600
        """
        entities = self.device.entities
        for p_channel in payload[mc.KEY_LATEST]:
            channel = p_channel[mc.KEY_CHANNEL]
            for key_data, value_data in p_channel[mc.KEY_DATA].items():
                if type(value_data) is not list:
                    continue
                try:
                    entity: MLNumericSensor = entities[f"{channel}_sensor_{key_data}"]  # type: ignore
                except KeyError:
                    entity_def = (
                        SensorLatestXNamespaceHandler.VALUE_KEY_ENTITY_DEF_MAP.get(
                            key_data,
                            SensorLatestXNamespaceHandler.VALUE_KEY_ENTITY_DEF_DEFAULT,
                        )
                    )
                    entity = entity_def.type(
                        self.device,
                        channel,
                        f"sensor_{key_data}",
                        **entity_def.args,
                    )
                    # this is needed if we detect a new channel through a PUSH msg parsing
                    self.polling_request_add_channel(channel)
                entity._parse(value_data[0])


def namespace_init_sensor_latestx(device: "MerossDevice"):
    # Hub(s) have a different ns handler so far
    if device.get_type() is DeviceType.DEVICE:
        SensorLatestXNamespaceHandler(device)
