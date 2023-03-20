from __future__ import annotations

import typing
import weakref

from homeassistant.helpers import device_registry

from .binary_sensor import MLBinarySensor
from .calendar import MLCalendar
from .climate import MtsClimate
from .const import DOMAIN, PARAM_HEARTBEAT_PERIOD, PARAM_HUBBATTERY_UPDATE_PERIOD
from .helpers import Loggable, schedule_async_callback
from .meross_device import MerossDevice
from .meross_profile import ApiProfile
from .merossclient import (  # mEROSS cONST
    const as mc,
    get_default_arguments,
    get_productnameuuid,
)
from .number import MLHubAdjustNumber
from .sensor import MLSensor
from .switch import MLSwitch

WELL_KNOWN_TYPE_MAP: dict[str, typing.Callable] = dict(
    {
        # typical entries (they're added on SubDevice declaration)
        # mc.TYPE_MS100: MS100SubDevice,
        # mc.TYPE_MTS100: MTS100SubDevice,
    }
)
# subdevices types listed in NS_APPLIANCE_HUB_SENSOR_ALL
SENSOR_ALL_TYPESET = (mc.TYPE_MS100, mc.TYPE_SMOKEALARM)
# subdevices types listed in NS_APPLIANCE_HUB_MTS100_ALL
MTS100_ALL_TYPESET = (mc.TYPE_MTS100, mc.TYPE_MTS100V3, mc.TYPE_MTS150)

# REMOVE
TRICK = False


