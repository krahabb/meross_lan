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
import typing

from .. import const as mlc
from ..merossclient import const as mc


class ObfuscateRule:
    """
    Obfuscate data without caching and mapping. This is needed
    for ever-varying key values like i.e. KEY_PARAMS (in cloudapi requests)
    """

    def obfuscate(self, value):
        return "<redacted>"


class ObfuscateMap(ObfuscateRule, dict):
    def obfuscate(self, value):
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


class ObfuscateUserIdMap(ObfuscateMap):
    def obfuscate(self, value: str | int):
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
        return super().obfuscate(value)


class ObfuscateServerMap(ObfuscateMap):
    def obfuscate(self, value: str):
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
                        OBFUSCATE_SERVER_MAP.obfuscate(host),
                        OBFUSCATE_PORT_MAP.obfuscate(port),
                    )
                )
        except Exception:
            pass

        return super().obfuscate(value)


# common (shared) obfuscation mappings for related keys
OBFUSCATE_NO_MAP = ObfuscateRule()
OBFUSCATE_DEVICE_ID_MAP = ObfuscateMap({})
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
    mc.KEY_UUID: OBFUSCATE_DEVICE_ID_MAP,
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
    mc.KEY_PARAMS: OBFUSCATE_NO_MAP,  # used in cloudapi POST request
    "Authorization": OBFUSCATE_NO_MAP,  # used in cloudapi POST headers
    # subdevice(s) ids are hardly sensitive since they
    # cannot be accessed over the api without knowing the uuid
    # of the hub device (which is obfuscated indeed). Masking
    # this would also require to obfuscate mc.KEY_ID used by hubs
    # and dumped in traces
    # mc.KEY_SUBDEVICEID: {},
    #
    # ConfigEntries keys
    mlc.CONF_DEVICE_ID: OBFUSCATE_DEVICE_ID_MAP,
    mlc.CONF_HOST: OBFUSCATE_HOST_MAP,
    # mlc.CONF_KEY: OBFUSCATE_KEY_MAP,
    mlc.CONF_CLOUD_KEY: OBFUSCATE_KEY_MAP,
    mlc.CONF_PASSWORD: OBFUSCATE_NO_MAP,
    #
    # MerossCloudProfile keys
    "appId": ObfuscateMap({}),
}


def obfuscated_list(data: list):
    """
    List obfuscation: recursevely invokes dict/list obfuscation on the list items.
    Simple objects are not obfuscated.
    """
    return [
        obfuscated_dict(value)
        if isinstance(value, dict)
        else obfuscated_list(value)
        if isinstance(value, list)
        else value
        for value in data
    ]


def obfuscated_dict(data: typing.Mapping[str, typing.Any]) -> dict[str, typing.Any]:
    """Dictionary obfuscation based on the set keys defined in OBFUSCATE_KEYS."""
    return {
        key: obfuscated_dict(value)
        if isinstance(value, dict)
        else obfuscated_list(value)
        if isinstance(value, list)
        else OBFUSCATE_KEYS[key].obfuscate(value)
        if key in OBFUSCATE_KEYS
        else value
        for key, value in data.items()
    }


def obfuscated_any(value):
    """Generalized type-variant obfuscation. Simple objects (not dict/list) are obfuscated."""
    return (
        obfuscated_dict(value)
        if isinstance(value, dict)
        else obfuscated_list(value)
        if isinstance(value, list)
        else OBFUSCATE_NO_MAP.obfuscate(value)
    )
