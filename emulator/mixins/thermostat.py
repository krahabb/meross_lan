""""""

from random import randint
import typing

from custom_components.meross_lan.helpers import clamp
from custom_components.meross_lan.merossclient import (
    const as mc,
    get_element_by_key,
    namespaces as mn,
    update_dict_strict,
    update_dict_strict_by_key,
)

if typing.TYPE_CHECKING:
    from .. import MerossEmulator, MerossEmulatorDescriptor


class ThermostatMixin(MerossEmulator if typing.TYPE_CHECKING else object):
    MAP_DEVICE_SCALE = {
        "mts200": 10,
        "mts200b": 10,
        "mts960": 100,
    }
    MAP_ENTITY_NS_DEFAULT = {
        mc.KEY_CALIBRATION: {
            mc.KEY_VALUE: 0,
            mc.KEY_MIN: -5,
            mc.KEY_MAX: 5,
        },
        mc.KEY_DEADZONE: {
            mc.KEY_VALUE: 0.5,
            mc.KEY_MIN: 0.5,
            mc.KEY_MAX: 3.5,
        },
        mc.KEY_FROST: {
            mc.KEY_VALUE: 5,
            mc.KEY_MIN: 5,
            mc.KEY_MAX: 15,
            mc.KEY_ONOFF: 0,
            mc.KEY_WARNING: 0,
        },
        mc.KEY_OVERHEAT: {
            mc.KEY_VALUE: 32,
            mc.KEY_MIN: 20,
            mc.KEY_MAX: 70,
            mc.KEY_ONOFF: 0,
            mc.KEY_WARNING: 0,
            mc.KEY_CURRENTTEMP: 32,
        },
    }

    def __init__(self, descriptor: "MerossEmulatorDescriptor", key):
        super().__init__(descriptor, key)
        self.device_scale = self.MAP_DEVICE_SCALE[descriptor.type]

    def _SET_Appliance_Control_Thermostat_Mode(self, header, payload):
        p_digest = self.descriptor.digest
        p_digest_mode_list = p_digest[mc.KEY_THERMOSTAT][mc.KEY_MODE]
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
                p_digest_windowopened_list = p_digest[mc.KEY_THERMOSTAT].get(
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
        p_digest = self.descriptor.digest
        p_digest_modeb_list = p_digest[mc.KEY_THERMOSTAT][mc.KEY_MODEB]
        for p_modeb in payload[mc.KEY_MODEB]:
            p_digest_modeb = update_dict_strict_by_key(p_digest_modeb_list, p_modeb)
            if p_digest_modeb[mc.KEY_ONOFF]:
                p_digest_modeb[mc.KEY_STATE] = (
                    mc.MTS960_STATE_ON
                    if p_digest_modeb[mc.KEY_TARGETTEMP]
                    > p_digest_modeb[mc.KEY_CURRENTTEMP]
                    else mc.MTS960_STATE_OFF
                )
            else:
                p_digest_modeb[mc.KEY_STATE] = mc.MTS960_STATE_UNKNOWN

        return mc.METHOD_SETACK, {mc.KEY_MODEB: p_digest_modeb}

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
        namespace_key = mn.NAMESPACES[namespace].key
        method = header[mc.KEY_METHOD]

        digest: list[dict[str, object]] = self.descriptor.namespaces[namespace][
            namespace_key
        ]
        response_list = []
        for p_request_channel in payload[namespace_key]:
            channel = p_request_channel[mc.KEY_CHANNEL]
            try:
                p_digest_channel = get_element_by_key(digest, mc.KEY_CHANNEL, channel)
            except Exception:
                p_digest_channel = dict(self.MAP_ENTITY_NS_DEFAULT[namespace_key])
                p_digest_channel[mc.KEY_CHANNEL] = channel
                p_digest_channel[mc.KEY_VALUE] = (
                    p_digest_channel[mc.KEY_VALUE] * self.device_scale
                )
                p_digest_channel[mc.KEY_MIN] = (
                    p_digest_channel[mc.KEY_MIN] * self.device_scale
                )
                p_digest_channel[mc.KEY_MAX] = (
                    p_digest_channel[mc.KEY_MAX] * self.device_scale
                )
                digest.append(p_digest_channel)

            p_digest_channel[mc.KEY_LMTIME] = self.epoch

            if method == mc.METHOD_GET:
                # randomize some input in case
                """
                generally speaking the KEY_VALUE hosts a config and not a reading
                some entity ns have additional values like 'Overheat' that carries 'currentTemp'
                """
                if mc.KEY_WARNING in p_digest_channel:
                    p_digest_channel[mc.KEY_WARNING] = randint(0, 2)
                if mc.KEY_CURRENTTEMP in p_digest_channel and randint(0, 5):
                    current_temp = p_digest_channel[mc.KEY_CURRENTTEMP]
                    current_temp += randint(-1, 1) * self.device_scale
                    p_digest_channel[mc.KEY_CURRENTTEMP] = clamp(
                        current_temp,
                        p_digest_channel[mc.KEY_MIN],
                        p_digest_channel[mc.KEY_MAX],
                    )
                response_list.append(p_digest_channel)
            elif method == mc.METHOD_SET:
                update_dict_strict(p_digest_channel, p_request_channel)

        if method == mc.METHOD_GET:
            return mc.METHOD_GETACK, {namespace_key: response_list}
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
