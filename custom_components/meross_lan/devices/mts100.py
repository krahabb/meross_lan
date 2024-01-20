from __future__ import annotations

import typing

from ..calendar import MtsSchedule
from ..climate import MtsClimate
from ..helpers import reverse_lookup
from ..merossclient import const as mc
from ..number import MtsSetPointNumber, MtsTemperatureNumber

if typing.TYPE_CHECKING:
    from ..binary_sensor import MLBinarySensor
    from ..meross_device_hub import MTS100SubDevice


class Mts100AdjustNumber(MtsTemperatureNumber):
    _attr_name = "Adjust temperature"

    namespace = mc.NS_APPLIANCE_HUB_MTS100_ADJUST
    key_namespace = mc.KEY_ADJUST
    key_channel = mc.KEY_ID
    key_value = mc.KEY_TEMPERATURE

    def __init__(self, climate: Mts100Climate):
        super().__init__(
            climate,
            f"config_{self.key_namespace}_{self.key_value}",
        )

    @property
    def native_max_value(self):
        return 5

    @property
    def native_min_value(self):
        return -5

    @property
    def native_step(self):
        return 0.5

    @property
    def device_scale(self):
        return 100


class Mts100Climate(MtsClimate):
    """Climate entity for hub paired devices MTS100, MTS100V3, MTS150"""

    MTS_MODE_TO_PRESET_MAP = {
        mc.MTS100_MODE_CUSTOM: MtsClimate.PRESET_CUSTOM,
        mc.MTS100_MODE_HEAT: MtsClimate.PRESET_COMFORT,
        mc.MTS100_MODE_COOL: MtsClimate.PRESET_SLEEP,
        mc.MTS100_MODE_ECO: MtsClimate.PRESET_AWAY,
        mc.MTS100_MODE_AUTO: MtsClimate.PRESET_AUTO,
    }
    # when setting target temp we'll set an appropriate payload key
    # for the mts100 depending on current 'preset' mode.
    # if mts100 is in any of 'off', 'auto' we just set the 'custom'
    # target temp but of course the valve will not follow
    # this temp since it's mode is not set to follow a manual set
    PRESET_TO_TEMPERATUREKEY_MAP = {
        MtsClimate.PRESET_CUSTOM: mc.KEY_CUSTOM,
        MtsClimate.PRESET_COMFORT: mc.KEY_COMFORT,
        MtsClimate.PRESET_SLEEP: mc.KEY_ECONOMY,
        MtsClimate.PRESET_AWAY: mc.KEY_AWAY,
        MtsClimate.PRESET_AUTO: mc.KEY_CUSTOM,
    }

    manager: MTS100SubDevice

    __slots__ = ("binary_sensor_window",)

    def __init__(self, manager: MTS100SubDevice):
        self._attr_extra_state_attributes = {}
        super().__init__(
            manager,
            manager.id,
            Mts100AdjustNumber,
            Mts100SetPointNumber,
            Mts100Schedule,
        )
        self.binary_sensor_window = manager.build_binary_sensor_window()

    @property
    def scheduleBMode(self):
        return self._attr_extra_state_attributes.get(mc.KEY_SCHEDULEBMODE)

    @scheduleBMode.setter
    def scheduleBMode(self, value):
        if value:
            self._attr_extra_state_attributes[mc.KEY_SCHEDULEBMODE] = value
        else:
            self._attr_extra_state_attributes.pop(mc.KEY_SCHEDULEBMODE)

    # interface: MtsClimate
    async def async_shutdown(self):
        await super().async_shutdown()
        self.binary_sensor_window: MLBinarySensor = None  # type: ignore

    def flush_state(self):
        if self._mts_onoff:
            self._attr_hvac_mode = MtsClimate.HVACMode.HEAT
            self._attr_hvac_action = (
                MtsClimate.HVACAction.HEATING
                if self._mts_active
                else MtsClimate.HVACAction.IDLE
            )
        else:
            self._attr_hvac_mode = MtsClimate.HVACMode.OFF
            self._attr_hvac_action = MtsClimate.HVACAction.OFF
        super().flush_state()

    async def async_set_preset_mode(self, preset_mode: str):
        mode = reverse_lookup(self.MTS_MODE_TO_PRESET_MAP, preset_mode)
        if mode is not None:
            if await self.manager.async_request_ack(
                mc.NS_APPLIANCE_HUB_MTS100_MODE,
                mc.METHOD_SET,
                {mc.KEY_MODE: [{mc.KEY_ID: self.id, mc.KEY_STATE: mode}]},
            ):
                self._mts_mode = mode
                self.flush_state()
            if not self._mts_onoff:
                await self.async_request_onoff(1)

    async def async_set_temperature(self, **kwargs):
        # since the device only accepts values multiple of 5
        # and offsets them by the current temp adjust
        # we'll add 4 so it will eventually round down to the correct
        # internal setpoint
        key = self.PRESET_TO_TEMPERATUREKEY_MAP[
            self._attr_preset_mode or self.PRESET_CUSTOM
        ]
        # when sending a temp this way the device will automatically
        # exit auto mode if needed. Also it will round-down the value
        # to the nearest multiple of 5
        if response := await self.manager.async_request_ack(
            mc.NS_APPLIANCE_HUB_MTS100_TEMPERATURE,
            mc.METHOD_SET,
            {
                mc.KEY_TEMPERATURE: [
                    {
                        mc.KEY_ID: self.id,
                        key: round(
                            kwargs[Mts100Climate.ATTR_TEMPERATURE] * self.device_scale
                        ),
                    }
                ]
            },
        ):
            self._parse_temperature(response[mc.KEY_PAYLOAD][mc.KEY_TEMPERATURE][0])

    async def async_request_onoff(self, onoff: int):
        if await self.manager.async_request_ack(
            mc.NS_APPLIANCE_HUB_TOGGLEX,
            mc.METHOD_SET,
            {mc.KEY_TOGGLEX: [{mc.KEY_ID: self.id, mc.KEY_ONOFF: onoff}]},
        ):
            self._mts_onoff = onoff
            self.flush_state()

    def is_mts_scheduled(self):
        return self._mts_onoff and self._mts_mode == mc.MTS100_MODE_AUTO

    @property
    def namespace(self):
        return mc.NS_APPLIANCE_HUB_MTS100_TEMPERATURE

    @property
    def key_namespace(self):
        mc.KEY_TEMPERATURE

    # message handlers
    def _parse_temperature(self, p_temperature: dict):
        if mc.KEY_ROOM in p_temperature:
            self._attr_current_temperature = (
                p_temperature[mc.KEY_ROOM] / self.device_scale
            )
            self.select_tracked_sensor.check_tracking()
            self.manager.sensor_temperature.update_state(self._attr_current_temperature)

        p_temperature_patch = {}
        _mts_adjusted_temperature = self._mts_adjusted_temperature
        # patch the mts rounding: the mts has a 'default' resolution of 0.5
        # (5 points in device units). If we set a temp adjust with sub-resolution
        # (like 0.1 °C for example) the device accepts that and starts offsetting the room
        # temperature but also starts to 'mess' its setpoints since it clearly
        # (or buggly) cannot manage these sub-resolutions (1-2-3-4 device points)
        # This code was an attempt to patch this rounding issue but even if somewhat working
        # it is not reliable (likely because updates are 'so asynchronous' that we always risk
        # loosing the setpoint track/patch algorithm since it works a bit like differential
        # encoders readers used in elctro-mechanics)
        # To totally overcome the issue, we've now 'fixed' also the resolution of temp adjust
        # to 0.5 °C and this appears to work consistently. The code is left (should no harm)
        # for future reference or tries
        for key in (
            mc.KEY_CURRENTSET,
            mc.KEY_CUSTOM,
            mc.KEY_COMFORT,
            mc.KEY_ECONOMY,
            mc.KEY_AWAY,
        ):
            if key not in p_temperature:
                continue
            _t = p_temperature[key]
            adjust = _t % 5
            if adjust:
                _t = _t - adjust
            if key in _mts_adjusted_temperature:
                _t_current = _mts_adjusted_temperature[key]
                if _t == _t_current:
                    # no change in our entity state
                    continue
                elif adjust and _t + 5 == _t_current:
                    # a change in mts adjust temperature rounded down a bit our setpoint
                    # so we 'fix' the mts
                    p_temperature_patch[key] = _t_current + adjust
                    continue
            _mts_adjusted_temperature[key] = _t

            if key is mc.KEY_CURRENTSET:
                self._attr_target_temperature = _t / self.device_scale
            elif key is mc.KEY_COMFORT:
                self.number_comfort_temperature.update_native_value(_t)
            elif key is mc.KEY_ECONOMY:
                self.number_sleep_temperature.update_native_value(_t)
            elif key is mc.KEY_AWAY:
                self.number_away_temperature.update_native_value(_t)

        if p_temperature_patch:
            p_temperature_patch[mc.KEY_ID] = self.id
            # TODO: this request should just fix the mts100 to the values expected
            # in HA but we're not sure and we should check the response and
            # see if it fits. We should use the await version and process the SET_ACK
            # payload since it carries the mts state but our code, as a general rule,
            # discards every SET_ACK. Here (manager.request) we still have the calback
            # for this but it's going to be removed in the next major release
            # The mts state will anyway be eventually pushed or we'll poll it very soon
            self.manager.request(
                (
                    mc.NS_APPLIANCE_HUB_MTS100_TEMPERATURE,
                    mc.METHOD_SET,
                    {mc.KEY_TEMPERATURE: [p_temperature_patch]},
                )
            )

        if mc.KEY_MIN in p_temperature:
            self._attr_min_temp = p_temperature[mc.KEY_MIN] / self.device_scale
        if mc.KEY_MAX in p_temperature:
            self._attr_max_temp = p_temperature[mc.KEY_MAX] / self.device_scale
        if mc.KEY_HEATING in p_temperature:
            self._mts_active = p_temperature[mc.KEY_HEATING]
        if mc.KEY_OPENWINDOW in p_temperature:
            self.binary_sensor_window.update_onoff(p_temperature[mc.KEY_OPENWINDOW])

        self.flush_state()


class Mts100SetPointNumber(MtsSetPointNumber):
    """
    customize MtsSetPointNumber to interact with Mts100 family valves
    """

    namespace = mc.NS_APPLIANCE_HUB_MTS100_TEMPERATURE
    key_namespace = mc.KEY_TEMPERATURE
    key_channel = mc.KEY_ID


class Mts100Schedule(MtsSchedule):
    namespace = mc.NS_APPLIANCE_HUB_MTS100_SCHEDULEB
    key_namespace = mc.KEY_SCHEDULE
    key_channel = mc.KEY_ID

    def __init__(self, climate: Mts100Climate):
        super().__init__(climate)
        self._schedule_unit_time = climate.manager.hub.descriptor.ability.get(
            mc.NS_APPLIANCE_HUB_MTS100_SCHEDULEB, {}
        ).get(mc.KEY_SCHEDULEUNITTIME, 15)
