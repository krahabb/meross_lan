import typing

from ..calendar import MtsSchedule
from ..climate import MtsClimate
from ..merossclient import const as mc, namespaces as mn
from ..number import MtsSetPointNumber, MtsTemperatureNumber

if typing.TYPE_CHECKING:
    from ..binary_sensor import MLBinarySensor
    from .hub import MTS100SubDevice


class Mts100AdjustNumber(MtsTemperatureNumber):

    ns = mn.NAMESPACES[mc.NS_APPLIANCE_HUB_MTS100_ADJUST]
    key_value = mc.KEY_TEMPERATURE

    # HA core entity attributes:
    native_max_value = 5
    native_min_value = -5
    native_step = 0.5

    def __init__(self, climate: "Mts100Climate"):
        self.name = "Adjust temperature"
        super().__init__(
            climate,
            f"config_{self.ns.key}_{self.key_value}",
        )
        # override the default climate.device_scale set in base cls
        self.device_scale = 100


class Mts100Climate(MtsClimate):
    """Climate entity for hub paired devices MTS100, MTS100V3, MTS150"""

    ns = mn.Appliance_Hub_Mts100_Temperature

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
        self.preset_mode = self.MTS_MODE_TO_PRESET_MAP.get(self._mts_mode)
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
        if (
            self.SET_TEMP_FORCE_MANUAL_MODE and self._mts_mode != mc.MTS100_MODE_CUSTOM
        ) or (self._mts_mode == mc.MTS100_MODE_AUTO):
            # setting the temperature automatically switches
            # to manual 'custom' mode. (2024-06-27) This is a change
            # against previous behavior where the mode was retained
            # (unless schedule mode) and the temp setting was directed to any
            # of the presets, whichever was active at the moment.
            # This is following #401 and seems more natural behavior.
            # self.SET_TEMP_FORCE_MANUAL_MODE acts as a config bool
            # to enable this behavior or fallback to the legacy one.
            # Keep in mind we're not also forcing the device to 'ON'.
            # This is intended (right now) to allow the user change
            # the setpoint without implying the device switch on.
            # Turning on/off the device must be an explicit action on HVACMode.
            if await self.manager.async_request_ack(
                mc.NS_APPLIANCE_HUB_MTS100_MODE,
                mc.METHOD_SET,
                {
                    mc.KEY_MODE: [
                        {mc.KEY_ID: self.id, mc.KEY_STATE: mc.MTS100_MODE_CUSTOM}
                    ]
                },
            ):
                self._mts_mode = mc.MTS100_MODE_CUSTOM

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
            self._parse_temperature(response[mc.KEY_PAYLOAD][mc.KEY_TEMPERATURE][0])

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

    def get_ns_adjust(self):
        return self.manager.hub.namespace_handlers[mc.NS_APPLIANCE_HUB_MTS100_ADJUST]

    # message handlers
    def _parse_temperature(self, payload: dict):
        if self._mts_payload == payload:
            return
        self._mts_payload = payload
        if mc.KEY_ROOM in payload:
            self._update_current_temperature(payload[mc.KEY_ROOM])
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
        self.schedule._schedule_entry_count_max = mode
        self.schedule._schedule_entry_count_min = mode


class Mts100SetPointNumber(MtsSetPointNumber):
    """
    customize MtsSetPointNumber to interact with Mts100 family valves
    """

    ns = mn.Appliance_Hub_Mts100_Temperature


class Mts100Schedule(MtsSchedule):
    ns = mn.Appliance_Hub_Mts100_ScheduleB

    def __init__(self, climate: Mts100Climate):
        super().__init__(climate)
        self._schedule_unit_time = climate.manager.hub.descriptor.ability.get(
            mc.NS_APPLIANCE_HUB_MTS100_SCHEDULEB, {}
        ).get(mc.KEY_SCHEDULEUNITTIME, 15)