class MerossDeviceHub(MerossDevice):
    """
    Specialized MerossDevice for smart hub(s) like MSH300
    """

    _lastupdate_battery = 0
    _lastupdate_sensor = None
    _lastupdate_mts100 = None

    def __init__(self, descriptor, entry):
        super().__init__(descriptor, entry)
        self.subdevices: dict[object, MerossSubDevice] = {}
        # invoke platform(s) async_setup_entry
        # in order to be able to eventually add entities when they 'pop up'
        # in the hub (see also self.async_add_sensors)
        self.platforms[MLSensor.PLATFORM] = None
        self.platforms[MLBinarySensor.PLATFORM] = None
        self.platforms[MtsClimate.PLATFORM] = None
        self.platforms[MLHubAdjustNumber.PLATFORM] = None
        self.platforms[MLSwitch.PLATFORM] = None
        self.platforms[MLCalendar.PLATFORM] = None

        # REMOVE
        global TRICK
        if TRICK:
            TRICK = False
            MS100SubDevice(
                self,
                {
                    "id": "120027D281CF",
                    "status": 1,
                    "onoff": 0,
                    "lastActiveTime": 1638019438,
                    "ms100": {
                        "latestTime": 1638019438,
                        "latestTemperature": 224,
                        "latestHumidity": 460,
                        "voltage": 2766,
                    },
                },
            )

    def _init_hub(self, payload: dict):
        for p_subdevice in payload[mc.KEY_SUBDEVICE]:
            self._subdevice_build(p_subdevice)

    def _handle_Appliance_Hub_Sensor_All(self, header: dict, payload: dict):
        if self._subdevice_parse(payload, mc.KEY_ALL):
            self._lastupdate_sensor = self.lastresponse

    def _handle_Appliance_Hub_Sensor_TempHum(self, header: dict, payload: dict):
        self._subdevice_parse(payload, mc.KEY_TEMPHUM)

    def _handle_Appliance_Hub_Sensor_Smoke(self, header: dict, payload: dict):
        self._subdevice_parse(payload, mc.KEY_SMOKEALARM)

    def _handle_Appliance_Hub_Sensor_Adjust(self, header: dict, payload: dict):
        self._subdevice_parse(payload, mc.KEY_ADJUST)

    def _handle_Appliance_Hub_Mts100_All(self, header: dict, payload: dict):
        if self._subdevice_parse(payload, mc.KEY_ALL):
            self._lastupdate_mts100 = self.lastresponse

    def _handle_Appliance_Hub_Mts100_Mode(self, header: dict, payload: dict):
        self._subdevice_parse(payload, mc.KEY_MODE)

    def _handle_Appliance_Hub_Mts100_Temperature(self, header: dict, payload: dict):
        self._subdevice_parse(payload, mc.KEY_TEMPERATURE)

    def _handle_Appliance_Hub_Mts100_Adjust(self, header: dict, payload: dict):
        self._subdevice_parse(payload, mc.KEY_ADJUST)

    def _handle_Appliance_Hub_Mts100_ScheduleB(self, header: dict, payload: dict):
        self._subdevice_parse(payload, mc.KEY_SCHEDULE)

    def _handle_Appliance_Hub_ToggleX(self, header: dict, payload: dict):
        self._subdevice_parse(payload, mc.KEY_TOGGLEX)

    def _handle_Appliance_Hub_Battery(self, header: dict, payload: dict):
        self._lastupdate_battery = self.lastresponse
        self._subdevice_parse(payload, mc.KEY_BATTERY)

    def _handle_Appliance_Hub_Online(self, header: dict, payload: dict):
        self._subdevice_parse(payload, mc.KEY_ONLINE)

    def _handle_Appliance_Digest_Hub(self, header: dict, payload: dict):
        self._parse_hub(payload[mc.KEY_HUB])

    def _subdevice_build(self, p_subdevice: dict):
        # parses the subdevice payload in 'digest' to look for a well-known type
        # and builds accordingly
        _type = None
        for p_key, p_value in p_subdevice.items():
            if isinstance(p_value, dict):
                _type = p_key
                break
        else:
            # the hub could report incomplete info anytime so beware.
            # this is true when subdevice is offline and hub has no recent info
            # we'll check our device registry for luck
            try:
                hassdevice = device_registry.async_get(
                    ApiProfile.hass
                ).async_get_device(identifiers={(DOMAIN, p_subdevice[mc.KEY_ID])})
                if hassdevice is None:
                    return None
                _type = hassdevice.model
            except:
                return None
        deviceclass = WELL_KNOWN_TYPE_MAP.get(_type)  # type: ignore
        if deviceclass is None:
            # build something anyway...
            return MerossSubDevice(self, p_subdevice, _type)  # type: ignore
        return deviceclass(self, p_subdevice)

    def _subdevice_parse(self, payload: dict, key: str):
        count = 0
        if isinstance(p_subdevices := payload.get(key), list):
            for p_subdevice in p_subdevices:
                p_id = p_subdevice[mc.KEY_ID]
                subdevice = self.subdevices.get(p_id)
                if subdevice is None:
                    # force a rescan since we discovered a new subdevice
                    # only if it appears this device is online else it
                    # would be a waste since we wouldnt have enough info
                    # to correctly build that
                    if (
                        p_subdevice.get(mc.KEY_ONLINE, {}).get(mc.KEY_STATUS)
                        == mc.STATUS_ONLINE
                    ):
                        self.request(*get_default_arguments(mc.NS_APPLIANCE_SYSTEM_ALL))
                else:
                    method = getattr(subdevice, f"_parse_{key}", None)
                    if method is not None:
                        method(p_subdevice)
                        count += 1
        return count

    def _parse_hub(self, p_hub: dict):
        # This is usually called inside _parse_all as part of the digest parsing
        # Here we'll check the fresh subdevice list against the actual one and
        # eventually manage newly added subdevices or removed ones #119
        # telling the caller to persist the changed configuration (self.needsave)
        if isinstance(p_subdevices := p_hub.get(mc.KEY_SUBDEVICE), list):
            subdevices_actual = set(self.subdevices.keys())
            for p_digest in p_subdevices:
                p_id = p_digest.get(mc.KEY_ID)
                subdevice = self.subdevices.get(p_id)
                if subdevice is None:
                    subdevice = self._subdevice_build(p_digest)
                    if subdevice is None:
                        continue
                    self.needsave = True
                else:
                    subdevices_actual.remove(p_id)
                subdevice.update_digest(p_digest)

            if subdevices_actual:
                # now we're left with non-existent (removed) subdevices
                self.needsave = True
                for p_id in subdevices_actual:
                    subdevice = self.subdevices[p_id]
                    self.warning(
                        "removing subdevice %s(%s) - configuration will be reloaded in 15 sec",
                        subdevice.type,
                        p_id,
                    )
                # before reloading we have to be sure configentry data were persisted
                # so we'll wait a bit..
                # also, we're not registering an unsub and we're not checking
                # for redundant invocations (playing a bit unsafe that is)
                async def _async_setup_again():
                    await ApiProfile.hass.config_entries.async_reload(self.entry_id)

                schedule_async_callback(ApiProfile.hass, 15, _async_setup_again)

    def _build_subdevices_payload(self, types: tuple, count: int):
        """
        This generator helps dealing with hubs hosting an high number
        of subdevices: when queried, the response payload might became huge
        with overflow issues likely on the device side (see #244).
        If this is the case, we'll split the request for fewer
        devices at a time. The count param allows some flexibility depending
        on expected payload size but we might have no clue especially for
        bigger payloads like NS_APPLIANCE_HUB_MTS100_SCHEDULEB
        """
        if len(subdevices := self.subdevices) > count:
            payload = []
            for subdevice in subdevices.values():
                if subdevice.type in types:
                    payload.append({mc.KEY_ID: subdevice.id})
                    if len(payload) == count:
                        yield payload
                        payload = []
            if payload:
                yield payload
        else:
            yield []

    async def async_request_updates(self, epoch, namespace):
        await super().async_request_updates(epoch, namespace)
        # we just ask for updates when something pops online (_lastupdate_xxxx == 0)
        # relying on push (over MQTT) or base polling updates (only HTTP) for any other changes
        # if _lastupdate_xxxx is None then it means that device class is not present in the hub
        # and we totally skip the request. This is especially true since I've discovered hubs
        # don't expose the full set of namespaces until a real subdevice type is binded.
        # If this is the case we would ask a namespace which is not supported at the moment
        # (see #167).
        # Also, we check here and there if the hub went offline while polling and we skip
        # the rest of the sequence (see super().async_request_updates for the same logic)
        if not self._online:
            return

        needpoll = (namespace is not None) or (self.lastmqttresponse == 0)
        if self._lastupdate_sensor is not None:
            if needpoll or (self._lastupdate_sensor == 0):
                await self.async_request(
                    *get_default_arguments(mc.NS_APPLIANCE_HUB_SENSOR_ADJUST)
                )
                for p in self._build_subdevices_payload(SENSOR_ALL_TYPESET, 8):
                    await self.async_request(
                        mc.NS_APPLIANCE_HUB_SENSOR_ALL, mc.METHOD_GET, {mc.KEY_ALL: p}
                    )

        if not self._online:
            return
        if self._lastupdate_mts100 is not None:
            if needpoll or (self._lastupdate_mts100 == 0):
                await self.async_request(
                    *get_default_arguments(mc.NS_APPLIANCE_HUB_MTS100_ADJUST)
                )
                for p in self._build_subdevices_payload(MTS100_ALL_TYPESET, 8):
                    await self.async_request(
                        mc.NS_APPLIANCE_HUB_MTS100_ALL, mc.METHOD_GET, {mc.KEY_ALL: p}
                    )
                for p in self._build_subdevices_payload(MTS100_ALL_TYPESET, 4):
                    if mc.NS_APPLIANCE_HUB_MTS100_SCHEDULEB in self.descriptor.ability:
                        await self.async_request(
                            mc.NS_APPLIANCE_HUB_MTS100_SCHEDULEB,
                            mc.METHOD_GET,
                            {mc.KEY_SCHEDULE: p},
                        )

        if not self._online:
            return
        if (epoch - self._lastupdate_battery) >= PARAM_HUBBATTERY_UPDATE_PERIOD:
            await self.async_request(
                *get_default_arguments(mc.NS_APPLIANCE_HUB_BATTERY)
            )

        # we also need to check for TOGGLEX state in case but this is not always needed:
        # for example, if we just have mts100-likes devices, their 'togglex' state is already carried by
        # NS_APPLIANCE_HUB_MTS100_ALL, or we may know some subdevices dont actually have togglex
        if not self._online:
            return
        if mc.NS_APPLIANCE_HUB_TOGGLEX in self.descriptor.ability:
            if needpoll:
                _excluded = (
                    mc.TYPE_MS100,
                    mc.TYPE_MTS100,
                    mc.TYPE_MTS100V3,
                    mc.TYPE_MTS150,
                )
                for subdevice in self.subdevices.values():
                    if subdevice.type not in _excluded:
                        await self.async_request(
                            *get_default_arguments(mc.NS_APPLIANCE_HUB_TOGGLEX)
                        )
                        break


