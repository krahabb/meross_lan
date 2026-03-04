from typing import TYPE_CHECKING, override

from homeassistant.components import siren

from .helpers.entity import MLBinaryEntity
from .merossclient.protocol import const as mc, namespaces as mn
from .number import MLConfigNumber
from .select import MLConfigSelect
from .switch import MLSwitch

if TYPE_CHECKING:
    from typing import Any, Final, NotRequired, Unpack

    from .helpers.device import Device
    from .merossclient.protocol.types import JsonDict, JsonMapping


async def async_setup_entry(hass, config_entry, async_add_devices):
    MLBinaryEntity.platform_setup_entry(
        hass, config_entry, async_add_devices, siren.DOMAIN
    )


class MLSiren(MLBinaryEntity, siren.SirenEntity):
    """
    This first implementation was mostly tailored to suit mts300 'fan hold time' feature
    We'll maybe generalize this platform when the need comes.
    After testing I found this entity a little useless since it just work for 'time of day'
    and not very well for time durations. The code is left for reference in the future.
    """

    class EnableSwitch(MLSwitch):
        ns = mn.Appliance_Config_Alarm
        NS_CHANNELS = MLSwitch.NS_CHANNELS_SINGLE
        key_value = mc.KEY_ENABLE
        ENTITY_KEY = f"{ns.slug}__{key_value}"

    class SongSelect(MLConfigSelect):
        ns = mn.Appliance_Config_Alarm
        NS_CHANNELS = MLConfigSelect.NS_CHANNELS_SINGLE
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

    class VolumeNumber(MLConfigNumber):
        ns = mn.Appliance_Config_Alarm
        NS_CHANNELS = MLConfigNumber.NS_CHANNELS_SINGLE
        key_value = mc.KEY_VOLUME
        ENTITY_KEY = f"{ns.slug}__{key_value}"
        native_min_value = 0
        native_max_value = 100

    if TYPE_CHECKING:
        parent: Final[Device]  # type: ignore[override]

        class Args(MLBinaryEntity.Args):
            pass

    PLATFORM = siren.DOMAIN
    ns = mn.Appliance_Control_Alarm
    NS_CHANNELS = MLBinaryEntity.NS_CHANNELS_SINGLE
    key_value = "event_security_value"
    ENTITY_KEY = f"{ns.slug}__event_security_value"
    native_on = 1
    native_off = 2

    SUPPORTED_FEATURES = (
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
        super().__init__(channel, device, **kwargs)
        device.register_parser_entity(self)
        if mn.Appliance_Config_Alarm in device.descriptor.ability:
            song_select = self.SongSelect(channel, device)
            self.available_tones = song_select.OPTIONS_MAP
            self.supported_features = self.SUPPORTED_FEATURES
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

    @override
    async def async_request_value(self, device_value, /) -> None:
        await self.async_request_payload(
            {"event": {"security": {"value": device_value}}}
        )
        self.update_device_value(device_value)

    @MLBinaryEntity.ha_action
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
