from typing import TYPE_CHECKING, override

from homeassistant.components import siren

from .helpers.entity import BinaryParser
from .merossclient.protocol import const as mc, namespaces as mn
from .number import NumberParser
from .select import SelectParser
from .switch import SwitchParser

if TYPE_CHECKING:
    from typing import Any, Final, NotRequired, Unpack

    from .helpers.device import Device
    from .merossclient.protocol.types import JsonDict, JsonMapping


async def async_setup_entry(hass, config_entry, async_add_devices):
    BinaryParser.platform_setup_entry(
        hass, config_entry, async_add_devices, siren.DOMAIN
    )


class Siren(BinaryParser, siren.SirenEntity):
    """
    This first implementation was mostly tailored to suit mts300 'fan hold time' feature
    We'll maybe generalize this platform when the need comes.
    After testing I found this entity a little useless since it just work for 'time of day'
    and not very well for time durations. The code is left for reference in the future.
    """

    class EnableSwitch(SwitchParser):
        ns = mn.Appliance_Config_Alarm
        NS_CHANNELS = SwitchParser.NS_CHANNELS_SINGLE
        key_value = mc.KEY_ENABLE
        ENTITY_KEY = f"{ns.slug}__{key_value}"

    class SongSelect(SelectParser):
        ns = mn.Appliance_Config_Alarm
        NS_CHANNELS = SelectParser.NS_CHANNELS_SINGLE
        key_value = mc.KEY_SONG
        ENTITY_KEY = f"{ns.slug}__{key_value}"

        OPTIONS_MAP = {
            1: "Siren",
            2: "Beep",
            3: "Chime",
            4: "Alarm",
            5: "Roar",
            6: "Whistle",
            7: "Buzzer",
        }

    class VolumeNumber(NumberParser):
        ns = mn.Appliance_Config_Alarm
        NS_CHANNELS = NumberParser.NS_CHANNELS_SINGLE
        key_value = mc.KEY_VOLUME
        ENTITY_KEY = f"{ns.slug}__{key_value}"
        _attr_native_max_value = 100
        _attr_native_min_value = 0

    if TYPE_CHECKING:
        parent: Final[Device]  # type: ignore[override]

        # HA core entity attributes:
        _attr_supported_features: Final[siren.SirenEntityFeature]

        class Args(BinaryParser.Args):
            pass

    PLATFORM = siren.DOMAIN
    ns = mn.Appliance_Control_Alarm
    NS_CHANNELS = BinaryParser.NS_CHANNELS_SINGLE
    key_value = "event_security_value"
    ENTITY_KEY = f"{ns.slug}__event_security_value"
    native_on = 1
    native_off = 2

    _attr_supported_features = (
        siren.SirenEntityFeature.TURN_ON
        | siren.SirenEntityFeature.TURN_OFF
        | siren.SirenEntityFeature.TONES
        | siren.SirenEntityFeature.VOLUME_SET
    )

    ATTR_KEY_MAP = {
        siren.ATTR_TONE: "song",
        siren.ATTR_VOLUME_LEVEL: "volume",
    }

    __slots__ = ()

    def __init__(self, channel: int, device: "Device", /, **kwargs: "Unpack[Args]"):
        if mn.Appliance_Config_Alarm in device.descriptor.ability:
            song_select = self.SongSelect(channel, device)
            self.available_tones = song_select.OPTIONS_MAP
            self.supported_features = self._attr_supported_features
            device.get_handler(mn.Appliance_Config_Alarm).register_parsers(
                self.EnableSwitch(channel, device),
                song_select,
                self.VolumeNumber(channel, device),
            )
        else:
            self.available_tones = {}
            self.supported_features = (
                siren.SirenEntityFeature.TURN_ON | siren.SirenEntityFeature.TURN_OFF
            )
        super().__init__(channel, device, **kwargs)
        device.register_parser_entity(self)

    @override
    async def async_request_value(self, device_value, /) -> None:
        await self.async_request_payload(
            {"event": {"security": {"value": device_value}}}
        )
        self.update_device_value(device_value)

    @override
    async def async_turn_on(self, **kwargs):
        if kwargs:
            payload = {}
            for kwarg_key, payload_key in self.ATTR_KEY_MAP.items():
                try:
                    payload[payload_key] = kwargs[kwarg_key]
                except KeyError:
                    pass
            await self.parent.async_request(
                *mn.Appliance_Config_Alarm.request_set(payload, self.channel)
            )

        await self.async_request_value(self.native_on)

    # interface: self
    def _parse_alarm(self, payload: "JsonDict") -> None:
        """Parse Appliance.Control.Alarm message."""
        try:
            self.update_device_value(payload["event"]["security"]["value"])
        except KeyError:
            pass