class MerossSubDevice(Loggable):

    _deviceentry = None  # weakly cached entry to the device registry

    def __init__(self, hub: MerossDeviceHub, p_digest: dict, _type: str):
        self.id = _id = p_digest[mc.KEY_ID]
        self.type = _type
        self.p_digest = p_digest
        self._online = False
        self.hub = hub
        hub.subdevices[_id] = self
        self.deviceentry_id = {"identifiers": {(DOMAIN, _id)}}
        with self.exception_warning("DeviceRegistry.async_get_or_create"):
            self._deviceentry = weakref.ref(
                device_registry.async_get(ApiProfile.hass).async_get_or_create(
                    config_entry_id=hub.entry_id,
                    via_device=next(iter(hub.deviceentry_id["identifiers"])),
                    manufacturer=mc.MANUFACTURER,
                    name=get_productnameuuid(_type, str(_id)),
                    model=_type,
                    **self.deviceentry_id,
                )
            )

        self.sensor_battery = self.build_sensor(MLSensor.DeviceClass.BATTERY)
        # this is a generic toggle we'll setup in case the subdevice
        # 'advertises' it and no specialized implementation is in place
        self.switch_togglex = None

    def log(self, level: int, msg: str, *args, **kwargs):
        self.hub.log(
            level, f"{self.__class__.__name__}({self.name}) {msg}", *args, **kwargs
        )

    def warning(self, msg: str, *args, **kwargs):
        self.hub.warning(
            f"{self.__class__.__name__}({self.name}) {msg}", *args, **kwargs
        )

    def build_sensor(self, device_class: MLSensor.DeviceClass):
        return MLSensor(self.hub, self.id, str(device_class), device_class, self)

    def build_sensor_noclass(self, entitykey: str):
        return MLSensor(self.hub, self.id, entitykey, None, self)

    def build_binary_sensor(self, device_class: MLBinarySensor.DeviceClass):
        return MLBinarySensor(self.hub, self.id, str(device_class), device_class, self)

    @property
    def online(self):
        return self._online

    @property
    def name(self) -> str:
        """
        returns a proper (friendly) device name for logging purposes
        """
        deviceentry = self._deviceentry and self._deviceentry()
        if deviceentry is None:
            deviceentry = device_registry.async_get(ApiProfile.hass).async_get_device(
                **self.deviceentry_id
            )
            if deviceentry is None:
                return get_productnameuuid(self.type, self.id)
            self._deviceentry = weakref.ref(deviceentry)

        return (
            deviceentry.name_by_user
            or deviceentry.name
            or get_productnameuuid(self.type, self.id)
        )

    def _setonline(self):
        # here we should request updates for all entities but
        # there could be some 'light' race conditions
        # since when the device (hub) comes online it requests itself
        # a full update and this could be the case.
        # If instead this online status change is due to the single
        # subdevice coming online then we'll just wait for the next
        # polling cycle by setting the battery update trigger..
        self.hub._lastupdate_battery = 0

    def update_digest(self, p_digest: dict):
        self.p_digest = p_digest
        self._parse_online(p_digest)

    def _parse_all(self, p_all: dict):
        # typically parses NS_APPLIANCE_HUB_SENSOR_ALL:
        # generally speaking this payload has a couple of well-known keys
        # plus a set of sensor values like (MS100 example):
        # {
        #     "id": "..."
        #     "online: "..."
        #     "temperature": {
        #         "latest": value
        #         ...
        #     }
        #     "humidity": {
        #         "latest": value
        #         ...
        #     }
        # }
        # so we just extract generic sensors where we find 'latest'
        # Luckily enough the key names in Meross will behave consistently in HA
        # at least for 'temperature' and 'humidity' (so far..) also, we divide
        # the value by 10 since that's a correct eurhystic for them (so far..).
        # Specialized subdevices might totally override this...
        self._parse_online(p_all.get(mc.KEY_ONLINE, {}))

        if self._online:
            for p_key, p_value in p_all.items():
                if isinstance(p_value, dict):
                    p_latest = p_value.get(mc.KEY_LATEST)
                    if isinstance(p_latest, int):
                        sensorattr = f"sensor_{p_key}"
                        sensor: MLSensor = getattr(self, sensorattr, None)  # type: ignore
                        if not sensor:
                            sensor = self.build_sensor(p_key)
                            setattr(self, sensorattr, sensor)
                        sensor.update_state(p_latest / 10)

    def _parse_battery(self, p_battery: dict):
        if self._online:
            self.sensor_battery.update_state(p_battery.get(mc.KEY_VALUE))

    def _parse_online(self, p_online: dict):
        if mc.KEY_STATUS in p_online:
            if p_online[mc.KEY_STATUS] == mc.STATUS_ONLINE:
                if self._online is False:
                    self._online = True
                    self._setonline()
            else:
                if self._online is True:
                    self._online = False
                    for entity in self.hub.entities.values():
                        # not every entity in hub is a 'subdevice' entity
                        if getattr(entity, "subdevice", None) is self:
                            entity.set_unavailable()

    def _parse_adjust(self, p_adjust: dict):
        for p_key, p_value in p_adjust.items():
            if p_key == mc.KEY_ID:
                continue
            number: MLHubAdjustNumber
            if (number := getattr(self, f"number_adjust_{p_key}", None)) is not None:  # type: ignore
                number.update_native_value(p_value)

    def _parse_togglex(self, p_togglex: dict):
        if self.switch_togglex is None:
            self.switch_togglex = MLSwitch(
                self.hub,
                self.id,
                None,
                MLSwitch.DeviceClass.SWITCH,
                self,
                mc.NS_APPLIANCE_HUB_TOGGLEX,
            )
            self.switch_togglex.key_channel = mc.KEY_ID
        self.switch_togglex._parse_togglex(p_togglex)


