from typing import TYPE_CHECKING, override

from homeassistant.components import siren

from . import const as mlc
from .helpers import entity as mle
from .merossclient.device.handler import MappingParser, NamespaceHandler
from .merossclient.protocol import const as mc, namespaces as mn
from .number import NumberParser
from .select import SelectParser
from .switch import SwitchParser

if TYPE_CHECKING:
    from typing import Any, ClassVar, Final, Mapping, NotRequired, Unpack

    from .helpers.device import Device
    from .merossclient.protocol import types as mt
    from .merossclient.protocol.types import JsonDict


class Siren(mle.BinaryParser, siren.SirenEntity):
    """Supports msh450 internal alarmas proposed in #625.
    This feature is associated with Appliance.Control.Alarm ns but its overall semantics
    are not clear yet. This ns also carries similar data points appearing for example on
    smokeSensor devices."""

    if TYPE_CHECKING:
        # HA core entity attributes:
        _attr_available_tones: Final[dict[int, str]]
        _attr_supported_features: Final[siren.SirenEntityFeature]

    PLATFORM = siren.DOMAIN

    init_value_on = 1  # TAKE in Meross app
    init_value_off = 2  # NORMAL in Meross app

    # Assuming Appliance.Config.Alarm is available with these presets
    # This might not be always the case.
    _attr_available_tones = mc.CONFIG_ALARM_SONGS
    _attr_supported_features = (
        siren.SirenEntityFeature.TURN_ON
        | siren.SirenEntityFeature.TURN_OFF
        | siren.SirenEntityFeature.TONES
        | siren.SirenEntityFeature.VOLUME_SET
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


class ConfigAlarm(MappingParser):

    POLLING_CONFIG_DEFAULT = mlc.POLLING_CONFIG_CONFIGURATION

    init_parser_defs = {
        mc.KEY_ENABLE: SwitchParser.DEF(
            entity_key=f"{mn.Appliance_Config_Alarm.slug}__{mc.KEY_ENABLE}",
            key_value=SwitchParser.SimpleKeyValue(mc.KEY_ENABLE),
        ),
        mc.KEY_SONG: SelectParser.DEF(
            entity_key=f"{mn.Appliance_Config_Alarm.slug}__{mc.KEY_SONG}",
            key_value=SelectParser.SimpleKeyValue(mc.KEY_SONG),
            options_map=mc.CONFIG_ALARM_SONGS,
        ),
        mc.KEY_VOLUME: NumberParser.DEF(
            entity_key=f"{mn.Appliance_Config_Alarm.slug}__{mc.KEY_VOLUME}",
            key_value=NumberParser.SimpleKeyValue(mc.KEY_VOLUME),
            native_min_value=0,
            native_max_value=100,
        ),
    }

    @classmethod
    def namespace_init(cls, ns: mn.Namespace, device: "Device", /):
        NamespaceHandler(ns, device, parser_class=cls, channels=(0,))


class ControlAlarm(MappingParser):

    if TYPE_CHECKING:
        parent: Final[Device]  # type:ignore[override]
        parsers: Final[dict[str, mle.ValueParser]]  # type: ignore[override]
        init_parser_defs: ClassVar[Mapping[str, type[mle.ValueParser]]]
        parser_defs: Mapping[str, type[mle.ValueParser]]

    POLLING_CONFIG_DEFAULT = mlc.POLLING_CONFIG_CONFIGURATION

    init_parser_defs = {
        mc.KEY_DEMOLISH: Siren.DEF(
            key_value=Siren.NestedKeyValue(mc.KEY_EVENT, mc.KEY_DEMOLISH, mc.KEY_VALUE),
        ),
        mc.KEY_INTERCONN: Siren.DEF(
            key_value=Siren.NestedKeyValue(
                mc.KEY_EVENT, mc.KEY_INTERCONN, mc.KEY_VALUE
            ),
        ),
        mc.KEY_MASECURITY: Siren.DEF(
            key_value=Siren.NestedKeyValue(
                mc.KEY_EVENT, mc.KEY_MASECURITY, mc.KEY_VALUE
            ),
        ),
        mc.KEY_SECURITY: Siren.DEF(
            key_value=Siren.NestedKeyValue(mc.KEY_EVENT, mc.KEY_SECURITY, mc.KEY_VALUE),
        ),
    }

    def __call__(self, payload: "mt.control.Alarm"):
        for key, value in payload[mc.KEY_EVENT].items():
            try:
                self.parsers[key](payload)
            except Exception as e:
                if key not in self.parsers:  # surely a KeyError
                    try:
                        self.parsers[key] = self.parser_defs[key](
                            self.index.value,
                            self.parent,
                            entity_key=f"{self.ns.slug}__{self.parser_defs[key]["key_value"]}",  # type: ignore
                            ns=self.ns,
                            index=self.index,
                            ns_value=value,
                            name=key,
                        )
                    except Exception as e:
                        self.log_exception(
                            self.WARNING,
                            e,
                            "creating parser for '%s' key in '%s' namespace",
                            key,
                            self.ns,
                        )
                else:
                    self.log_exception(
                        self.WARNING,
                        e,
                        "parsing key '%s': payload=%s",
                        key,
                        _any=payload,
                        timeout=14400,
                    )

    @classmethod
    def namespace_init(cls, ns: mn.Namespace, device: "Device", /):
        NamespaceHandler(ns, device, parser_class=cls, channels=(0,))


async_setup_entry = Siren.platform_setup_entry
