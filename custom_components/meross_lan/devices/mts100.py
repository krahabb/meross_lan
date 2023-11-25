from __future__ import annotations

import typing

from ..calendar import MtsSchedule
from ..climate import MtsClimate, MtsSetPointNumber
from ..helpers import reverse_lookup
from ..merossclient import const as mc

if typing.TYPE_CHECKING:
    from ..meross_device_hub import MTS100SubDevice


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
        super().__init__(manager, manager.id, Mts100Schedule(manager, manager.id, self))

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

            def _ack_callback(acknowledge: bool, header: dict, payload: dict):
                if acknowledge:
                    self._mts_mode = mode
                    self.update_mts_state()

            await self.manager.async_request(
                mc.NS_APPLIANCE_HUB_MTS100_MODE,
                mc.METHOD_SET,
                {mc.KEY_MODE: [{mc.KEY_ID: self.id, mc.KEY_STATE: mode}]},
                _ack_callback,
            )

            if not self._mts_onoff:
                await self.async_request_onoff(1)

    async def async_set_temperature(self, **kwargs):
        t = kwargs[Mts100Climate.ATTR_TEMPERATURE]
        key = Mts100Climate.PRESET_TO_TEMPERATUREKEY_MAP[
            self._attr_preset_mode or Mts100Climate.PRESET_CUSTOM
        ]

        def _ack_callback(acknowledge: bool, header: dict, payload: dict):
            if acknowledge:
                self._attr_target_temperature = t
                self.update_mts_state()

        # when sending a temp this way the device will automatically
        # exit auto mode if needed
        await self.manager.async_request(
            mc.NS_APPLIANCE_HUB_MTS100_TEMPERATURE,
            mc.METHOD_SET,
            {
                mc.KEY_TEMPERATURE: [{mc.KEY_ID: self.id, key: int(t * 10)}]
            },  # the device rounds down ?!
            _ack_callback,
        )

    async def async_request_onoff(self, onoff: int):
        def _ack_callback(acknowledge: bool, header: dict, payload: dict):
            if acknowledge:
                self._mts_onoff = onoff
                self.update_mts_state()

        await self.manager.async_request(
            mc.NS_APPLIANCE_HUB_TOGGLEX,
            mc.METHOD_SET,
            {mc.KEY_TOGGLEX: [{mc.KEY_ID: self.id, mc.KEY_ONOFF: onoff}]},
            _ack_callback,
        )

    def is_mts_scheduled(self):
        return self._mts_onoff and self._mts_mode == mc.MTS100_MODE_AUTO

    def update_mts_state(self):
        self._attr_preset_mode = self.MTS_MODE_TO_PRESET_MAP.get(self._mts_mode)  # type: ignore
        if self._mts_onoff:
            self._attr_hvac_mode = MtsClimate.HVACMode.HEAT
            self._attr_hvac_action = (
                MtsClimate.HVACAction.HEATING if self._mts_active else MtsClimate.HVACAction.IDLE
            )
        else:
            self._attr_hvac_mode = MtsClimate.HVACMode.OFF
            self._attr_hvac_action = MtsClimate.HVACAction.OFF

        super().update_mts_state()


class Mts100SetPointNumber(MtsSetPointNumber):
    """
    customize MtsSetPointNumber to interact with Mts100 family valves
    """

    namespace = mc.NS_APPLIANCE_HUB_MTS100_TEMPERATURE
    key_namespace = mc.KEY_TEMPERATURE
    key_channel = mc.KEY_ID


class Mts100Schedule(MtsSchedule):
    def __init__(self, manager: MTS100SubDevice, channel, climate: Mts100Climate):
        super().__init__(
            manager, channel, climate, mc.NS_APPLIANCE_HUB_MTS100_SCHEDULEB, mc.KEY_ID
        )
        self._schedule_unit_time = manager.hub.descriptor.ability.get(
            mc.NS_APPLIANCE_HUB_MTS100_SCHEDULEB, {}
        ).get(mc.KEY_SCHEDULEUNITTIME, 15)
