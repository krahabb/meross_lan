import typing

from ..merossclient import const as mc
from ..fan import MLFan

if typing.TYPE_CHECKING:
    from ..helpers.namespaces import DigestParseFunc
    from ..meross_device import MerossDevice

def digest_init(device: "MerossDevice", digest) -> "DigestParseFunc":
    """[{ "channel": 2, "speed": 3, "maxSpeed": 3 }]"""
    for channel_digest in digest:
        MLFan(device, channel_digest[mc.KEY_CHANNEL])
    # mc.NS_APPLIANCE_CONTROL_FAN should already be there since the namespace
    # handlers dict has been initialized before digest
    return device.namespace_handlers[mc.NS_APPLIANCE_CONTROL_FAN]._parse_list
