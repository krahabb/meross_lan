"""
Obfuscation:

working on a set of well-known keys to hide values from a structure
when logging/tracing.
The 'OBFUSCATE_KEYS' dict mandates which key values are patched and
how (ObfuscateRule). It generally mantains a set of obfuscated values stored in
the ObfuscateMap instance so that every time we obfuscate a key value,
we return the same (stable) obfuscation in order to correlate data in
traces and logs. Some keys are not cached/mapped and just 'redacted'
"""

import re
from typing import TYPE_CHECKING

from .protocol import const as mc

if TYPE_CHECKING:
    from .protocol.types import JsonDict, JsonMapping


class ObfuscateRule:
    """
    Obfuscate data based on the rule defined by the subclass implementation.
    This is the base class for all obfuscation rules, which can be as simple
    as a static redaction or as complex as a mapping with caching of obfuscated values.
    """

    def __call__(self, value): ...

    def clear(self):
        """Resets any cached data"""
        pass


class ObfuscateAny(ObfuscateRule):
    """
    Obfuscate data without caching and mapping. This is needed
    for ever-varying key values like i.e. KEY_PARAMS (in cloudapi requests)
    """

    def __call__(self, value):
        if value.__class__ is dict:
            return {key: self(value) for key, value in value.items()}
        elif value.__class__ is list:
            return [self(item) for item in value]
        else:
            return "<redacted>"


class ObfuscateDict(ObfuscateRule):
    """
    Obfuscate dict values by applying obfuscation rules to the dict items.
    This is needed for ever-varying key values like i.e. KEY_PARAMS (in cloudapi requests)
    which are dicts with varying keys and values.
    """

    def __call__(self, value: "JsonMapping") -> "JsonDict":
        return obfuscated_dict(value)


class ObfuscateMap(ObfuscateRule, dict):
    def __call__(self, value):
        """
        for every value we obfuscate, we'll keep
        a cache of 'unique' obfuscated values in order
        to be able to relate 'stable' identical vales in traces
        for debugging/diagnostics purposes
        """
        if value not in self:
            # first time seen: generate the obfuscation
            count = len(self)
            if isinstance(value, str):
                # we'll preserve string length when obfuscating strings
                obfuscated_value = str(count)
                padding = len(value) - len(obfuscated_value)
                if padding > 0:
                    self[value] = "#" * padding + obfuscated_value
                else:
                    self[value] = "#" + obfuscated_value
            else:
                self[value] = "@" + str(count)

        return self[value]

    def clear(self):
        dict.clear(self)


class ObfuscateUserIdMap(ObfuscateMap):
    def __call__(self, value: str | int):
        # terrible patch here since we want to match
        # values (userid) which are carried both as strings
        # (in mc.KEY_USERID_) and as int (in mc.KEY_USERID)
        try:
            # no type checks before conversion since we're
            # confident its almost an integer decimal number
            value = int(value)
        except Exception:
            # but we play safe anyway
            pass
        return super().__call__(value)


class ObfuscateServerMap(ObfuscateMap):
    def __call__(self, value: str):
        # mc.KEY_DOMAIN and mc.KEY_RESERVEDDOMAIN could
        # carry the protocol port embedded like: "server.domain.com:port"
        # so, in order to map to the same values as in mc.KEY_SERVER,
        # mc.KEY_PORT and the likes we'll need special processing
        try:
            if (colon_index := value.find(":")) != -1:
                host = value[0:colon_index]
                port = int(value[colon_index + 1 :])
                return ":".join(
                    (
                        OBFUSCATE_SERVER_MAP(host),
                        OBFUSCATE_PORT_MAP(port),
                    )
                )
        except Exception:
            pass

        return super().__call__(value)

    def clear(self):
        OBFUSCATE_PORT_MAP.clear()
        return super().clear()


class ObfuscateFrom(ObfuscateRule):
    """
    Obfuscate the "from" payload field which may carry the device "uuid"
    or the "userid"
    """

    def __call__(self, value: str):
        """
        Renders the obfuscated uuid in place like:
        "/appliance/###############################0/publish"
        or, when matching an userid like:
        "/app/########0-whatever/subscribe
        """
        # start by matching the eventual userid since the pattern is more specific
        if m := mc.RE_PATTERN_TOPIC_USERID.match(value):
            return "".join((m.group(1), OBFUSCATE_USERID_MAP(m.group(2)), m.group(3)))

        # this is a 'broad matcher' capturing whatever looks like an UUID (32 alfanumerics)
        def _sub(match: re.Match):
            return "".join(
                (
                    match.group(1),
                    OBFUSCATE_UUID_MAP(match.group(2)),
                    match.group(3),
                )
            )

        return mc.RE_PATTERN_UUID.sub(_sub, value)

    def clear(self):
        OBFUSCATE_USERID_MAP.clear()
        OBFUSCATE_UUID_MAP.clear()


