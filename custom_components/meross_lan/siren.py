from typing import TYPE_CHECKING, override

from homeassistant.components import siren

from . import const as mlc
from .helpers.entity import BinaryParser
from .merossclient.device.handler import MappingParser, NamespaceHandler
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
    TODO: implement more generalized support for Appliance.Control.Alarm namespace
    which seems to carry more features than just a simple on/off siren switch.
    """

    if TYPE_CHECKING:
        parent: Final[Device]  # type: ignore[override]

        # HA core entity attributes:
        init_available_tones: Final[dict[int, str]]
        init_supported_features: Final[siren.SirenEntityFeature]

        class Args(BinaryParser.Args):
            pass

    PLATFORM = siren.DOMAIN
    POLLING_CONFIG_DEFAULT = mlc.POLLING_CONFIG_CONFIGURATION
    init_key_value = BinaryParser.NestedKeyValue("event", "security", "value")
    init_entity_key = f"{mn.Appliance_Control_Alarm.slug}__{init_key_value}"
    init_value_on = 1
    init_value_off = 2

    # These 2 next configurations should conform to HA core Entity _attr_* pattern
    # but the actual cachedproperties metaclass is fighting us by wrapping
    # those in properties and thus preventing us from using them as class variables.
    # This is hard to cope with in general but in this case
    # the 'conflict' would need to declare an extra attribute (in the module or in the class)
    # since we need to use the available tones preset also for ConfigAlarm parser declaration.

    # Assuming Appliance.Config.Alarm is available with these presets
    # This might not be always the case.
    init_available_tones = {
        1: "Siren",
        2: "Beep",
        3: "Chime",
        4: "Alarm",
        5: "Roar",
        6: "Whistle",
        7: "Buzzer",
    }

    init_supported_features = (
        siren.SirenEntityFeature.TURN_ON
        | siren.SirenEntityFeature.TURN_OFF
        | siren.SirenEntityFeature.TONES
        | siren.SirenEntityFeature.VOLUME_SET
    )

    SLOTS_AUTO_INIT = (
        "available_tones",
        "supported_features",
    )

    @override
    async def async_turn_on(self, **kwargs):
        if kwargs:
            payload = self.index.copy()
            try:
                payload[mc.KEY_SONG] = kwargs[siren.ATTR_TONE]
            except KeyError:
                pass
            try:
                payload[mc.KEY_VOLUME] = round(kwargs[siren.ATTR_VOLUME_LEVEL] * 100)
            except KeyError:
                pass
            await self.parent.async_request(
                *mn.Appliance_Config_Alarm.request_set(payload)
            )
        await self.async_request_value(self.value_on)

    @classmethod
    def namespace_init(cls, ns: mn.Namespace, device: "Device", /):
        NamespaceHandler(ns, device, parser_class=cls, channels=(0,))


class ConfigAlarm(MappingParser):

    init_parser_defs = {
        mc.KEY_ENABLE: SwitchParser.ENTITY_DEF(
            entity_key=f"{mn.Appliance_Config_Alarm.slug}__{mc.KEY_ENABLE}",
            key_value=SwitchParser.SimpleKeyValue(mc.KEY_ENABLE),
        ),
        mc.KEY_SONG: SelectParser.ENTITY_DEF(
            entity_key=f"{mn.Appliance_Config_Alarm.slug}__{mc.KEY_SONG}",
            key_value=SelectParser.SimpleKeyValue(mc.KEY_SONG),
            options_map=Siren.init_available_tones,
        ),
        mc.KEY_VOLUME: NumberParser.ENTITY_DEF(
            entity_key=f"{mn.Appliance_Config_Alarm.slug}__{mc.KEY_VOLUME}",
            key_value=NumberParser.SimpleKeyValue(mc.KEY_VOLUME),
            native_min_value=0,
            native_max_value=100,
        ),
    }

    @classmethod
    def namespace_init(cls, ns: mn.Namespace, device: "Device", /):
        NamespaceHandler(ns, device, parser_class=cls, channels=(0,))


async_setup_entry = Siren.platform_setup_entry
