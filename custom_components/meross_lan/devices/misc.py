"""
Miscellaneous namespace handlers and devices/entities.
This unit is a collection of rarely used small components where having
a dedicated unit for each of them would increase the number of small modules.
"""

from typing import TYPE_CHECKING

from ..climate import MtsClimate
from ..helpers.namespaces import NamespaceHandler, mn
from ..merossclient.protocol import const as mc
from ..sensor import SensorParser
from .ms600 import PresenceSensor

if TYPE_CHECKING:
    from typing import Final

    from ..helpers.device import Device, MerossMessage
    from ..merossclient.protocol import types as mt


class SensorLatestNamespaceHandler(NamespaceHandler):
    """
    Specialized handler for Appliance.Control.Sensor.Latest actually carried in thermostats
    (seen on an MTS200 so far:2024-06)
    """

    if TYPE_CHECKING:
        ENTITY_ARGS: Final[dict[str, SensorParser.Args]]

    VALUE_KEY_EXCLUDED = (mc.KEY_TIMESTAMP, mc.KEY_TIMESTAMPMS)

    ENTITY_ARGS = {
        mc.KEY_HUMI: SensorParser.HUMIDITY_ARGS,
        mc.KEY_TEMP: SensorParser.TEMPERATURE_ARGS | {"device_scale": 100},
        mc.KEY_LIGHT: SensorParser.LIGHT_ARGS,
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
                        entities[f"{channel}_sensor_{key}"].update_device_value(value)
                    except KeyError:
                        SensorParser(
                            channel,
                            self.parent,
                            **(
                                SensorLatestNamespaceHandler.ENTITY_ARGS.get(key, {})
                                | {
                                    "entity_key": f"sensor_{key}",
                                    "device_value": value,
                                }
                            ),
                        )
                        self.polling_request_add_channel(channel)

                    if key == mc.KEY_HUMI:
                        # look for a thermostat and sync the reported humidity
                        try:
                            climate: "MtsClimate" = entities[channel]  # type: ignore
                            humidity = value / 10
                            if climate.current_humidity != humidity:
                                climate.current_humidity = humidity
                                climate.flush_state()
                        except (AttributeError, KeyError):
                            # not a climate (missing current_humidity) or no entity for the channel
                            pass


class SensorLatestXNamespaceHandler(NamespaceHandler):
    """
    Specialized handler for Appliance.Control.Sensor.LatestX. This ns carries
    a variadic payload of sensor values (seen on Hub/ms130 and ms600).
    This specific implementation is for standard Device(s) while
    Hub(s) have a somewhat different parser.
    """

    if TYPE_CHECKING:
        ENTITY_DEFS: Final[dict[str, SensorParser.Initializer]]

    # many of these defs are guesses
    ENTITY_DEFS = {
        mc.KEY_HUMI: SensorParser.Humidity,
        mc.KEY_LIGHT: SensorParser.Light,
        mc.KEY_PRESENCE: PresenceSensor,
        mc.KEY_TEMP: SensorParser.ENTITY_DEF(
            **(SensorParser.TEMPERATURE_ARGS | {"device_scale": 100})
        ),
    }

    def __init__(self, ns: mn.Namespace, device: "Device", /):
        NamespaceHandler.__init__(
            self,
            ns,
            device,
            handler=self._handle_Appliance_Control_Sensor_LatestX,
        )
        if device.descriptor.type.startswith(mc.TYPE_MS600):
            PresenceSensor(0, device)
            SensorParser(
                0, device, **(SensorParser.LIGHT_ARGS | {"entity_key": "sensor_light"})
            )
            self.polling_request_add_channel(0).update(
                {mc.KEY_DATA: [mc.KEY_PRESENCE, mc.KEY_LIGHT]}
            )
        else:
            self.polling_request_add_channel(0).update({mc.KEY_DATA: []})

    def _handle_Appliance_Control_Sensor_LatestX(self, message: "MerossMessage", /):
        ns = self.id
        key_idx = ns.key_idx
        entities = self.parent.entities
        p_channel: "mt.sensor.LatestXResponse_C"
        for p_channel in message.payload[ns.key]:
            channel: int = p_channel[key_idx]
            for data_key, data_value in p_channel[mc.KEY_DATA].items():
                try:
                    entities[f"{channel}_sensor_{data_key}"].update_device_value(
                        data_value[0]["value"]
                    )
                except KeyError:
                    # Likely missing the entity for this channel/data_key. It might also be
                    # a KeyError raised by accessing data_value[0]["value"] (or IndexError)
                    # but it will be raised again when constructing the entity.
                    SensorLatestXNamespaceHandler.ENTITY_DEFS.get(
                        data_key, SensorParser
                    )(
                        channel,
                        self.parent,
                        entity_key=f"sensor_{data_key}",
                        device_value=data_value[0]["value"],
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


def namespace_init_sensor_latestx(ns: mn.Namespace, device: "Device", /):
    # Hub(s) have a different ns handler so far
    # TODO: try to reconcile in a single handler
    if not device.descriptor.is_hub:
        SensorLatestXNamespaceHandler(ns, device)