# common (shared) obfuscation mappings for related keys
OBFUSCATE_ANY = ObfuscateAny()
OBFUSCATE_DICT = ObfuscateDict()
OBFUSCATE_UUID_MAP = ObfuscateMap({})
OBFUSCATE_HOST_MAP = ObfuscateMap({})
OBFUSCATE_USERID_MAP = ObfuscateUserIdMap({})
OBFUSCATE_SERVER_MAP = ObfuscateServerMap({})
OBFUSCATE_PORT_MAP = ObfuscateMap({})
OBFUSCATE_KEY_MAP = ObfuscateMap({})
OBFUSCATE_KEYS: dict[str, ObfuscateRule] = {
    # MEROSS PROTOCOL PAYLOADS keys
    # devices uuid(s) is better obscured since knowing this
    # could allow malicious attempts at the public Meross mqtt to
    # correctly address the device (with some easy hacks on signing)
    mc.KEY_UUID: OBFUSCATE_UUID_MAP,
    mc.KEY_FROM: ObfuscateFrom(),
    mc.KEY_MACADDRESS: ObfuscateMap({}),
    mc.KEY_WIFIMAC: ObfuscateMap({}),
    mc.KEY_SSID: ObfuscateMap({}),
    mc.KEY_GATEWAYMAC: ObfuscateMap({}),
    mc.KEY_INNERIP: OBFUSCATE_HOST_MAP,
    mc.KEY_SERVER: OBFUSCATE_SERVER_MAP,
    mc.KEY_PORT: OBFUSCATE_PORT_MAP,
    mc.KEY_SECONDSERVER: OBFUSCATE_SERVER_MAP,
    mc.KEY_SECONDPORT: OBFUSCATE_PORT_MAP,
    mc.KEY_ACTIVESERVER: OBFUSCATE_SERVER_MAP,
    mc.KEY_MAINSERVER: OBFUSCATE_SERVER_MAP,
    mc.KEY_MAINPORT: OBFUSCATE_PORT_MAP,
    mc.KEY_USERID: OBFUSCATE_USERID_MAP,
    mc.KEY_SN: ObfuscateMap({}),
    mc.KEY_SETUPID: ObfuscateMap({}),
    mc.KEY_SETUPCODE: ObfuscateMap({}),
    mc.KEY_TOKEN: ObfuscateMap({}),
    mc.KEY_KEY: OBFUSCATE_KEY_MAP,
    #
    # MEROSS CLOUD HTTP API KEYS
    mc.KEY_USERID_: OBFUSCATE_USERID_MAP,  # MerossCloudCredentials
    mc.KEY_EMAIL: ObfuscateMap({}),  # MerossCloudCredentials
    # mc.KEY_KEY: OBFUSCATE_KEY_MAP,# MerossCloudCredentials
    # mc.KEY_TOKEN: ObfuscateMap({}),# MerossCloudCredentials
    mc.KEY_DOMAIN: OBFUSCATE_SERVER_MAP,  # MerossCloudCredentials and DeviceInfoType
    mc.KEY_MQTTDOMAIN: OBFUSCATE_SERVER_MAP,  # MerossCloudCredentials
    mc.KEY_CLUSTER: ObfuscateMap({}),  # DeviceInfoType
    mc.KEY_RESERVEDDOMAIN: OBFUSCATE_SERVER_MAP,  # DeviceInfoType
    mc.KEY_PARAMS: OBFUSCATE_ANY,  # used in cloudapi POST request
    "Authorization": OBFUSCATE_ANY,  # used in cloudapi POST headers
    # subdevice(s) ids are hardly sensitive since they
    # cannot be accessed over the api without knowing the uuid
    # of the hub device (which is obfuscated indeed). Masking
    # this would also require to obfuscate mc.KEY_ID used by hubs
    # and dumped in traces
    # mc.KEY_SUBDEVICEID: {},
    #
    # These keys are typically used in logging._Logger to match keyvalue arguments that need to be obfuscated when
    # sent to the underlying Logger. Due to the hierarchical nature of logging.Loggable, this allows
    # to control the effective obfuscation at an higher level in the Loggavles runtime instance tree.
    # The logging code actually matches any key in OBFUSCATE_KEYS and applies the obfuscation rule to
    # the value before sending it to the underlying Logger.
    #
    "_message": OBFUSCATE_DICT,
    "_header": OBFUSCATE_DICT,
    "_payload": OBFUSCATE_DICT,
    "_any": OBFUSCATE_ANY,  # catch-all for any key we may have missed and want to obfuscate in a generic way
}


def obfuscated_list(data: list):
    """
    List obfuscation: recursevely invokes dict/list obfuscation on the list items.
    Simple objects are not obfuscated.
    """
    return [
        (
            obfuscated_dict(value)
            if value.__class__ is dict
            else obfuscated_list(value) if value.__class__ is list else value
        )
        for value in data
    ]


def obfuscated_dict(data: "JsonMapping") -> "JsonDict":
    """Dictionary obfuscation based on the set keys defined in OBFUSCATE_KEYS."""
    return {
        key: (
            obfuscated_dict(value)
            if value.__class__ is dict
            else (
                obfuscated_list(value)
                if value.__class__ is list
                else (OBFUSCATE_KEYS[key](value) if key in OBFUSCATE_KEYS else value)
            )
        )
        for key, value in data.items()
    }
