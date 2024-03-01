import typing

from ..light import MLLight
from ..merossclient import const as mc

if typing.TYPE_CHECKING:
    from ..helpers.namespaces import DigestParseFunc
    from ..meross_device import MerossDevice

def digest_init(device: "MerossDevice", digest) -> "DigestParseFunc":
    """{ "channel": 0, "capacity": 4 }"""

    MLLight(device, digest)
    return device.namespace_handlers[mc.NS_APPLIANCE_CONTROL_LIGHT]._parse_generic
