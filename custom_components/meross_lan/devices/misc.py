"""
Miscellaneous namespace handlers and devices/entities.
This unit is a collection of rarely used small components where having
a dedicated unit for each of them would increase the number of small modules.
"""

from typing import TYPE_CHECKING

from .. import const as mlc
from ..climate import MtsClimate
from ..helpers.namespaces import NamespaceHandler, mn
from ..merossclient.protocol import const as mc
from ..sensor import (
    MLHumiditySensor,
    MLLightSensor,
    MLNumericSensor,
    MLTemperatureSensor,
)
from .ms600 import MLPresenceSensor

if TYPE_CHECKING:
    from ..helpers.device import Device
    from ..merossclient.protocol import types as mt
    from ..merossclient.protocol.types import sensor as mt_s

class SensorLatestNamespaceHandler(NamespaceHandler):
    """
    Specialized handler for Appliance.Control.Sensor.Latest actually carried in thermostats
    (seen on an MTS200 so far:2024-06)
    """

    VALUE_KEY_EXCLUDED = (mc.KEY_TIMESTAMP, mc.KEY_TIMESTAMPMS)

    VALUE_KEY_ENTITY_DEF_DEFAULT = MLNumericSensor.SensorDef(MLNumericSensor)
    VALUE_KEY_ENTITY_DEF_MAP = {
        mc.KEY_HUMI: MLNumericSensor.SensorDef(
            MLHumiditySensor
        ),  # confirmed in MTS200 trace (2024/06)
        mc.KEY_TEMP: MLNumericSensor.SensorDef(
            MLTemperatureSensor, device_scale=100
        ),  # just guessed (2024/04)
        mc.KEY_LIGHT: MLNumericSensor.SensorDef(
            MLLightSensor
        ),  # just guessed (2024/09)
    }

    def __init__(self, device: "Device"):
        NamespaceHandler.__init__(
            self,
            device,
            mn.Appliance_Control_Sensor_Latest,
            handler=self._handle_Appliance_Control_Sensor_Latest,
        )
        self.polling_request_add_channel(0)

    def _handle_Appliance_Control_Sensor_Latest(self, header, payload):
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
                            **entity_def.kwargs,
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
    This specific implementation is for standard Device(s) while
    Hub(s) have a somewhat different parser.
    """

    VALUE_KEY_ENTITY_DEF_DEFAULT = MLNumericSensor.SensorDef()
    # many of these defs are guesses
    VALUE_KEY_ENTITY_DEF_MAP = {
        mc.KEY_HUMI: MLNumericSensor.SensorDef(MLHumiditySensor),
        mc.KEY_LIGHT: MLNumericSensor.SensorDef(MLLightSensor),
        mc.KEY_PRESENCE: MLNumericSensor.SensorDef(MLPresenceSensor),
        mc.KEY_TEMP: MLNumericSensor.SensorDef(MLTemperatureSensor, device_scale=100),
    }

    __slots__ = ()

    def __init__(self, device: "Device"):
        NamespaceHandler.__init__(
            self,
            device,
            mn.Appliance_Control_Sensor_LatestX,
            handler=self._handle_Appliance_Control_Sensor_LatestX,
        )
        if device.descriptor.type.startswith(mc.TYPE_MS600):
            MLPresenceSensor(device, 0, "sensor_presence")
            MLLightSensor(device, 0, "sensor_light")
            self.polling_request_add_channel(0, {mc.KEY_DATA: [mc.KEY_PRESENCE, mc.KEY_LIGHT]})
        else:
            self.polling_request_add_channel(0, {mc.KEY_DATA: []})

    def _handle_Appliance_Control_Sensor_LatestX(self, header, payload, /):
        ns = self.ns
        key_channel = ns.key_channel
        entities = self.device.entities
        p_channel: "mt_s.LatestXResponse_C"
        for p_channel in payload[ns.key]:
            channel: int = p_channel[key_channel]
            for data_key, data_value in p_channel[mc.KEY_DATA].items():
                try:
                    entity: MLNumericSensor = entities[f"{channel}_sensor_{data_key}"]  # type: ignore
                except KeyError:
                    # new channel or data_key
                    entity_def = (
                        SensorLatestXNamespaceHandler.VALUE_KEY_ENTITY_DEF_MAP.get(
                            data_key,
                            SensorLatestXNamespaceHandler.VALUE_KEY_ENTITY_DEF_DEFAULT,
                        )
                    )
                    entity = entity_def.type(
                        self.device,
                        channel,
                        f"sensor_{data_key}",
                        **entity_def.kwargs,
                    )

                    polling_request_channels = self.polling_request_channels
                    for channel_payload in polling_request_channels:
                        if channel_payload[key_channel] == channel:
                            channel_payload[mc.KEY_DATA].append(data_key)
                            break
                    else:
                        polling_request_channels.append({key_channel: channel, mc.KEY_DATA: [data_key]})
                        self.polling_response_size = (
                            self.polling_response_base_size
                            + len(polling_request_channels) * self.polling_response_item_size
                        )
                entity._parse(data_value[0])


def namespace_init_sensor_latestx(device: "Device"):
    # Hub(s) have a different ns handler so far
    # TODO: try to reconcile in a single handler
    if device.get_type() is mlc.DeviceType.DEVICE:
        SensorLatestXNamespaceHandler(device)
