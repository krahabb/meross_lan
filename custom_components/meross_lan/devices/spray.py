import typing

from ..meross_entity import MEDictChannelMixin
from ..merossclient import const as mc, namespaces as mn
from ..select import MLConfigSelect

if typing.TYPE_CHECKING:
    from ..meross_device import DigestInitReturnType, MerossDevice


def digest_init_spray(device: "MerossDevice", digest) -> "DigestInitReturnType":
    """[{"channel": 0, "mode": 0, "lmTime": 1629035486, "lastMode": 1, "onoffTime": 1629035486}]"""
    for channel_digest in digest:
        MLSpray(device, channel_digest[mc.KEY_CHANNEL])

    handler = device.get_handler(mn.Appliance_Control_Spray)
    return handler.parse_list, (handler,)


class MLSpray(MEDictChannelMixin, MLConfigSelect):
    """
    SelectEntity class for Appliance.Control.Spray namespace. This is also
    slightly customized in MLDiffuserSpray to override namespace mapping and
    message formatting.
    """

    ns = mn.Appliance_Control_Spray
    key_value = mc.KEY_MODE

    OPTIONS_MAP = {
        mc.SPRAY_MODE_OFF: "off",
        mc.SPRAY_MODE_CONTINUOUS: "on",
        mc.SPRAY_MODE_INTERMITTENT: "eco",
    }

    manager: "MerossDevice"

    def __init__(self, manager: "MerossDevice", channel: object):
        super().__init__(manager, channel, mc.KEY_SPRAY)
        manager.register_parser_entity(self)
