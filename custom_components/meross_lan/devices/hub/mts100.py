from typing import TYPE_CHECKING, override

from . import SubDeviceEntity
from ...binary_sensor import BinarySensor
from ...climate import MtsClimate
from ...merossclient.protocol import const as mc
from ...merossclient.protocol.namespaces import hub as mn_h
from ...switch import EmulatedSwitch

if TYPE_CHECKING:
    from typing import Unpack

    from . import SubDevice, mt_h


class Mts100Climate(SubDeviceEntity, MtsClimate):
    """Climate entity for hub paired devices MTS100, MTS100V3, MTS150"""

    class AdjustNumber(MtsClimate.AdjustNumber):

        ns = mn_h.Appliance_Hub_Mts100_Adjust
        key_value = mc.KEY_TEMPERATURE
        ENTITY_KEY = f"config_{ns.key}_{key_value}"
        _attr_device_scale = 100
        _attr_native_max_value = 5
        _attr_native_min_value = -5
        _attr_native_step = 0.5

    class SetPointNumber(MtsClimate.SetPointNumber):
        """
        customize SetPointNumber to interact with Mts100 family valves
        """

        ns = mn_h.Appliance_Hub_Mts100_Temperature

    class Schedule(MtsClimate.Schedule):
        ns = mn_h.Appliance_Hub_Mts100_ScheduleB

        def __init__(self, climate: "Mts100Climate", /):
            MtsClimate.Schedule.__init__(self, climate)
            self._schedule_unit_time = climate.parent.parent.descriptor.ability.get(
                mn_h.Appliance_Hub_Mts100_ScheduleB, {}
            ).get(mc.KEY_SCHEDULEUNITTIME, 15)

    if TYPE_CHECKING:
        ns_payload: mt_h._Mts100_Temperature
        binary_sensor_window: BinarySensor
        switch_patch_hvacaction: EmulatedSwitch

    NS_HUB = (
        mn_h.Appliance_Hub_Mts100_All,
        mn_h.Appliance_Hub_Mts100_Mode,
        mn_h.Appliance_Hub_ToggleX,
        *SubDeviceEntity.NS_HUB,
    )
    ns = mn_h.Appliance_Hub_Mts100_Temperature

    # MtsClimate class attributes
    device_scale = mc.MTS100_TEMP_SCALE

    MTS_MODE_TO_PRESET_MAP = {
        mc.MTS100_MODE_CUSTOM: MtsClimate.Preset.CUSTOM,
        mc.MTS100_MODE_HEAT: MtsClimate.Preset.COMFORT,
        mc.MTS100_MODE_COOL: MtsClimate.Preset.SLEEP,
        mc.MTS100_MODE_ECO: MtsClimate.Preset.AWAY,
        mc.MTS100_MODE_AUTO: MtsClimate.Preset.AUTO,
    }
    # when setting target temp we'll set an appropriate payload key
    # for the mts100 depending on current 'preset' mode.
    # if mts100 is in any of 'off', 'auto' we just set the 'custom'
    # target temp but of course the valve will not follow
    # this temp since it's mode is not set to follow a manual set
    MTS_MODE_TO_TEMPERATUREKEY_MAP = mc.MTS100_MODE_TO_CURRENTSET_MAP

    # HA core entity attributes:
    _unrecorded_attributes = frozenset(
        {
            mc.KEY_SCHEDULEBMODE,
            *MtsClimate._unrecorded_attributes,
        }
    )

    __slots__ = (
        "binary_sensor_window",
        "switch_patch_hvacaction",
    )

    def __init__(self, subid: str, subdevice: "SubDevice", /):
        self.extra_state_attributes = {}
        match subdevice.key_digest:
            case mc.TYPE_MTS100 | mc.TYPE_MTS100V3:
                self._parse = self._parse_mts100
            case mc.TYPE_MTS150 | mc.TYPE_MTS150P:
                # mts150p subdevs should still report their key_digest as mts150
                # but we handle that option as possible though
                self._parse = self._parse_mts150

        super().__init__(subid, subdevice)
        self.binary_sensor_window = BinarySensor(
            subid,
            subdevice,
            entity_key=str(BinarySensor.DeviceClass.WINDOW),
            device_class=BinarySensor.DeviceClass.WINDOW,
        )
        self.switch_patch_hvacaction = EmulatedSwitch(
            subid,
            subdevice,
            entity_key="patch_hvacaction",
            is_on=False,
        )
        self.switch_patch_hvacaction.register_state_callback(self.flush_state)

    def shutdown(self):
        super().shutdown()
        del self.binary_sensor_window
        del self.switch_patch_hvacaction
        del self._parse

    # interface: MtsClimate
    @override
    def flush_state(self, /):
        self.preset_mode = self.MTS_MODE_TO_PRESET_MAP.get(self._mts_mode)
        if self._mts_onoff:
            self.hvac_mode = MtsClimate.HVACMode.HEAT
            if self.switch_patch_hvacaction.is_on:
                # locally compute the state of the valve ignoring what's being
                # reported in self._mts_active (see #331)
                self.hvac_action = (
                    MtsClimate.HVACAction.HEATING
                    if (
                        (self.target_temperature or 0) > (self.current_temperature or 0)
                    )
                    else MtsClimate.HVACAction.IDLE
                )
            else:
                self.hvac_action = (
                    MtsClimate.HVACAction.HEATING
                    if self._mts_active
                    else MtsClimate.HVACAction.IDLE
                )
        else:
            self.hvac_mode = MtsClimate.HVACMode.OFF
            self.hvac_action = MtsClimate.HVACAction.OFF
        MtsClimate.flush_state(self)

    async def async_set_hvac_mode(self, hvac_mode: MtsClimate.HVACMode, /):
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
            await self.parent.async_request(
                *mn_h.Appliance_Hub_Mts100_Mode.request_set(
                    {mc.KEY_STATE: mc.MTS100_MODE_CUSTOM}, self.id
                )
            )
            self._mts_mode = mc.MTS100_MODE_CUSTOM

        key = mc.MTS100_MODE_TO_CURRENTSET_MAP.get(self._mts_mode) or mc.KEY_CUSTOM
        await self.async_request_parse_ex(
            {key: round(kwargs[Mts100Climate.ATTR_TEMPERATURE] * self.device_scale)}
        )

    @override
    async def async_request_preset(self, mode: int, /):
        """Requests an mts mode and (ensure) turn-on"""
        await self.parent.async_request(
            *mn_h.Appliance_Hub_Mts100_Mode.request_set({mc.KEY_STATE: mode}, self.id)
        )
        self._mts_mode = mode
        if not self._mts_onoff:
            await self.parent.async_request(
                *mn_h.Appliance_Hub_ToggleX.request_set({mc.KEY_ONOFF: 1}, self.id)
            )
            self._mts_onoff = 1
        try:
            target_temperature = self.ns_payload[mc.MTS100_MODE_TO_CURRENTSET_MAP[mode]]
            self.ns_payload[mc.KEY_CURRENTSET] = target_temperature
            self.target_temperature = target_temperature / self.device_scale
        except KeyError:
            pass
        self.flush_state()

    @override
    async def async_request_onoff(self, onoff: int, /):
        await self.parent.async_request(
            *mn_h.Appliance_Hub_ToggleX.request_set({mc.KEY_ONOFF: onoff}, self.id)
        )
        self._mts_onoff = onoff
        self.flush_state()

    @override
    def is_mts_scheduled(self, /):
        return self._mts_onoff and self._mts_mode == mc.MTS100_MODE_AUTO

    # interface: SubDeviceEntity
    def _parse_all(self, payload: "mt_h.Mts100_All", /):
        self.parent._parse_online(payload[mc.KEY_ONLINE])
        if not self.available:
            return
        if mc.KEY_SCHEDULEBMODE in payload:
            self.update_scheduleb_mode(payload[mc.KEY_SCHEDULEBMODE])
        if p_mode := payload.get(mc.KEY_MODE):
            self._mts_mode = p_mode[mc.KEY_STATE]
        if p_togglex := payload.get(mc.KEY_TOGGLEX):
            self._mts_onoff = p_togglex[mc.KEY_ONOFF]
        if p_temperature := payload.get(mc.KEY_TEMPERATURE):
            self._parse_temperature(p_temperature)
        else:
            self.flush_state()

    # interface: self
    def _parse_togglex(self, payload: "mt_h.ToggleX", /):
        self._mts_onoff = payload[mc.KEY_ONOFF]
        self.flush_state()

    def _parse_mode(self, payload: "mt_h._Mts100_Mode", /):
        self._mts_mode = payload[mc.KEY_STATE]
        self.flush_state()

    def _parse_temperature(self, payload: "mt_h._Mts100_Temperature", /):
        if self.ns_payload == payload:
            return
        self.ns_payload = payload
        if mc.KEY_ROOM in payload:
            self._update_current_temperature(payload[mc.KEY_ROOM])
        if mc.KEY_CURRENTSET in payload:
            self.target_temperature = payload[mc.KEY_CURRENTSET] / self.device_scale
            if (
                len(payload) == 2
            ):  # { "id: "...", "room": ...} or { "id: "...", "currentSet": ...}
                # only room temperature/setpoint updated -> this is 99.9% a PUSH
                # whenever the target temp or mode changes
                self.flush_state()
                self.handlers[mn_h.Appliance_Hub_Mts100_Mode].schedule_get(self.id)
                return
        if mc.KEY_MIN in payload:
            self.min_temp = payload[mc.KEY_MIN] / self.device_scale
        if mc.KEY_MAX in payload:
            self.max_temp = payload[mc.KEY_MAX] / self.device_scale
        if mc.KEY_HEATING in payload:
            self._mts_active = payload[mc.KEY_HEATING]
        if mc.KEY_OPENWINDOW in payload:
            self.binary_sensor_window.update_boolean_value(payload[mc.KEY_OPENWINDOW])

        for (
            key_temp,
            number_preset_temperature,
        ) in self.number_preset_temperature.items():
            try:
                number_preset_temperature.update_device_value(payload[key_temp])
            except KeyError:
                pass
        self.flush_state()

    def _parse_mts100(self, payload, /):
        """parse digest key for mts100/mts100v3 subdevice"""
        self._mts_mode = payload[mc.KEY_MODE]
        self.flush_state()

    def _parse_mts150(self, payload, /):
        """parse digest key for mts150/mts150p subdevice"""
        self._mts_mode = payload[mc.KEY_MODE]
        # TODO: parse more keys?
        self.flush_state()

    def update_scheduleb_mode(self, mode, /):
        self.extra_state_attributes[mc.KEY_SCHEDULEBMODE] = mode
        self.schedule._schedule_entry_count_max = mode
        self.schedule._schedule_entry_count_min = mode