class MS100SubDevice(MerossSubDevice):
    def __init__(self, hub: MerossDeviceHub, p_digest: dict):
        super().__init__(hub, p_digest, mc.TYPE_MS100)
        self.sensor_temperature = self.build_sensor(MLSensor.DeviceClass.TEMPERATURE)
        self.sensor_humidity = self.build_sensor(MLSensor.DeviceClass.HUMIDITY)
        self.number_adjust_temperature = MLHubAdjustNumber(
            self,
            mc.KEY_TEMPERATURE,
            mc.NS_APPLIANCE_HUB_SENSOR_ADJUST,
            MLHubAdjustNumber.DeviceClass.TEMPERATURE,
            -5,
            5,
            0.1,
        )
        self.number_adjust_humidity = MLHubAdjustNumber(
            self,
            mc.KEY_HUMIDITY,
            mc.NS_APPLIANCE_HUB_SENSOR_ADJUST,
            MLHubAdjustNumber.DeviceClass.HUMIDITY,
            -20,
            20,
            1,
        )

    def _setonline(self):
        super()._setonline()
        self.hub._lastupdate_sensor = 0

    def update_digest(self, p_digest: dict):
        super().update_digest(p_digest)
        if self._online:
            self._parse_tempHum(p_digest[mc.TYPE_MS100])

    def _parse_tempHum(self, p_temphum: dict):
        if isinstance(value := p_temphum.get(mc.KEY_LATESTTEMPERATURE), int):
            self.sensor_temperature.update_state(value / 10)
        if isinstance(value := p_temphum.get(mc.KEY_LATESTHUMIDITY), int):
            self.sensor_humidity.update_state(value / 10)


