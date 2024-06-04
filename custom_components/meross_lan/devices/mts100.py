import typing

from ..calendar import MtsSchedule
from ..climate import MtsClimate
from ..merossclient import const as mc
from ..number import MtsSetPointNumber, MtsTemperatureNumber

if typing.TYPE_CHECKING:
    from ..binary_sensor import MLBinarySensor
    from .hub import MTS100SubDevice


class Mts100AdjustNumber(MtsTemperatureNumber):

    namespace = mc.NS_APPLIANCE_HUB_MTS100_ADJUST
    key_namespace = mc.KEY_ADJUST
    key_channel = mc.KEY_ID
    key_value = mc.KEY_TEMPERATURE

    # HA core entity attributes:
    native_max_value = 5
    native_min_value = -5
    native_step = 0.5

    def __init__(self, climate: "Mts100Climate"):
        self.name = "Adjust temperature"
        super().__init__(
            climate,
            f"config_{self.key_namespace}_{self.key_value}",
        )
        # override the default climate.device_scale set in base cls
        self.device_scale = 100


class Mts100Climate(MtsClimate):
    """Climate entity for hub paired devices MTS100, MTS100V3, MTS150"""

    namespace = mc.NS_APPLIANCE_HUB_MTS100_TEMPERATURE
    key_namespace = mc.KEY_TEMPERATURE

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
    MTS_MODE_TO_TEMPERATUREKEY_MAP = mc.MTS100_MODE_TO_CURRENTSET_MAP

    manager: "MTS100SubDevice"

    __slots__ = ("binary_sensor_window",)

    def __init__(self, manager: "MTS100SubDevice"):
        self.extra_state_attributes = {}
        super().__init__(
            manager,
            manager.id,
            Mts100AdjustNumber,
            Mts100SetPointNumber,
            Mts100Schedule,
        )
        self.binary_sensor_window = manager.build_binary_sensor_window()

    # interface: MtsClimate
    async def async_shutdown(self):
        await super().async_shutdown()
        self.binary_sensor_window: "MLBinarySensor" = None  # type: ignore

    def flush_state(self):
        if self._mts_onoff:
            self.hvac_mode = MtsClimate.HVACMode.HEAT
            self.hvac_action = (
                MtsClimate.HVACAction.HEATING
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
        await self.async_request_onoff(1)

    async def async_set_temperature(self, **kwargs):
        if self._mts_mode == mc.MTS100_MODE_AUTO:
            # when sending a temp this way the device should automatically
            # exit auto mode if needed. We're anyway forcing going
            # to manual mode when the device is set to schedule
            await self.async_request_mode(mc.MTS100_MODE_CUSTOM)
        key = mc.MTS100_MODE_TO_CURRENTSET_MAP.get(self._mts_mode) or mc.KEY_CUSTOM
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
            self._parse(response[mc.KEY_PAYLOAD][mc.KEY_TEMPERATURE][0])

    async def async_request_mode(self, mode: int):
        """Requests an mts mode and (ensure) turn-on"""
        if await self.manager.async_request_ack(
            mc.NS_APPLIANCE_HUB_MTS100_MODE,
            mc.METHOD_SET,
            {mc.KEY_MODE: [{mc.KEY_ID: self.id, mc.KEY_STATE: mode}]},
        ):
            self._mts_mode = mode
            if not self._mts_onoff:
                if await self.manager.async_request_ack(
                    mc.NS_APPLIANCE_HUB_TOGGLEX,
                    mc.METHOD_SET,
                    {mc.KEY_TOGGLEX: [{mc.KEY_ID: self.id, mc.KEY_ONOFF: 1}]},
                ):
                    self._mts_onoff = 1
            key_temp = mc.MTS100_MODE_TO_CURRENTSET_MAP.get(mode)
            if key_temp in self._mts_payload:
                target_temperature = self._mts_payload[key_temp]
                self._mts_payload[mc.KEY_CURRENTSET] = target_temperature
                self.target_temperature = target_temperature / self.device_scale
            self.flush_state()

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

    # message handlers
    def _parse(self, payload: dict):
        if self._mts_payload == payload:
            return
        self._mts_payload = payload
        if mc.KEY_ROOM in payload:
            mts100 = self.manager
            self.current_temperature = payload[mc.KEY_ROOM] / self.device_scale
            self.select_tracked_sensor.check_tracking()
            if mts100.sensor_temperature.update_native_value(self.current_temperature):
                strategy = mts100.hub.namespace_handlers[
                    mc.NS_APPLIANCE_HUB_MTS100_ADJUST
                ]
                if strategy.lastrequest < (mts100.hub.lastresponse - 30):
                    strategy.lastrequest = 0

        if mc.KEY_CURRENTSET in payload:
            self.target_temperature = payload[mc.KEY_CURRENTSET] / self.device_scale
        if mc.KEY_MIN in payload:
            self.min_temp = payload[mc.KEY_MIN] / self.device_scale
        if mc.KEY_MAX in payload:
            self.max_temp = payload[mc.KEY_MAX] / self.device_scale
        if mc.KEY_HEATING in payload:
            self._mts_active = payload[mc.KEY_HEATING]
        if mc.KEY_OPENWINDOW in payload:
            self.binary_sensor_window.update_onoff(payload[mc.KEY_OPENWINDOW])

        for (
            key_temp,
            number_preset_temperature,
        ) in self.number_preset_temperature.items():
            if key_temp in payload:
                number_preset_temperature.update_device_value(payload[key_temp])

        self.flush_state()

    # interface: self
    def update_scheduleb_mode(self, mode):
        self.extra_state_attributes[mc.KEY_SCHEDULEBMODE] = mode
        self.schedule._schedule_entry_count = mode


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
