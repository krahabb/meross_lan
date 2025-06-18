import typing

from ...helpers.namespaces import NamespaceHandler, mc, mn
from ...merossclient.protocol.namespaces import thermostat as mn_t
from .mts200 import Mts200Climate
from .mts960 import Mts960Climate
from .mtsthermostat import (
    MLScreenBrightnessNumber,
    MtsDeadZoneNumber,
    MtsExternalSensorSwitch,
    MtsFrostNumber,
    MtsOverheatNumber,
    MtsWindowOpened,
)

if typing.TYPE_CHECKING:
    from typing import Any, Callable, Unpack

    from ...helpers.device import Device, DigestInitReturnType, DigestParseFunc
    from ...merossclient.protocol import types as mt
    from ...merossclient.protocol.namespaces import Namespace
    from .mtsthermostat import MtsThermostatClimate

    # MtsThermostatClimate = Mts200Climate | Mts960Climate


CLIMATE_INITIALIZERS: dict[str, type["MtsThermostatClimate"]] = {
    mc.KEY_MODE: Mts200Climate,
    mc.KEY_MODEB: Mts960Climate,
}
"""Core (climate) entities to initialize in _init_thermostat"""

DIGEST_KEY_TO_NAMESPACE: dict[str, "Namespace"] = {
    mc.KEY_MODE: mn_t.Appliance_Control_Thermostat_Mode,
    mc.KEY_MODEB: mn_t.Appliance_Control_Thermostat_ModeB,
    mc.KEY_SUMMERMODE: mn_t.Appliance_Control_Thermostat_SummerMode,
    mc.KEY_WINDOWOPENED: mn_t.Appliance_Control_Thermostat_WindowOpened,
}
"""Maps the digest key to the associated namespace handler (used in _parse_thermostat)"""

# "Mode", "ModeB","SummerMode","WindowOpened" are carried in digest so we don't poll them
# We're using PollingStrategy for namespaces actually confirmed (by trace/diagnostics)
# to be PUSHED when over MQTT. The rest are either 'never seen' or 'not pushed'


def digest_init_thermostat(device: "Device", digest: dict) -> "DigestInitReturnType":

    ability = device.descriptor.ability

    digest_handlers: dict[str, "DigestParseFunc"] = {}
    digest_pollers: set["NamespaceHandler"] = set()

    for ns_key, ns_digest in digest.items():

        try:
            ns = DIGEST_KEY_TO_NAMESPACE[ns_key]
        except KeyError:
            # ns_key is still not mapped in DIGEST_KEY_TO_NAMESPACE
            for namespace in ability:
                ns = mn.NAMESPACES[namespace]
                if ns.is_thermostat and (ns.key == ns_key):
                    DIGEST_KEY_TO_NAMESPACE[ns_key] = ns
                    break
            else:
                # ns_key is really unknown..
                digest_handlers[ns_key] = device.digest_parse_empty
                continue

        handler = device.get_handler(ns)
        digest_handlers[ns_key] = handler.parse_list
        digest_pollers.add(handler)

        if climate_class := CLIMATE_INITIALIZERS.get(ns_key):
            for channel_digest in ns_digest:
                climate_class(device, channel_digest[mc.KEY_CHANNEL])

    def digest_parse(digest: dict):
        """
        MTS200 typically carries:
        {
            "mode": [...],
            "summerMode": [],
            "windowOpened": []
        }
        MTS960 typically carries:
        {
            "modeB": [...]
        }
        """
        for ns_key, ns_digest in digest.items():
            digest_handlers[ns_key](ns_digest)

    return digest_parse, digest_pollers


class ScreenBrightnessNamespaceHandler(NamespaceHandler):
    """
    This ns only appears with thermostats so far.. so we put it here but it could
    nevertheless live in its own module (or in 'misc' maybe)
    """

    __slots__ = (
        "number_brightness_operation",
        "number_brightness_standby",
    )

    def __init__(self, device: "Device"):
        NamespaceHandler.__init__(
            self,
            device,
            mn.Appliance_Control_Screen_Brightness,
            handler=self._handle_Appliance_Control_Screen_Brightness,
        )
        self.polling_request_add_channel(0)
        self.number_brightness_operation = MLScreenBrightnessNumber(
            device, mc.KEY_OPERATION
        )
        self.number_brightness_standby = MLScreenBrightnessNumber(
            device, mc.KEY_STANDBY
        )

    def _handle_Appliance_Control_Screen_Brightness(
        self, header: "mt.MerossHeaderType", payload: "mt.MerossPayloadType", /
    ):
        for p_channel in payload[mc.KEY_BRIGHTNESS]:
            if p_channel[mc.KEY_CHANNEL] == 0:
                self.number_brightness_operation.update_device_value(
                    p_channel[mc.KEY_OPERATION]
                )
                self.number_brightness_standby.update_device_value(
                    p_channel[mc.KEY_STANDBY]
                )
                break
