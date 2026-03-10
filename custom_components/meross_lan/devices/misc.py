"""
Miscellaneous namespace handlers and devices/entities.
This unit is a collection of rarely used small components where having
a dedicated unit for each of them would increase the number of small modules.
"""

from typing import TYPE_CHECKING

from ..climate import MtsClimate
from ..helpers.namespaces import NamespaceHandler, mn
from ..merossclient.protocol import const as mc
from ..sensor import (
    HumiditySensor,
    LightSensor,
    SensorParser,
    TemperatureSensor,
)
from .ms600 import PresenceSensor

if TYPE_CHECKING:
    from ..helpers.device import Device, MerossMessage
    from ..merossclient.protocol import types as mt
    from ..merossclient.protocol.types import sensor as mt_s


class SensorLatestNamespaceHandler(NamespaceHandler):
    """
    Specialized handler for Appliance.Control.Sensor.Latest actually carried in thermostats
    (seen on an MTS200 so far:2024-06)
    """

    VALUE_KEY_EXCLUDED = (mc.KEY_TIMESTAMP, mc.KEY_TIMESTAMPMS)

    ENTITY_DEFS = {
        mc.KEY_HUMI: HumiditySensor.ENTITY_DEF(),  # confirmed in MTS200 trace (2024/06)
        mc.KEY_TEMP: TemperatureSensor.ENTITY_DEF(
            device_scale=100
        ),  # just guessed (2024/04)
        mc.KEY_LIGHT: LightSensor.ENTITY_DEF(),  # just guessed (2024/09)
    }

    def __init__(self, ns: mn.Namespace, device: "Device", /):
        NamespaceHandler.__init__(
            self,
            ns,
            device,
            handler=self._handle_Appliance_Control_Sensor_Latest,
        )
        self.polling_request_add_channel(0)

    def _handle_Appliance_Control_Sensor_Latest(self, message: "MerossMessage", /):
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
        entities = self.parent.entities
        for p_channel in message.payload[mc.KEY_LATEST]:
            channel = p_channel[mc.KEY_CHANNEL]
            for p_value in p_channel[mc.KEY_VALUE]:
                # I guess 'value' carries a list of sensors values
                # carried in a dict like {"humi": 596, "timestamp": 1718302844}
                for key, value in p_value.items():
                    if key in SensorLatestNamespaceHandler.VALUE_KEY_EXCLUDED:
                        continue
                    try:
                        entity: SensorParser = entities[f"{channel}_sensor_{key}"]  # type: ignore
                    except KeyError:
                        try:
                            entity_def = SensorLatestNamespaceHandler.ENTITY_DEFS[key]
                        except KeyError:
                            entity = SensorParser(
                                channel, self.parent, entity_key=f"sensor_{key}"
                            )
                        else:
                            entity = entity_def.type(
                                channel,
                                self.parent,
                                entity_key=f"sensor_{key}",
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

    # many of these defs are guesses
    ENTITY_DEFS = {
        mc.KEY_HUMI: HumiditySensor.ENTITY_DEF(),
        mc.KEY_LIGHT: LightSensor.ENTITY_DEF(),
        mc.KEY_PRESENCE: PresenceSensor.ENTITY_DEF(),
        mc.KEY_TEMP: TemperatureSensor.ENTITY_DEF(device_scale=100),
    }

    __slots__ = ()

    def __init__(self, ns: mn.Namespace, device: "Device", /):
        NamespaceHandler.__init__(
            self,
            ns,
            device,
            handler=self._handle_Appliance_Control_Sensor_LatestX,
        )
        if device.descriptor.type.startswith(mc.TYPE_MS600):
            PresenceSensor(0, device)
            LightSensor(0, device, entity_key="sensor_light")
            self.polling_request_add_channel(
                0, {mc.KEY_DATA: [mc.KEY_PRESENCE, mc.KEY_LIGHT]}
            )
        else:
            self.polling_request_add_channel(0, {mc.KEY_DATA: []})

    def _handle_Appliance_Control_Sensor_LatestX(self, message: "MerossMessage", /):
        ns = self.id
        key_idx = ns.key_idx
        entities = self.parent.entities
        p_channel: "mt_s.LatestXResponse_C"
        for p_channel in message.payload[ns.key]:
            channel: int = p_channel[key_idx]
            for data_key, data_value in p_channel[mc.KEY_DATA].items():
                try:
                    entity: SensorParser = entities[f"{channel}_sensor_{data_key}"]  # type: ignore
                except KeyError:
                    # new channel or data_key
                    try:
                        entity_def = SensorLatestXNamespaceHandler.ENTITY_DEFS[data_key]
                    except KeyError:
                        entity = SensorParser(
                            channel, self.parent, entity_key=f"sensor_{data_key}"
                        )
                    else:
                        entity = entity_def.type(
                            channel,
                            self.parent,
                            entity_key=f"sensor_{data_key}",
                            **entity_def.kwargs,
                        )

                    polling_request_channels = self.polling_request_channels
                    for channel_payload in polling_request_channels:
                        if channel_payload[key_idx] == channel:
                            channel_payload[mc.KEY_DATA].append(data_key)
                            break
                    else:
                        polling_request_channels.append(
                            {key_idx: channel, mc.KEY_DATA: [data_key]}
                        )
                        self.polling_response_size = (
                            self.HEADER_AVG_SIZE
                            + len(polling_request_channels) * ns.payload_item_size
                        )
                entity._parse(data_value[0])


def namespace_init_sensor_latestx(ns: mn.Namespace, device: "Device", /):
    # Hub(s) have a different ns handler so far
    # TODO: try to reconcile in a single handler
    if not device.descriptor.is_hub:
        SensorLatestXNamespaceHandler(ns, device)
