"""
Miscellaneous namespace handlers and devices/entities.
This unit is a collection of rarely used small components where having
a dedicated unit for each of them would increase the number of small modules.
"""

from typing import TYPE_CHECKING, override

from ..climate import MtsClimate
from ..helpers.namespaces import EntityDefNamespaceHandler, NamespaceHandler, mn
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

    POLLING_CONFIG_DEFAULT = NamespaceHandler.POLLING_CONFIG_FASTSENSOR

    VALUE_KEY_EXCLUDED = (mc.KEY_TIMESTAMP, mc.KEY_TIMESTAMPMS)

    ENTITY_ARGS = {
        mc.KEY_HUMI: SensorParser.HUMIDITY_ARGS,
        mc.KEY_TEMP: SensorParser.TEMPERATURE_ARGS | {"device_scale": 100},
        mc.KEY_LIGHT: SensorParser.LIGHT_ARGS,
    }

    def __init__(self, ns: mn.Namespace, device: "Device", /):
        NamespaceHandler.__init__(self, ns, device)
        self.polling_request_add_index(mn.IndexType.channel(0))

    @override
    def _handle_channel_list(self, message: "MerossMessage", /):
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
                        index = mn.IndexType.channel(channel)
                        self.parent.add_entity(
                            SensorParser(
                                channel,
                                self.parent,
                                **(
                                    SensorLatestNamespaceHandler.ENTITY_ARGS.get(
                                        key, {}
                                    )
                                    | {
                                        "index": index,
                                        "entity_key": f"sensor_{key}",
                                        "device_value": value,
                                        "device_info": self.parent.get_device_entry_info(
                                            channel
                                        ),
                                    }
                                ),
                            )
                        )
                        self.polling_request_add_index(index)

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


class SensorLatestXNamespaceHandler(EntityDefNamespaceHandler):
    """
    Specialized handler for Appliance.Control.Sensor.LatestX. This ns carries
    a variadic payload of sensor values (seen on Hub/ms130 and ms600).
    This specific implementation is for standard Device(s) while
    Hub(s) have a somewhat different parser.
    """

    if TYPE_CHECKING:
        init_entity_defs: Final[dict[str, type[SensorParser]]]

    POLLING_CONFIG_DEFAULT = EntityDefNamespaceHandler.POLLING_CONFIG_FASTSENSOR

    # many of these defs are guesses
    init_entity_defs = {
        mc.KEY_HUMI: SensorParser.ENTITY_DEF(**SensorParser.HUMIDITY_ARGS),
        mc.KEY_LIGHT: SensorParser.ENTITY_DEF(**SensorParser.LIGHT_ARGS),
        mc.KEY_PRESENCE: PresenceSensor.ENTITY_DEF(),
        mc.KEY_TEMP: SensorParser.ENTITY_DEF(
            **(SensorParser.TEMPERATURE_ARGS | {"device_scale": 100})
        ),
    }

    def __init__(self, ns: mn.Namespace, device: "Device", /):
        NamespaceHandler.__init__(self, ns, device)
        if not device.descriptor.is_hub:
            if device.descriptor.type.startswith(mc.TYPE_MS600):
                data_keys = [mc.KEY_PRESENCE, mc.KEY_LIGHT]
            else:
                data_keys = []  # no idea of other devices supported
            index = mn.IndexType.channel(0)
            for data_key in data_keys:
                self.entity_defs[data_key](
                    0,
                    device,
                    entity_key=f"sensor_{data_key}",
                    index=index,
                    device_info=device.get_device_entry_info(0),
                )
            self.polling_request_payload.append(
                {mc.KEY_CHANNEL: 0, mc.KEY_DATA: data_keys}
            )

    @override
    def _handle_channel_list(self, message: "MerossMessage", /):
        entities = self.parent.entities
        payload: "mt.sensor.LatestX_C"
        for payload in message.payload[self.id.key]:
            channel: int = payload[mc.KEY_CHANNEL]
            for data_key, data_value in payload[mc.KEY_DATA].items():
                try:
                    entities[f"{channel}_sensor_{data_key}"].update_device_value(
                        data_value[0]["value"]
                    )
                except KeyError:
                    # Likely missing the entity for this channel/data_key. It might also be
                    # a KeyError raised by accessing data_value[0]["value"] (or IndexError)
                    # but it will be raised again when constructing the entity.
                    index = mn.IndexType.channel(channel)
                    self.entity_defs.get(data_key, SensorParser)(
                        channel,
                        self.parent,
                        entity_key=f"sensor_{data_key}",
                        index=index,
                        device_info=self.parent.get_device_entry_info(channel),
                        device_value=data_value[0]["value"],
                    )
                    for channel_payload in self.polling_request_payload:
                        if channel_payload[mc.KEY_CHANNEL] == channel:
                            channel_payload[mc.KEY_DATA].append(data_key)
                            break
                    else:
                        self.polling_request_payload.append(
                            {mc.KEY_CHANNEL: channel, mc.KEY_DATA: [data_key]}
                        )
                        self.polling_response_size += self.id.payload_item_size
