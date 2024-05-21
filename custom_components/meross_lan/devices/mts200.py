import typing

from ..calendar import MtsSchedule
from ..climate import MtsClimate
from ..merossclient import const as mc
from ..number import MtsSetPointNumber

if typing.TYPE_CHECKING:
    from ..meross_device import MerossDevice
    from ..number import MtsTemperatureNumber


class Mts200SetPointNumber(MtsSetPointNumber):
    """
    customize MtsSetPointNumber to interact with Mts200 family valves
    """

    namespace = mc.NS_APPLIANCE_CONTROL_THERMOSTAT_MODE
    key_namespace = mc.KEY_MODE


class Mts200Climate(MtsClimate):
    """Climate entity for MTS200 devices"""

    manager: "MerossDevice"
    namespace = mc.NS_APPLIANCE_CONTROL_THERMOSTAT_MODE
    key_namespace = mc.KEY_MODE

    MTS_MODE_TO_PRESET_MAP = {
        mc.MTS200_MODE_MANUAL: MtsClimate.PRESET_CUSTOM,
        mc.MTS200_MODE_HEAT: MtsClimate.PRESET_COMFORT,
        mc.MTS200_MODE_COOL: MtsClimate.PRESET_SLEEP,
        mc.MTS200_MODE_ECO: MtsClimate.PRESET_AWAY,
        mc.MTS200_MODE_AUTO: MtsClimate.PRESET_AUTO,
    }
    # right now we're only sure summermode == '1' is 'HEAT'
    MTS_SUMMERMODE_TO_HVAC_MODE = {
        None: MtsClimate.HVACMode.HEAT,  # mapping when no summerMode avail
        mc.MTS200_SUMMERMODE_COOL: MtsClimate.HVACMode.COOL,
        mc.MTS200_SUMMERMODE_HEAT: MtsClimate.HVACMode.HEAT,
    }
    HVAC_MODE_TO_MTS_SUMMERMODE = {
        MtsClimate.HVACMode.HEAT: mc.MTS200_SUMMERMODE_HEAT,
        MtsClimate.HVACMode.COOL: mc.MTS200_SUMMERMODE_COOL,
    }
    MTS_SUMMERMODE_TO_HVAC_ACTION: dict[int | None, MtsClimate.HVACAction] = {
        None: MtsClimate.HVACAction.HEATING,  # mapping when no summerMode avail
        mc.MTS200_SUMMERMODE_COOL: MtsClimate.HVACAction.COOLING,
        mc.MTS200_SUMMERMODE_HEAT: MtsClimate.HVACAction.HEATING,
    }
    MTS_MODE_TO_TEMPERATUREKEY_MAP = mc.MTS200_MODE_TO_TARGETTEMP_MAP

    __slots__ = ("_mts_summermode",)

    def __init__(
        self,
        manager: "MerossDevice",
        channel: object,
        adjust_number_class: typing.Type["MtsTemperatureNumber"],
    ):
        super().__init__(
            manager,
            channel,
            adjust_number_class,
            Mts200SetPointNumber,
            Mts200Schedule,
        )
        self._mts_summermode = None
        if mc.NS_APPLIANCE_CONTROL_THERMOSTAT_SUMMERMODE in manager.descriptor.ability:
            self.hvac_modes = [
                MtsClimate.HVACMode.OFF,
                MtsClimate.HVACMode.HEAT,
                MtsClimate.HVACMode.COOL,
            ]

    # interface: MtsClimate
    def flush_state(self):
        if self._mts_onoff:
            self.hvac_mode = self.MTS_SUMMERMODE_TO_HVAC_MODE.get(self._mts_summermode)
            self.hvac_action = (
                self.MTS_SUMMERMODE_TO_HVAC_ACTION.get(self._mts_summermode)
                if self._mts_active
                else MtsClimate.HVACAction.IDLE
            )
        else:
            self.hvac_mode = MtsClimate.HVACMode.OFF
            self.hvac_action = MtsClimate.HVACAction.OFF

        super().flush_state()

    async def async_set_hvac_mode(self, hvac_mode: MtsClimate.HVACMode):
        if hvac_mode == MtsClimate.HVACMode.OFF:
            await self.async_request_onoff(0)
            return

        if not (self._mts_summermode is None):
            # this is an indicator the device supports it
            summermode = self.HVAC_MODE_TO_MTS_SUMMERMODE[hvac_mode]
            if self._mts_summermode != summermode:
                await self.async_request_summermode(summermode)

        await self.async_request_onoff(1)

    async def async_set_temperature(self, **kwargs):
        key = mc.MTS200_MODE_TO_TARGETTEMP_MAP.get(self._mts_mode) or mc.KEY_MANUALTEMP
        await self._async_request_mode(
            {
                mc.KEY_CHANNEL: self.channel,
                mc.KEY_MODE: (
                    mc.MTS200_MODE_MANUAL
                    if key is mc.KEY_MANUALTEMP
                    else self._mts_mode
                ),
                mc.KEY_ONOFF: 1,
                key: round(kwargs[self.ATTR_TEMPERATURE] * self.device_scale),
            }
        )

    async def async_request_mode(self, mode: int):
        await self._async_request_mode(
            {
                mc.KEY_CHANNEL: self.channel,
                mc.KEY_MODE: mode,
                mc.KEY_ONOFF: 1,
            }
        )

    async def async_request_onoff(self, onoff: int):
        await self._async_request_mode(
            {mc.KEY_CHANNEL: self.channel, mc.KEY_ONOFF: onoff}
        )

    def is_mts_scheduled(self):
        return self._mts_onoff and self._mts_mode == mc.MTS200_MODE_AUTO

    # interface: self
    async def async_request_summermode(self, summermode: int):
        if await self.manager.async_request_ack(
            mc.NS_APPLIANCE_CONTROL_THERMOSTAT_SUMMERMODE,
            mc.METHOD_SET,
            {
                mc.KEY_SUMMERMODE: [
                    {mc.KEY_CHANNEL: self.channel, mc.KEY_MODE: summermode}
                ]
            },
        ):
            # it looks that (at least when sending '0') even
            # if acknowledged the mts doesnt really update it
            self._mts_summermode = summermode
            self.flush_state()

    async def _async_request_mode(self, p_mode: dict):
        if response := await self.manager.async_request_ack(
            mc.NS_APPLIANCE_CONTROL_THERMOSTAT_MODE,
            mc.METHOD_SET,
            {mc.KEY_MODE: [p_mode]},
        ):
            try:
                payload = response[mc.KEY_PAYLOAD][mc.KEY_MODE]
                self._parse(payload[0] if isinstance(payload, list) else payload)
            except KeyError:
                # optimistic update
                payload = self._mts_payload | p_mode
                if mc.KEY_MODE in p_mode:
                    key_temp = mc.MTS200_MODE_TO_TARGETTEMP_MAP.get(p_mode[mc.KEY_MODE])
                    if key_temp in payload:
                        payload[mc.KEY_TARGETTEMP] = payload[key_temp]
                self._parse(payload)

    # message handlers
    def _parse(self, payload: dict):
        """{
            "channel": 0,
            "onoff": 1,
            "mode": 3,
            "state": 0,
            "currentTemp": 210,
            "heatTemp": 240,
            "coolTemp": 210,
            "ecoTemp": 120,
            "manualTemp": 230,
            "warning": 0,
            "targetTemp": 205,
            "min": 50,
            "max": 350,
            "lmTime": 1642425303
        }"""
        if self._mts_payload == payload:
            return
        self._mts_payload = payload
        if mc.KEY_MODE in payload:
            self._mts_mode = payload[mc.KEY_MODE]
        if mc.KEY_ONOFF in payload:
            self._mts_onoff = payload[mc.KEY_ONOFF]
        if mc.KEY_STATE in payload:
            self._mts_active = payload[mc.KEY_STATE]
        if mc.KEY_CURRENTTEMP in payload:
            self.current_temperature = payload[mc.KEY_CURRENTTEMP] / self.device_scale
            self.select_tracked_sensor.check_tracking()
        if mc.KEY_TARGETTEMP in payload:
            self.target_temperature = payload[mc.KEY_TARGETTEMP] / self.device_scale
        if mc.KEY_MIN in payload:
            self.min_temp = payload[mc.KEY_MIN] / self.device_scale
        if mc.KEY_MAX in payload:
            self.max_temp = payload[mc.KEY_MAX] / self.device_scale

        for (
            key_temp,
            number_preset_temperature,
        ) in self.number_preset_temperature.items():
            if key_temp in payload:
                number_preset_temperature.update_device_value(payload[key_temp])

        self.flush_state()

    def _parse_holdAction(self, payload: dict):
        """{"channel": 0, "mode": 0, "expire": 1697010767}"""
        # TODO: it looks like this message is related to #369.
        # The trace shows the log about the missing handler in 4.5.2
        # and it looks like when we receive this, it is a notification
        # the mts is not really changing its setpoint (as per the issue).
        # We need more info about how to process this. This handler however
        # will be fully implemented in next major (5.x) since the new Mts200
        # architecture is too different from current version one and
        # it would be a mess to merge branches afterway

    def _parse_summerMode(self, payload: dict):
        """{ "channel": 0, "mode": 0 }"""
        if mc.KEY_MODE in payload:
            summermode = payload[mc.KEY_MODE]
            if self._mts_summermode != summermode:
                self._mts_summermode = summermode
                self.flush_state()


class Mts200Schedule(MtsSchedule):
    namespace = mc.NS_APPLIANCE_CONTROL_THERMOSTAT_SCHEDULE
    key_namespace = mc.KEY_SCHEDULE
    key_channel = mc.KEY_CHANNEL

    def __init__(self, climate: Mts200Climate):
        super().__init__(climate)