WELL_KNOWN_TYPE_MAP[mc.TYPE_MS100] = MS100SubDevice


class MTS100SubDevice(MerossSubDevice):
    def __init__(
        self, hub: MerossDeviceHub, p_digest: dict, _type: str = mc.TYPE_MTS100
    ):
        super().__init__(hub, p_digest, _type)
        from .devices import mts100

        self.climate = mts100.Mts100Climate(self)
        self.number_comfort_temperature = mts100.Mts100SetPointNumber(
            self.climate, mts100.PRESET_COMFORT
        )
        self.number_sleep_temperature = mts100.Mts100SetPointNumber(
            self.climate, mts100.PRESET_SLEEP
        )
        self.number_away_temperature = mts100.Mts100SetPointNumber(
            self.climate, mts100.PRESET_AWAY
        )
        self.schedule = mts100.Mts100Schedule(self.climate)
        self.binary_sensor_window = self.build_binary_sensor(
            MLBinarySensor.DeviceClass.WINDOW
        )
        self.sensor_temperature = self.build_sensor(MLSensor.DeviceClass.TEMPERATURE)
        self.number_adjust_temperature = MLHubAdjustNumber(
            self,
            mc.KEY_TEMPERATURE,
            mc.NS_APPLIANCE_HUB_MTS100_ADJUST,
            MLHubAdjustNumber.DeviceClass.TEMPERATURE,
            -5,
            5,
            0.1,
        )

    def _setonline(self):
        super()._setonline()
        self.hub._lastupdate_mts100 = 0

    def _parse_all(self, p_all: dict):
        self._parse_online(p_all.get(mc.KEY_ONLINE, {}))

        climate = self.climate

        climate.scheduleBMode = p_all.get(mc.KEY_SCHEDULEBMODE)

        if isinstance(p_mode := p_all.get(mc.KEY_MODE), dict):
            climate._mts_mode = p_mode.get(mc.KEY_STATE)

        if isinstance(p_togglex := p_all.get(mc.KEY_TOGGLEX), dict):
            climate._mts_onoff = p_togglex.get(mc.KEY_ONOFF)

        if isinstance(p_temperature := p_all.get(mc.KEY_TEMPERATURE), dict):
            self._parse_temperature(p_temperature)
        else:
            climate.update_modes()
            self.schedule.update_climate_modes()

    def _parse_mode(self, p_mode: dict):
        climate = self.climate
        climate._mts_mode = p_mode.get(mc.KEY_STATE)
        climate.update_modes()
        self.schedule.update_climate_modes()

    def _parse_temperature(self, p_temperature: dict):
        climate = self.climate
        if isinstance(_t := p_temperature.get(mc.KEY_ROOM), int):
            climate._attr_current_temperature = _t / 10
            self.sensor_temperature.update_state(climate._attr_current_temperature)
        if isinstance(_t := p_temperature.get(mc.KEY_CURRENTSET), int):
            climate._attr_target_temperature = _t / 10
        if isinstance(_t := p_temperature.get(mc.KEY_MIN), int):
            climate._attr_min_temp = _t / 10
        if isinstance(_t := p_temperature.get(mc.KEY_MAX), int):
            climate._attr_max_temp = _t / 10
        if mc.KEY_HEATING in p_temperature:
            climate._mts_heating = p_temperature[mc.KEY_HEATING]
        climate.update_modes()
        self.schedule.update_climate_modes()

        if isinstance(_t := p_temperature.get(mc.KEY_COMFORT), int):
            self.number_comfort_temperature.update_native_value(_t)
        if isinstance(_t := p_temperature.get(mc.KEY_ECONOMY), int):
            self.number_sleep_temperature.update_native_value(_t)
        if isinstance(_t := p_temperature.get(mc.KEY_AWAY), int):
            self.number_away_temperature.update_native_value(_t)

        if mc.KEY_OPENWINDOW in p_temperature:
            self.binary_sensor_window.update_onoff(p_temperature[mc.KEY_OPENWINDOW])

    def _parse_togglex(self, p_togglex: dict):
        climate = self.climate
        climate._mts_onoff = p_togglex.get(mc.KEY_ONOFF)
        climate.update_modes()
        self.schedule.update_climate_modes()

    def _parse_schedule(self, p_schedule: dict):
        self.schedule._parse_schedule(p_schedule)


