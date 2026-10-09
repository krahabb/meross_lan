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
    from typing import Final, Unpack

    from .helpers.device import Device


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

    _INIT_value_on = 1  # TAKE in Meross app
    _INIT_value_off = 2  # NORMAL in Meross app

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

    HANDLER_CHANNELS = (0,)

    _INIT_parser_defs = {
        mc.KEY_ENABLE: SwitchParser.DEF(
            entity_key=f"{mn.Appliance_Config_Alarm.slug}__{mc.KEY_ENABLE}",
        ),
        mc.KEY_SONG: SelectParser.DEF(
            entity_key=f"{mn.Appliance_Config_Alarm.slug}__{mc.KEY_SONG}",
            options_map=mc.CONFIG_ALARM_SONGS,
        ),
        mc.KEY_VOLUME: NumberParser.DEF(
            entity_key=f"{mn.Appliance_Config_Alarm.slug}__{mc.KEY_VOLUME}",
            native_min_value=0,
            native_max_value=100,
        ),
    }


class ControlAlarm(MappingParser):

    HANDLER_CHANNELS = (0,)

    _INIT_excluded_keys = MappingParser._INIT_excluded_keys + ("source",)
    _INIT_parser_defs = {
        mc.KEY_EVENT: {
            _key: {
                mc.KEY_VALUE: Siren.DEF(
                    entity_key=f"{mn.Appliance_Control_Alarm.slug}__{mc.KEY_EVENT}_{_key}_{mc.KEY_VALUE}",
                    name=f"{_key.capitalize()} event",
                ),
            }
            for _key in (
                mc.KEY_DEMOLISH,
                mc.KEY_INTERCONN,
                mc.KEY_MASECURITY,
                mc.KEY_SECURITY,
            )
        },
    }


async_setup_entry = Siren.platform_setup_entry
