""""""
from __future__ import annotations

from random import randint
import typing

from custom_components.meross_lan.merossclient import const as mc, get_element_by_key

from .. import MerossEmulator, MerossEmulatorDescriptor


class ThermostatMixin(MerossEmulator if typing.TYPE_CHECKING else object):
    def __init__(self, descriptor: MerossEmulatorDescriptor, key):
        super().__init__(descriptor, key)
        # ensure (despite our trace content..) the mc.NS_APPLIANCE_CONTROL_THERMOSTAT_OVERHEAT
        # list is in place
        self._thermostat_overheat: list[dict[str, object]] = descriptor.namespaces[
            mc.NS_APPLIANCE_CONTROL_THERMOSTAT_OVERHEAT
        ][mc.KEY_OVERHEAT]
        for p_digest_thermostat_mode in descriptor.digest[mc.KEY_THERMOSTAT][
            mc.KEY_MODE
        ]:
            channel = p_digest_thermostat_mode[mc.KEY_CHANNEL]
            try:
                get_element_by_key(self._thermostat_overheat, mc.KEY_CHANNEL, channel)
            except Exception:
                self._thermostat_overheat.append(
                    {
                        mc.KEY_CHANNEL: channel,
                        mc.KEY_WARNING: 0,
                        mc.KEY_VALUE: 335,
                        mc.KEY_ONOFF: 0,
                        mc.KEY_MIN: 200,
                        mc.KEY_MAX: 700,
                        mc.KEY_CURRENTTEMP: 355,
                        mc.KEY_LMTIME: 0
                    }
                )

    def _GET_Appliance_Control_Thermostat_Overheat(self, header, payload):
        """
        {
            "overheat": [
                {
                    "channel":0, "warning":0, "value": 335, "onoff": 1,
                    "min": 200, "max": 700, "lmTime": 1674121910, "currentTemp": 355,
                }
            ]
        }
        """
        response_overheat_list = []
        for p_payload_channel in payload[mc.KEY_OVERHEAT]:
            channel = p_payload_channel[mc.KEY_CHANNEL]
            p_overheat = get_element_by_key(
                self._thermostat_overheat, mc.KEY_CHANNEL, channel
            )
            p_overheat[mc.KEY_WARNING] = randint(0, 2)
            p_overheat[mc.KEY_LMTIME] = self.epoch
            response_overheat_list.append(p_overheat)
        return mc.METHOD_GETACK, {mc.KEY_OVERHEAT: response_overheat_list}

    def _SET_Appliance_Control_Thermostat_Mode(self, header, payload):
        p_digest = self.descriptor.digest
        p_digest_mode_list = p_digest[mc.KEY_THERMOSTAT][mc.KEY_MODE]
        p_digest_windowopened_list = {}
        p_mode_list = payload[mc.KEY_MODE]
        for p_mode in p_mode_list:
            channel = p_mode[mc.KEY_CHANNEL]
            p_digest_mode = get_element_by_key(
                p_digest_mode_list, mc.KEY_CHANNEL, channel
            )
            p_digest_mode.update(p_mode)
            mode = p_digest_mode[mc.KEY_MODE]
            MODE_KEY_MAP = {
                mc.MTS200_MODE_HEAT: mc.KEY_HEATTEMP,
                mc.MTS200_MODE_COOL: mc.KEY_COOLTEMP,
                mc.MTS200_MODE_ECO: mc.KEY_ECOTEMP,
                mc.MTS200_MODE_CUSTOM: mc.KEY_MANUALTEMP,
            }
            if mode in MODE_KEY_MAP:
                p_digest_mode[mc.KEY_TARGETTEMP] = p_digest_mode[MODE_KEY_MAP[mode]]
            else:  # we use this to trigger a windowOpened later in code
                p_digest_windowopened_list = p_digest[mc.KEY_THERMOSTAT][
                    mc.KEY_WINDOWOPENED
                ]
            if p_digest_mode[mc.KEY_ONOFF]:
                p_digest_mode[mc.KEY_STATE] = (
                    1
                    if p_digest_mode[mc.KEY_TARGETTEMP]
                    > p_digest_mode[mc.KEY_CURRENTTEMP]
                    else 0
                )
            else:
                p_digest_mode[mc.KEY_STATE] = 0

            # randomly switch the window
            for p_digest_windowopened in p_digest_windowopened_list:
                if p_digest_windowopened[mc.KEY_CHANNEL] == channel:
                    p_digest_windowopened[mc.KEY_STATUS] = (
                        0 if p_digest_windowopened[mc.KEY_STATUS] else 1
                    )
                    break

        return mc.METHOD_SETACK, {}
