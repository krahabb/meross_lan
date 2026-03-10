from typing import TYPE_CHECKING

from ..merossclient.protocol import const as mc, namespaces as mn
from ..select import SelectParser

if TYPE_CHECKING:
    from ..helpers.device import Device
    from ..helpers.entity import ChannelType
    from ..merossclient.protocol.types import JsonList


def digest_init_spray(
    device: "Device", digest: "JsonList", /
) -> "Device.DigestInitReturnType":
    """[{"channel": 0, "mode": 0, "lmTime": 1629035486, "lastMode": 1, "onoffTime": 1629035486}]"""
    for channel_digest in digest:
        Spray(channel_digest[mc.KEY_CHANNEL], device)

    handler = device.get_handler(mn.Appliance_Control_Spray)
    return handler.parse_list, (handler,)


class Spray(SelectParser):
    """
    SelectEntity class for Appliance.Control.Spray namespace. This is also
    slightly customized in DiffuserSpray to override namespace mapping and
    message formatting.
    """

    ENTITY_KEY = mc.KEY_SPRAY
    ns = mn.Appliance_Control_Spray
    key_value = mc.KEY_MODE

    OPTIONS_MAP = {
        mc.SPRAY_MODE_OFF: "off",
        mc.SPRAY_MODE_CONTINUOUS: "on",
        mc.SPRAY_MODE_INTERMITTENT: "eco",
    }

    _attr_entity_category = None

    def __init__(self, channel: "ChannelType", device: "Device", /):
        SelectParser.__init__(self, channel, device)
        device.register_parser_entity(self)
