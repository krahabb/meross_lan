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
    from .merossclient.protocol.types import JsonDict


class Siren(BinaryParser, siren.SirenEntity):
    """
    Supports msh450 internal alarm as proposed in #625
    """

    class EnableSwitch(SwitchParser):
        init_key_value = SwitchParser.SimpleKeyValue(mc.KEY_ENABLE)
        init_entity_key = f"{mn.Appliance_Config_Alarm.slug}__{init_key_value}"

    class SongSelect(SelectParser):
        init_key_value = SelectParser.SimpleKeyValue(mc.KEY_SONG)
        init_entity_key = f"{mn.Appliance_Config_Alarm.slug}__{init_key_value}"

        init_options_map = {
            1: "Siren",
            2: "Beep",
            3: "Chime",
            4: "Alarm",
            5: "Roar",
            6: "Whistle",
            7: "Buzzer",
        }

    class VolumeNumber(NumberParser):
        init_key_value = NumberParser.SimpleKeyValue(mc.KEY_VOLUME)
        init_entity_key = f"{mn.Appliance_Config_Alarm.slug}__{init_key_value}"
        _attr_native_max_value = 100
        _attr_native_min_value = 0

    if TYPE_CHECKING:
        parent: Final[Device]  # type: ignore[override]

        # HA core entity attributes:
        _attr_supported_features: Final[siren.SirenEntityFeature]

        class Args(BinaryParser.Args):
            pass

    PLATFORM = siren.DOMAIN
    init_key_value = BinaryParser.NestedKeyValue("event", "security", "value")
    init_entity_key = f"{mn.Appliance_Control_Alarm.slug}__{init_key_value}"
    init_value_on = 1
    init_value_off = 2

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

    __slots__ = BinaryParser._calc_slots()

    def __init__(self, id, device: "Device", /, **kwargs: "Unpack[Args]"):
        BinaryParser.__init__(self, id, device, **kwargs)
        ns_config_alarm = mn.Appliance_Config_Alarm
        if ns_config_alarm in device.descriptor.ability:
            song_select = Siren.SongSelect(
                id, device, ns=ns_config_alarm, index=self.index
            )
            self.available_tones = song_select.options_map
            self.supported_features = self._attr_supported_features
            device.get_handler(ns_config_alarm).register_parsers(
                Siren.EnableSwitch(id, device, ns=ns_config_alarm, index=self.index),
                song_select,
                Siren.VolumeNumber(id, device, ns=ns_config_alarm, index=self.index),
            )
        else:
            self.available_tones = {}
            self.supported_features = (
                siren.SirenEntityFeature.TURN_ON | siren.SirenEntityFeature.TURN_OFF
            )

    @override
    async def async_turn_on(self, **kwargs):
        if kwargs:
            payload = self.index.copy()
            for kwarg_key, payload_key in self.ATTR_KEY_MAP.items():
                try:
                    payload[payload_key] = kwargs[kwarg_key]
                except KeyError:
                    pass
            await self.parent.async_request(
                *mn.Appliance_Config_Alarm.request_set(payload)
            )
        await self.async_request_value(self.value_on)

    @classmethod
    def namespace_init(cls, ns: mn.Namespace, device: "Device", /):
        device._create_handler(ns, parser_class=cls, channels=(0,))


async_setup_entry = Siren.platform_setup_entry