WELL_KNOWN_TYPE_MAP[mc.TYPE_MTS100] = MTS100SubDevice


class MTS100V3SubDevice(MTS100SubDevice):
    def __init__(self, hub: MerossDeviceHub, p_digest: dict):
        super().__init__(hub, p_digest, mc.TYPE_MTS100V3)


WELL_KNOWN_TYPE_MAP[mc.TYPE_MTS100V3] = MTS100V3SubDevice


class MTS150SubDevice(MTS100SubDevice):
    def __init__(self, hub: MerossDeviceHub, p_digest: dict):
        super().__init__(hub, p_digest, mc.TYPE_MTS150)


WELL_KNOWN_TYPE_MAP[mc.TYPE_MTS150] = MTS150SubDevice


class SmokeAlarmSubDevice(MerossSubDevice):
    def __init__(self, hub: MerossDeviceHub, p_digest: dict):
        super().__init__(hub, p_digest, mc.TYPE_SMOKEALARM)
        self.sensor_status = self.build_sensor_noclass("status")
        self.sensor_interConn = self.build_sensor_noclass("interConn")

    def _setonline(self):
        super()._setonline()
        self.hub._lastupdate_sensor = 0

    def update_digest(self, p_digest: dict):
        super().update_digest(p_digest)
        if self._online:
            self._parse_smokeAlarm(p_digest[mc.TYPE_SMOKEALARM])

    def _parse_all(self, p_all: dict):
        self._parse_online(p_all.get(mc.KEY_ONLINE, {}))
        if self._online:
            self._parse_smokeAlarm(p_all[mc.TYPE_SMOKEALARM])

    def _parse_smokeAlarm(self, p_smokealarm: dict):
        if isinstance(value := p_smokealarm.get(mc.KEY_STATUS), int):
            self.sensor_status.update_state(value)
        if isinstance(value := p_smokealarm.get(mc.KEY_INTERCONN), int):
            self.sensor_interConn.update_state(value)


WELL_KNOWN_TYPE_MAP[mc.TYPE_SMOKEALARM] = SmokeAlarmSubDevice
