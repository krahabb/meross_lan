""""""

import math
from random import randint
from typing import TYPE_CHECKING, override

from custom_components.meross_lan.helpers import clamp
from custom_components.meross_lan.merossclient import (
    get_element_by_key,
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
    from typing import Any, ClassVar, Mapping

    from custom_components.meross_lan.merossclient.protocol.types import (
        thermostat as mt_t,
    )

    from . import MerossEmulatorDescriptor


class ThermostatMixin(MerossEmulator if TYPE_CHECKING else object):

    NAMESPACES_DEFAULT: "MerossEmulator.NSDefault" = {
        mn.Appliance_Control_TempUnit: (
            MerossEmulator.NSDefaultMode.MixOut,
            {mc.KEY_CHANNEL: 0, "tempUnit": 1},
        ),
        mn_t.Appliance_Control_Thermostat_CtlRange: (
            MerossEmulator.NSDefaultMode.MixOut,
            {
                mc.KEY_CHANNEL: 0,
                "max": 11000,
                "min": -3000,
                "ctlMax": 5000,
                "ctlMin": 0,
            },
        ),
        mn_t.Appliance_Control_Thermostat_HoldAction: (
            MerossEmulator.NSDefaultMode.MixOut,
            {mc.KEY_CHANNEL: 0, "mode": 0, "time": 0},
        ),
    }

    if TYPE_CHECKING:
        CURRENT_TEMPERATURE_MEAN: ClassVar[float]
        CURRENT_TEMPERATURE_DELTA: ClassVar[float]
        CURRENT_TEMPERATURE_PERIOD: ClassVar[float]

        device_scale: int
        p_mode: mt_t.Mode_C | mt_t.ModeB_C | mt_t.ModeC_C

    MAP_DEVICE = {
        "mts2": (
            10,
            {
                mc.KEY_CHANNEL: 0,
                "value": 0,
                "max": 80,
                "min": -80,
            },
        ),
        "mts3": (
            100,
            {
                mc.KEY_CHANNEL: 0,
                "value": 0,
                "max": 450,
                "min": -450,
                "humiValue": 0,
            },
        ),
        "mts9": (
            100,
            {
                mc.KEY_CHANNEL: 0,
                "value": 0,
                "max": 2000,
                "min": -2000,
            },
        ),
    }

    # Implement an oscillator to emulate current temperature change
    CURRENT_TEMPERATURE_MEAN = 18
    CURRENT_TEMPERATURE_DELTA = 5  # amplitude (half)
    CURRENT_TEMPERATURE_PERIOD = 300  # period of full cycle (seconds)

    def __init__(self, descriptor: "MerossEmulatorDescriptor", key):
        for _type in self.MAP_DEVICE:
            if descriptor.type.startswith(_type):
                break
        else:
            raise RuntimeError("Unsupported thermostat type " + descriptor.type)
        self.device_scale = self.MAP_DEVICE[_type][0]

        super().__init__(descriptor, key)

        # sanityze
        ability = descriptor.ability
        ns = mn_t.Appliance_Control_Thermostat_Calibration
        if ns.name in ability:
            self.update_namespace_state(
                ns,
                MerossEmulator.NSDefaultMode.MixOut,
                self.MAP_DEVICE[_type][1],
            )
        ns = mn_t.Appliance_Control_Thermostat_DeadZone
        if ns.name in ability:
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
        if ns.name in ability:
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
        if ns.name in ability:
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

        if mn_t.Appliance_Control_Thermostat_Mode.name in ability:
            self.mode_ns = mn_t.Appliance_Control_Thermostat_Mode
            p_mode: "mt_t.Mode_C" = descriptor.digest[mc.KEY_THERMOSTAT][mc.KEY_MODE][0]
            self.p_mode = p_mode
            self.temp_min = p_mode[mc.KEY_MIN]
            self.temp_max = p_mode[mc.KEY_MAX]
            self.update_state_func = lambda: self._update_Mode(p_mode)
        elif mn_t.Appliance_Control_Thermostat_ModeB.name in ability:
            self.mode_ns = mn_t.Appliance_Control_Thermostat_ModeB
            p_modeb: "mt_t.ModeB_C" = descriptor.digest[mc.KEY_THERMOSTAT][
                mc.KEY_MODEB
            ][0]
            self.p_mode = p_modeb
            p_ctlrange: "mt_t.CtlRange_C" = self.get_namespace_state(
                mn_t.Appliance_Control_Thermostat_CtlRange, 0
            )
            self.temp_min = p_ctlrange[mc.KEY_MIN]
            self.temp_max = p_ctlrange[mc.KEY_MAX]
            self.update_state_func = lambda: self._update_ModeB(p_modeb)
        elif mn_t.Appliance_Control_Thermostat_ModeC.name in ability:
            self.mode_ns = mn_t.Appliance_Control_Thermostat_ModeC
            p_modec: "mt_t.ModeC_C" = self.namespaces[
                mn_t.Appliance_Control_Thermostat_ModeC.name
            ][mn_t.Appliance_Control_Thermostat_ModeC.key][0]
            self.p_mode = p_modec
            self.temp_min = 5 * self.device_scale
            self.temp_max = 35 * self.device_scale
            self.update_state_func = lambda: self._update_ModeC(p_modec)
        else:
            raise RuntimeError("Unsupported thermostat")

    @override
    def _scheduler(self):
        super()._scheduler()
        # Apply current temp 'modulation' and update internal state.
        # We assume only channel == 0 here.
        self._update_current_temp()

    def _handler_default(self, method: str, namespace: str, payload: "Mapping"):
        if not namespace in (
            mn_t.Appliance_Control_Thermostat_Calibration.name,
            mn_t.Appliance_Control_Thermostat_Frost.name,
            mn_t.Appliance_Control_Thermostat_DeadZone.name,
            mn_t.Appliance_Control_Thermostat_Overheat.name,
        ):
            return super()._handler_default(method, namespace, payload)

        """ Namespace layout as in... (with some exceptions)
        {
            "frost": [
                {
                    "channel":0, "warning":0, "value": 335, "onoff": 1,
                    "min": 200, "max": 700
                }
            ]
        }
        """

        ns = self.NAMESPACES[namespace]
        ns_key = ns.key
        p_state: list[dict[str, Any]] = self.namespaces[namespace][ns_key]
        match method:
            case mc.METHOD_GET:
                response_list = []
                for p_channel_request in payload[ns_key]:
                    channel = p_channel_request[mc.KEY_CHANNEL]
                    p_channel_state = get_element_by_key(
                        p_state, ns.key_channel, channel
                    )
                    response_list.append(p_channel_state)
                    # randomize some input in case
                    """
                    generally speaking the KEY_VALUE hosts a config and not a measure
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
                        p_channel_state[mc.KEY_LMTIME] = self.epoch

                return mc.METHOD_GETACK, {ns_key: response_list}

            case mc.METHOD_SET:
                for p_channel_request in payload[ns_key]:
                    channel = p_channel_request[mc.KEY_CHANNEL]
                    p_channel_state = get_element_by_key(
                        p_state, ns.key_channel, channel
                    )
                    _changed = False
                    if mc.KEY_VALUE in p_channel_state:
                        try:
                            value = clamp(
                                p_channel_request[mc.KEY_VALUE],
                                p_channel_state[mc.KEY_MIN],
                                p_channel_state[mc.KEY_MAX],
                            )
                            if p_channel_state[mc.KEY_VALUE] != value:
                                p_channel_state[mc.KEY_VALUE] = value
                                _changed = True
                                if ns is mn_t.Appliance_Control_Thermostat_Calibration:
                                    self._update_current_temp()
                        except KeyError:
                            pass
                    if mc.KEY_ONOFF in p_channel_state:
                        try:
                            value = p_channel_request[mc.KEY_ONOFF]
                            if p_channel_state[mc.KEY_ONOFF] != value:
                                p_channel_state[mc.KEY_ONOFF] = value
                                _changed = True
                        except KeyError:
                            pass
                    if _changed and ns.has_push and self.mqtt_connected:
                        self.mqtt_publish_push(namespace, {ns_key: [p_channel_state]})

                return mc.METHOD_SETACK, {}
            case _:
                raise Exception(f"unsupported request method {method}")

    def _SET_Appliance_Control_TempUnit(self, header, payload):
        ns = mn.Appliance_Control_TempUnit
        p_channel_state_list = self.namespaces[ns.name][ns.key]
        for p_channel in payload[ns.key]:
            p_channel_state = update_dict_strict_by_key(p_channel_state_list, p_channel)
        return mc.METHOD_SETACK, {ns.key: p_channel_state_list}

    def _SET_Appliance_Control_Thermostat_Mode(self, header, payload):
        p_digest_thermostat = self.descriptor.digest[mc.KEY_THERMOSTAT]
        p_digest_mode_list = p_digest_thermostat[mc.KEY_MODE]
        for p_mode in payload[mc.KEY_MODE]:
            p_digest_mode = update_dict_strict_by_key(p_digest_mode_list, p_mode)
            self._update_Mode(p_digest_mode)

        return mc.METHOD_SETACK, {}

    def _SET_Appliance_Control_Thermostat_ModeB(self, header, payload):
        p_digest_modeb_list = self.descriptor.digest[mc.KEY_THERMOSTAT][mc.KEY_MODEB]
        for p_modeb in payload[mc.KEY_MODEB]:
            p_digest_modeb = update_dict_strict_by_key(p_digest_modeb_list, p_modeb)
            self._update_ModeB(p_digest_modeb)
        # WARNING: returning only the last element of the loop (usually just 1 item per device tho)
        return mc.METHOD_SETACK, {mc.KEY_MODEB: [p_digest_modeb]}

    def _SET_Appliance_Control_Thermostat_ModeC(self, header, payload):
        ns = mn_t.Appliance_Control_Thermostat_ModeC
        p_digest_modec_list = self.namespaces[ns.name][ns.key]
        for p_modec in payload[ns.key]:
            p_digest_modec: "mt_t.ModeC_C" = update_dict_strict_by_key(
                p_digest_modec_list, p_modec
            )
            self._update_ModeC(p_digest_modec)

        # WARNING: returning only the last element of the loop (usually just 1 item per device tho)
        return mc.METHOD_SETACK, {ns.key: [p_digest_modec]}

    def _update_Mode(self, p_mode: "mt_t.Mode_C"):
        try:
            p_mode[mc.KEY_TARGETTEMP] = p_mode[
                mc.MTS200_MODE_TO_TARGETTEMP_MAP[p_mode[mc.KEY_MODE]]
            ]
        except KeyError:
            pass

        if p_mode[mc.KEY_ONOFF]:
            p_mode[mc.KEY_STATE] = (
                1 if p_mode[mc.KEY_TARGETTEMP] > p_mode[mc.KEY_CURRENTTEMP] else 0
            )
        else:
            p_mode[mc.KEY_STATE] = 0

    def _update_ModeB(self, p_modeb: "mt_t.ModeB_C"):
        if p_modeb[mc.KEY_ONOFF] == mc.MTS960_ONOFF_ON:
            match p_modeb[mc.KEY_MODE]:
                case mc.MTS960_MODE_HEAT_COOL | mc.MTS960_MODE_SCHEDULE:
                    if p_modeb[mc.KEY_WORKING] == mc.MTS960_WORKING_HEAT:
                        p_modeb[mc.KEY_STATE] = (
                            mc.MTS960_STATE_ON
                            if p_modeb[mc.KEY_TARGETTEMP] > p_modeb[mc.KEY_CURRENTTEMP]
                            else mc.MTS960_STATE_OFF
                        )
                    else:
                        p_modeb[mc.KEY_STATE] = (
                            mc.MTS960_STATE_ON
                            if p_modeb[mc.KEY_TARGETTEMP] < p_modeb[mc.KEY_CURRENTTEMP]
                            else mc.MTS960_STATE_OFF
                        )
                case _:
                    # timer mode not handled
                    p_modeb[mc.KEY_STATE] = mc.MTS960_STATE_OFF
        else:
            p_modeb[mc.KEY_STATE] = mc.MTS960_STATE_OFF

    def _update_ModeC(self, p_modec: "mt_t.ModeC_C"):
        current_temp = p_modec["currentTemp"]
        # It looks like currentTemp is rounded up to 1°C while sensorTemp should be at least 0.5°C resolution.
        p_modec["currentTemp"] = round(current_temp / 100) * 100
        # Assume sensor association is internal sensor for now
        p_modec["sensorTemp"] = round(current_temp / 50) * 50
        p_fan = p_modec["fan"]
        fan_mode = p_fan["fMode"]
        fan_speed = p_fan["speed"]
        # actually we assume (here and in component)
        # (fan_speed != 0) <-> (fMode == MANUAL)
        p_more = p_modec["more"]
        match p_modec[mc.KEY_MODE]:
            case mc.MTS300_MODE_OFF:
                p_more["hStatus"] = 0
                p_more["cStatus"] = 0
                p_more["fStatus"] = 0
                p_more["aStatus"] = 0
                p_more["hdStatus"] = 0
            case mc.MTS300_MODE_HEAT:
                delta_t = round(
                    (p_modec["targetTemp"]["heat"] - current_temp) / self.device_scale
                )
                p_more["hStatus"] = (
                    0 if delta_t <= 0 else 3 if delta_t >= 3 else delta_t
                )
                p_more["cStatus"] = 0
                p_more["fStatus"] = fan_speed or p_more["hStatus"]
            case mc.MTS300_MODE_COOL:
                p_more["hStatus"] = 0
                delta_t = round(
                    (current_temp - p_modec["targetTemp"]["cold"]) / self.device_scale
                )
                p_more["cStatus"] = (
                    0 if delta_t <= 0 else 2 if delta_t >= 2 else delta_t
                )
                p_more["fStatus"] = fan_speed or p_more["cStatus"]
            case mc.MTS300_MODE_AUTO:

                p_targettemp = p_modec["targetTemp"]
                delta_t = round(
                    (p_targettemp["heat"] - current_temp) / self.device_scale
                )
                if delta_t >= 0:
                    p_more["hStatus"] = 3 if delta_t >= 3 else delta_t
                    p_more["cStatus"] = 0
                    p_more["fStatus"] = fan_speed or p_more["hStatus"]
                else:
                    delta_t = round(
                        (current_temp - p_targettemp["cold"]) / self.device_scale
                    )
                    p_more["hStatus"] = 0
                    p_more["cStatus"] = (
                        0 if delta_t <= 0 else 2 if delta_t >= 2 else delta_t
                    )
                    p_more["fStatus"] = fan_speed or p_more["cStatus"]

    def _update_current_temp(self, /):
        p_calibration: "mt_t.Calibration_C" = self.get_namespace_state(
            mn_t.Appliance_Control_Thermostat_Calibration, 0
        )
        _current_temp = round(
            (
                self.CURRENT_TEMPERATURE_MEAN
                + self.CURRENT_TEMPERATURE_DELTA
                * math.sin(self.epoch * 2 * math.pi / self.CURRENT_TEMPERATURE_PERIOD)
            )
            * self.device_scale
        )
        _current_temp = clamp(
            p_calibration[mc.KEY_VALUE] + _current_temp,
            self.temp_min,
            self.temp_max,
        )
        if _current_temp != self.p_mode[mc.KEY_CURRENTTEMP]:
            self.p_mode[mc.KEY_CURRENTTEMP] = _current_temp
            self.update_state_func()
            if self.mqtt_connected:
                self.mqtt_publish_push(
                    self.mode_ns.name, {self.mode_ns.key: [self.p_mode]}
                )
