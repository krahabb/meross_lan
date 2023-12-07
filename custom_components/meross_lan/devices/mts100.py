from __future__ import annotations

import typing

from ..calendar import MtsSchedule
from ..climate import MtsClimate, MtsSetPointNumber
from ..helpers import reverse_lookup
from ..merossclient import const as mc
from ..number import MLConfigNumber

if typing.TYPE_CHECKING:
    from ..meross_device_hub import MTS100SubDevice


class Mts100AdjustNumber(MLConfigNumber):
    namespace = mc.NS_APPLIANCE_HUB_MTS100_ADJUST
    key_namespace = mc.KEY_ADJUST
    key_channel = mc.KEY_ID
    key_value = mc.KEY_TEMPERATURE

    __slots__ = ("climate",)

    def __init__(self, manager: MTS100SubDevice, climate: Mts100Climate):
        self.climate = climate  # climate not initialized yet
        self._attr_name = "Adjust temperature"
        super().__init__(
            manager,
            manager.id,
            f"config_{self.key_namespace}_{self.key_value}",
            MLConfigNumber.DeviceClass.TEMPERATURE,
        )

    @property
    def native_max_value(self):
        return 5

    @property
    def native_min_value(self):
        return -5

    @property
    def native_step(self):
        return 0.1

    @property
    def native_unit_of_measurement(self):
        return MtsClimate.TEMP_CELSIUS

    async def async_set_native_value(self, value: float):
        # when sending the 'adjust' to the valve, the device also modifies
        # it's temperature setpoints (target temp, away, cool, heat and so)
        # This is due to internal exotic rounding when the adjust offset is not
        # a multiple of 0.5 °C
        # It's unclear how and why this happens so we'll try to circumvent
        # the issue by saving the actual values before the adjust command
        # and then resending the (previous) setpoints
        climate = self.climate
        target_temperature = climate.target_temperature
        comfort_temperature = climate.number_comfort_temperature.native_value
        away_temperature = climate.number_away_temperature.native_value
        sleep_temperature = climate.number_sleep_temperature.native_value

        await super().async_set_native_value(value)

        p_temperature = {mc.KEY_ID: climate.id}
        if target_temperature:
            p_temperature[mc.KEY_CUSTOM] = (
                round(target_temperature * mc.MTS_TEMP_SCALE)
                + climate._mts_adjust_offset
            )
        if comfort_temperature:
            p_temperature[mc.KEY_COMFORT] = (
                round(comfort_temperature * mc.MTS_TEMP_SCALE)
                + climate._mts_adjust_offset
            )
        if away_temperature:
            p_temperature[mc.KEY_AWAY] = (
                round(away_temperature * mc.MTS_TEMP_SCALE) + climate._mts_adjust_offset
            )
        if sleep_temperature:
            p_temperature[mc.KEY_ECONOMY] = (
                round(sleep_temperature * mc.MTS_TEMP_SCALE)
                + climate._mts_adjust_offset
            )

        if response := await self.manager.async_request_ack(
            mc.NS_APPLIANCE_HUB_MTS100_TEMPERATURE,
            mc.METHOD_SET,
            {mc.KEY_TEMPERATURE: [p_temperature]},
        ):
            self.climate._parse_temperature(
                response[mc.KEY_PAYLOAD][mc.KEY_TEMPERATURE][0]
            )

    @property
    def device_scale(self):
        return 100

    def update_native_value(self, device_value):
        super().update_native_value(device_value)
        # hub adjust has a scale of 100 while the other climate temperature
        # numbers have a scale of 10 (MTS_SCALE)
        adjust_offset = round(device_value / 10) % 5
        # since adjust have a resolution of 0.1 °C while temp setpoints have a 0.5 °C
        # stepping, when the adjust is not a multiple of 0.5 the MTS looses the
        # correct setpoints and starts to round down their values.
        # it looks like it is not able to represent correctly the offsets when
        # these are not in multiple of 0.5. We therefore try to 'patch'
        # these readings before sending them to HA
        self.climate._mts_adjust_offset = (
            adjust_offset if adjust_offset < 3 else adjust_offset - 5
        )
        # _mts_adjust_offset will then be used to offset the T setpoints and will be 0 when
        # the adjust value is a 0.5 multiple or the corresponding remainder when it is not.
        # the offset is set so it 'down-rounds' when it is 0.1 or 0.2. Instead it will 'up-rounds'
        # when it is 0.3 or 0.4


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

    def __init__(self, manager: MTS100SubDevice):
        self._attr_extra_state_attributes = {}
        super().__init__(
            manager,
            manager.id,
            manager.build_binary_sensor_window(),
            Mts100AdjustNumber(manager, self),
            Mts100SetPointNumber,
            Mts100Schedule,
        )

    @property
    def scheduleBMode(self):
        return self._attr_extra_state_attributes.get(mc.KEY_SCHEDULEBMODE)

    @scheduleBMode.setter
    def scheduleBMode(self, value):
        if value:
            self._attr_extra_state_attributes[mc.KEY_SCHEDULEBMODE] = value
        else:
            self._attr_extra_state_attributes.pop(mc.KEY_SCHEDULEBMODE)

    async def async_set_hvac_mode(self, hvac_mode: MtsClimate.HVACMode):
        if hvac_mode == MtsClimate.HVACMode.OFF:
            await self.async_request_onoff(0)
        else:
            await self.async_request_onoff(1)

    async def async_set_preset_mode(self, preset_mode: str):
        mode = reverse_lookup(Mts100Climate.MTS_MODE_TO_PRESET_MAP, preset_mode)
        if mode is not None:
            if await self.manager.async_request_ack(
                mc.NS_APPLIANCE_HUB_MTS100_MODE,
                mc.METHOD_SET,
                {mc.KEY_MODE: [{mc.KEY_ID: self.id, mc.KEY_STATE: mode}]},
            ):
                self._mts_mode = mode
                self.update_mts_state()
            if not self._mts_onoff:
                await self.async_request_onoff(1)

    async def async_set_temperature(self, **kwargs):
        device_temperature = (
            round(kwargs[Mts100Climate.ATTR_TEMPERATURE] * mc.MTS_TEMP_SCALE)
            + self._mts_adjust_offset
        )
        key = Mts100Climate.PRESET_TO_TEMPERATUREKEY_MAP[
            self._attr_preset_mode or Mts100Climate.PRESET_CUSTOM
        ]
        # when sending a temp this way the device will automatically
        # exit auto mode if needed
        if response := await self.manager.async_request_ack(
            mc.NS_APPLIANCE_HUB_MTS100_TEMPERATURE,
            mc.METHOD_SET,
            {mc.KEY_TEMPERATURE: [{mc.KEY_ID: self.id, key: device_temperature}]},
        ):
            self._parse_temperature(response[mc.KEY_PAYLOAD][mc.KEY_TEMPERATURE][0])

    async def async_request_onoff(self, onoff: int):
        if await self.manager.async_request_ack(
            mc.NS_APPLIANCE_HUB_TOGGLEX,
            mc.METHOD_SET,
            {mc.KEY_TOGGLEX: [{mc.KEY_ID: self.id, mc.KEY_ONOFF: onoff}]},
        ):
            self._mts_onoff = onoff
            self.update_mts_state()

    def is_mts_scheduled(self):
        return self._mts_onoff and self._mts_mode == mc.MTS100_MODE_AUTO

    def update_mts_state(self):
        self._attr_preset_mode = self.MTS_MODE_TO_PRESET_MAP.get(self._mts_mode)  # type: ignore
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

        super().update_mts_state()

    # message handlers
    def _parse_temperature(self, p_temperature: dict):
        if mc.KEY_ROOM in p_temperature:
            self._attr_current_temperature = (
                p_temperature[mc.KEY_ROOM] / mc.MTS_TEMP_SCALE
            )
            self.select_tracked_sensor.check_tracking()
            self.manager.sensor_temperature.update_state(self._attr_current_temperature)
        if mc.KEY_CURRENTSET in p_temperature:
            self._attr_target_temperature = (
                p_temperature[mc.KEY_CURRENTSET] - self._mts_adjust_offset
            ) / mc.MTS_TEMP_SCALE
        if mc.KEY_MIN in p_temperature:
            self._attr_min_temp = p_temperature[mc.KEY_MIN] / mc.MTS_TEMP_SCALE
        if mc.KEY_MAX in p_temperature:
            self._attr_max_temp = p_temperature[mc.KEY_MAX] / mc.MTS_TEMP_SCALE
        if mc.KEY_HEATING in p_temperature:
            self._mts_active = p_temperature[mc.KEY_HEATING]
        if mc.KEY_COMFORT in p_temperature:
            self.number_comfort_temperature.update_native_value(
                p_temperature[mc.KEY_COMFORT]
            )
        if mc.KEY_ECONOMY in p_temperature:
            self.number_sleep_temperature.update_native_value(
                p_temperature[mc.KEY_ECONOMY]
            )
        if mc.KEY_AWAY in p_temperature:
            self.number_away_temperature.update_native_value(p_temperature[mc.KEY_AWAY])
        if mc.KEY_OPENWINDOW in p_temperature:
            self.binary_sensor_window.update_onoff(p_temperature[mc.KEY_OPENWINDOW])

        self.update_mts_state()


class Mts100SetPointNumber(MtsSetPointNumber):
    """
    customize MtsSetPointNumber to interact with Mts100 family valves
    """

    namespace = mc.NS_APPLIANCE_HUB_MTS100_TEMPERATURE
    key_namespace = mc.KEY_TEMPERATURE
    key_channel = mc.KEY_ID


class Mts100Schedule(MtsSchedule):
    namespace = mc.NS_APPLIANCE_HUB_MTS100_SCHEDULEB
    key_channel = mc.KEY_ID

    def __init__(self, climate: Mts100Climate):
        super().__init__(climate)
        self._schedule_unit_time = climate.manager.hub.descriptor.ability.get(
            mc.NS_APPLIANCE_HUB_MTS100_SCHEDULEB, {}
        ).get(mc.KEY_SCHEDULEUNITTIME, 15)
