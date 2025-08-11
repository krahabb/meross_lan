""""""

from random import randint
from typing import TYPE_CHECKING

from custom_components.meross_lan.helpers import clamp
from custom_components.meross_lan.merossclient import (
    get_element_by_key,
    update_dict_strict,
    update_dict_strict_by_key,
)
from custom_components.meross_lan.merossclient.protocol import (
    const as mc,
    namespaces as mn,
)
from custom_components.meross_lan.merossclient.protocol.namespaces import (
    thermostat as mn_t,
)

from . import MerossEmulator

if TYPE_CHECKING:
    from custom_components.meross_lan.merossclient.protocol.types import thermostat

    from . import MerossEmulatorDescriptor


class ThermostatMixin(MerossEmulator if TYPE_CHECKING else object):

    NAMESPACES_DEFAULT: "MerossEmulator.NSDefault" = {
        mn.Appliance_Control_TempUnit: (
            MerossEmulator.NSDefaultMode.MixOut,
            {mc.KEY_CHANNEL: 0, "tempUnit": 1},
        ),
        mn_t.Appliance_Control_Thermostat_HoldAction: (
            MerossEmulator.NSDefaultMode.MixOut,
            {mc.KEY_CHANNEL: 0, "mode": 0, "time": 0},
        ),
    }

    MAP_DEVICE_SCALE = {
        "mts200": 10,
        "mts200b": 10,
        "mts300": 100,
        "mts960": 100,
    }

    def __init__(self, descriptor: "MerossEmulatorDescriptor", key):
        super().__init__(descriptor, key)
        self.device_scale = self.MAP_DEVICE_SCALE[descriptor.type]
        # sanityze
        ns = mn_t.Appliance_Control_Thermostat_Calibration
        if ns.name in descriptor.ability:
            self.update_namespace_state(
                ns,
                MerossEmulator.NSDefaultMode.MixOut,
                (
                    {
                        mc.KEY_CHANNEL: 0,
                        "value": 0,
                        "max": 450,
                        "min": -450,
                        "humiValue": 0,
                    }
                    if descriptor.type.startswith("mts300")
                    else (
                        {
                            mc.KEY_CHANNEL: 0,
                            "value": 0,
                            "max": 2000,
                            "min": -2000,
                        }
                        if descriptor.type.startswith("mts960")
                        else {
                            mc.KEY_CHANNEL: 0,
                            "value": 0,
                            "max": 8 * self.device_scale,
                            "min": -8 * self.device_scale,
                        }
                    )
                ),
            )
        ns = mn_t.Appliance_Control_Thermostat_DeadZone
        if ns.name in descriptor.ability:
            self.update_namespace_state(
                ns,
                MerossEmulator.NSDefaultMode.MixOut,
                {
                    mc.KEY_CHANNEL: 0,
                    "value": 0.5 * self.device_scale,
                    "max": 3.5 * self.device_scale,
                    "min": 0.5 * self.device_scale,
                },
            )
        ns = mn_t.Appliance_Control_Thermostat_Frost
        if ns.name in descriptor.ability:
            self.update_namespace_state(
                ns,
                MerossEmulator.NSDefaultMode.MixOut,
                {
                    mc.KEY_CHANNEL: 0,
                    "value": 0.5 * self.device_scale,
                    "max": 3.5 * self.device_scale,
                    "min": 0.5 * self.device_scale,
                    "onoff": 0,
                    "warning": 0,
                },
            )
        ns = mn_t.Appliance_Control_Thermostat_Overheat
        if ns.name in descriptor.ability:
            self.update_namespace_state(
                ns,
                MerossEmulator.NSDefaultMode.MixOut,
                {
                    mc.KEY_CHANNEL: 0,
                    "value": 32 * self.device_scale,
                    "max": 70 * self.device_scale,
                    "min": 20 * self.device_scale,
                    "onoff": 0,
                    "warning": 0,
                    "currentTemp": 32 * self.device_scale,
                },
            )

    def _SET_Appliance_Control_TempUnit(self, header, payload):
        ns = mn.Appliance_Control_TempUnit
        p_channel_state_list = self.namespaces[ns.name][ns.key]
        for p_channel in payload[ns.key]:
            p_channel_state = update_dict_strict_by_key(p_channel_state_list, p_channel)
        return mc.METHOD_SETACK, {ns.key: p_channel_state_list}

    def _SET_Appliance_Control_Thermostat_Mode(self, header, payload):
        p_digest_thermostat = self.descriptor.digest[mc.KEY_THERMOSTAT]
        p_digest_mode_list = p_digest_thermostat[mc.KEY_MODE]
        p_digest_windowopened_list = []
        for p_mode in payload[mc.KEY_MODE]:
            channel = p_mode[mc.KEY_CHANNEL]
            p_digest_mode = update_dict_strict_by_key(p_digest_mode_list, p_mode)
            mode = p_digest_mode[mc.KEY_MODE]
            if mode in mc.MTS200_MODE_TO_TARGETTEMP_MAP:
                p_digest_mode[mc.KEY_TARGETTEMP] = p_digest_mode[
                    mc.MTS200_MODE_TO_TARGETTEMP_MAP[mode]
                ]
            else:  # we use this to trigger a windowOpened later in code
                p_digest_windowopened_list = p_digest_thermostat.get(
                    mc.KEY_WINDOWOPENED, []
                )
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

    def _SET_Appliance_Control_Thermostat_ModeB(self, header, payload):
        p_digest_modeb_list = self.descriptor.digest[mc.KEY_THERMOSTAT][mc.KEY_MODEB]
        for p_modeb in payload[mc.KEY_MODEB]:
            p_digest_modeb = update_dict_strict_by_key(p_digest_modeb_list, p_modeb)
            if p_digest_modeb[mc.KEY_ONOFF]:
                match p_digest_modeb[mc.KEY_MODE]:
                    case mc.MTS960_MODE_HEAT_COOL:
                        match p_digest_modeb[mc.KEY_WORKING]:
                            case mc.MTS960_WORKING_HEAT:
                                p_digest_modeb[mc.KEY_STATE] = (
                                    mc.MTS960_STATE_ON
                                    if p_digest_modeb[mc.KEY_TARGETTEMP]
                                    > p_digest_modeb[mc.KEY_CURRENTTEMP]
                                    else mc.MTS960_STATE_OFF
                                )
                            case mc.MTS960_WORKING_COOL:
                                p_digest_modeb[mc.KEY_STATE] = (
                                    mc.MTS960_STATE_ON
                                    if p_digest_modeb[mc.KEY_TARGETTEMP]
                                    < p_digest_modeb[mc.KEY_CURRENTTEMP]
                                    else mc.MTS960_STATE_OFF
                                )
                    case mc.MTS960_MODE_SCHEDULE:
                        pass
                    case mc.MTS960_MODE_TIMER:
                        pass
            else:
                p_digest_modeb[mc.KEY_STATE] = mc.MTS960_STATE_UNKNOWN
        # WARNING: returning only the last element of the loop (usually just 1 item per device tho)
        return mc.METHOD_SETACK, {mc.KEY_MODEB: [p_digest_modeb]}

    def _SET_Appliance_Control_Thermostat_ModeC(self, header, payload):
        ns = mn_t.Appliance_Control_Thermostat_ModeC
        p_digest_modec_list = self.namespaces[ns.name][ns.key]
        for p_modec in payload[ns.key]:
            p_digest_modec: "thermostat.ModeC_C" = update_dict_strict_by_key(
                p_digest_modec_list, p_modec
            )
            p_fan = p_digest_modec["fan"]
            fan_mode = p_fan["fMode"]
            fan_speed = p_fan["speed"]
            # actually we assume (here and in component)
            # (fan_speed != 0) <-> (fMode == MANUAL)
            p_more = p_digest_modec["more"]
            currenttemp = p_digest_modec["currentTemp"]
            p_targettemp = p_digest_modec["targetTemp"]
            match p_digest_modec[mc.KEY_MODE]:
                case mc.MTS300_MODE_OFF:
                    p_more["hStatus"] = 0
                    p_more["cStatus"] = 0
                    p_more["fStatus"] = 0
                case mc.MTS300_MODE_HEAT:
                    delta_t = round(
                        (p_targettemp["heat"] - currenttemp) / self.device_scale
                    )
                    p_more["hStatus"] = (
                        0 if delta_t <= 0 else 3 if delta_t >= 3 else delta_t
                    )
                    p_more["cStatus"] = 0
                    p_more["fStatus"] = fan_speed or p_more["hStatus"]
                case mc.MTS300_MODE_COOL:
                    p_more["hStatus"] = 0
                    delta_t = round(
                        (currenttemp - p_targettemp["cold"]) / self.device_scale
                    )
                    p_more["cStatus"] = (
                        0 if delta_t <= 0 else 2 if delta_t >= 2 else delta_t
                    )
                    p_more["fStatus"] = fan_speed or p_more["cStatus"]
                case mc.MTS300_MODE_AUTO:
                    delta_t = round(
                        (p_targettemp["heat"] - currenttemp) / self.device_scale
                    )
                    if delta_t > 0:
                        p_more["hStatus"] = 3 if delta_t >= 3 else delta_t
                        p_more["cStatus"] = 0
                        p_more["fStatus"] = fan_speed or p_more["hStatus"]
                    else:
                        delta_t = round(
                            (currenttemp - p_targettemp["cold"]) / self.device_scale
                        )
                        p_more["hStatus"] = 0
                        p_more["cStatus"] = (
                            0 if delta_t <= 0 else 2 if delta_t >= 2 else delta_t
                        )
                        p_more["fStatus"] = fan_speed or p_more["cStatus"]

        # WARNING: returning only the last element of the loop (usually just 1 item per device tho)
        return mc.METHOD_SETACK, {ns.key: [p_digest_modec]}

    def _handle_Appliance_Control_Thermostat_Any(self, header, payload):
        """
        {
            "frost": [
                {
                    "channel":0, "warning":0, "value": 335, "onoff": 1,
                    "min": 200, "max": 700
                }
            ]
        }
        """
        namespace = header[mc.KEY_NAMESPACE]
        method = header[mc.KEY_METHOD]

        ns = self.NAMESPACES[namespace]
        ns_key = ns.key

        p_state: list[dict[str, object]] = self.namespaces[namespace][ns_key]
        response_list = []
        for p_request_channel in payload[ns_key]:
            channel = p_request_channel[mc.KEY_CHANNEL]
            p_channel_state = get_element_by_key(p_state, ns.key_channel, channel)
            p_channel_state[mc.KEY_LMTIME] = self.epoch
            if method == mc.METHOD_GET:
                # randomize some input in case
                """
                generally speaking the KEY_VALUE hosts a config and not a reading
                some entity ns have additional values like 'Overheat' that carries 'currentTemp'
                """
                if mc.KEY_WARNING in p_channel_state:
                    p_channel_state[mc.KEY_WARNING] = randint(0, 2)
                if mc.KEY_CURRENTTEMP in p_channel_state and randint(0, 5):
                    current_temp = p_channel_state[mc.KEY_CURRENTTEMP]
                    current_temp += randint(-1, 1) * self.device_scale
                    p_channel_state[mc.KEY_CURRENTTEMP] = clamp(
                        current_temp,
                        p_channel_state[mc.KEY_MIN],
                        p_channel_state[mc.KEY_MAX],
                    )
                response_list.append(p_channel_state)
            elif method == mc.METHOD_SET:
                update_dict_strict(p_channel_state, p_request_channel)

        if method == mc.METHOD_GET:
            return mc.METHOD_GETACK, {ns_key: response_list}
        elif method == mc.METHOD_SET:
            return mc.METHOD_SETACK, {}
        else:
            raise Exception(f"unsupported request method {method}")

    def _GET_Appliance_Control_Thermostat_Calibration(self, header, payload):
        return self._handle_Appliance_Control_Thermostat_Any(header, payload)

    def _SET_Appliance_Control_Thermostat_Calibration(self, header, payload):
        return self._handle_Appliance_Control_Thermostat_Any(header, payload)

    def _GET_Appliance_Control_Thermostat_Frost(self, header, payload):
        return self._handle_Appliance_Control_Thermostat_Any(header, payload)

    def _SET_Appliance_Control_Thermostat_Frost(self, header, payload):
        return self._handle_Appliance_Control_Thermostat_Any(header, payload)

    def _GET_Appliance_Control_Thermostat_DeadZone(self, header, payload):
        return self._handle_Appliance_Control_Thermostat_Any(header, payload)

    def _SET_Appliance_Control_Thermostat_DeadZone(self, header, payload):
        return self._handle_Appliance_Control_Thermostat_Any(header, payload)

    def _GET_Appliance_Control_Thermostat_Overheat(self, header, payload):
        return self._handle_Appliance_Control_Thermostat_Any(header, payload)

    def _SET_Appliance_Control_Thermostat_Overheat(self, header, payload):
        return self._handle_Appliance_Control_Thermostat_Any(header, payload)
