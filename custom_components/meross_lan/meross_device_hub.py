from __future__ import annotations

import asyncio
import typing

from homeassistant.helpers import device_registry

from . import meross_entity as me
from .binary_sensor import MLBinarySensor
from .calendar import MLCalendar
from .climate import MtsClimate
from .const import DOMAIN, PARAM_HUBBATTERY_UPDATE_PERIOD
from .helpers import ApiProfile, SmartPollingStrategy, schedule_async_callback
from .meross_device import MerossDevice, MerossDeviceBase
from .merossclient import (  # mEROSS cONST
    const as mc,
    get_default_arguments,
    get_productnameuuid,
    is_device_online,
)
from .number import MLHubAdjustNumber
from .sensor import MLSensor
from .switch import MLSwitch

if typing.TYPE_CHECKING:
    from .devices.mts100 import Mts100Climate, Mts100Schedule, Mts100SetPointNumber
    from .meross_device import ResponseCallbackType
    from .meross_entity import MerossEntity


WELL_KNOWN_TYPE_MAP: dict[str, typing.Callable] = dict(
    {
        # typical entries (they're added on SubDevice declaration)
        # mc.TYPE_MS100: MS100SubDevice,
        # mc.TYPE_MTS100: MTS100SubDevice,
    }
)
# subdevices types listed in NS_APPLIANCE_HUB_MTS100_ALL
MTS100_ALL_TYPESET = {mc.TYPE_MTS100, mc.TYPE_MTS100V3, mc.TYPE_MTS150}

# REMOVE
TRICK = False


class MerossDeviceHub(MerossDevice):
    """
    Specialized MerossDevice for smart hub(s) like MSH300
    """

    __slots__ = (
        "subdevices",
        "_lastupdate_sensor",
        "_lastupdate_mts100",
        "_unsub_setup_again",
    )

    def __init__(self, descriptor, entry):
        self.subdevices: dict[object, MerossSubDevice] = {}
        self._lastupdate_sensor = None
        self._lastupdate_mts100 = None
        self._unsub_setup_again: asyncio.TimerHandle | None = None
        super().__init__(descriptor, entry)
        # invoke platform(s) async_setup_entry
        # in order to be able to eventually add entities when they 'pop up'
        # in the hub (see also self.async_add_sensors)
        self.platforms[MLSensor.PLATFORM] = None
        self.platforms[MLBinarySensor.PLATFORM] = None
        self.platforms[MtsClimate.PLATFORM] = None
        self.platforms[MLHubAdjustNumber.PLATFORM] = None
        self.platforms[MLSwitch.PLATFORM] = None
        self.platforms[MLCalendar.PLATFORM] = None

        self.polling_dictionary[mc.NS_APPLIANCE_HUB_BATTERY] = SmartPollingStrategy(
            mc.NS_APPLIANCE_HUB_BATTERY, None, PARAM_HUBBATTERY_UPDATE_PERIOD
        )

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

    # interface: EntityManager
    def managed_entities(self, platform):
        entities = super().managed_entities(platform)
        for subdevice in self.subdevices.values():
            entities.extend(subdevice.managed_entities(platform))
        return entities

    # interface: MerossDevice
    async def async_shutdown(self):
        if self._unsub_setup_again:
            self._unsub_setup_again.cancel()
            self._unsub_setup_again = None
        # shutdown the base first to stop polling in case
        await super().async_shutdown()
        for subdevice in self.subdevices.values():
            await subdevice.async_shutdown()
        self.subdevices.clear()

    def _set_offline(self):
        for subdevice in self.subdevices.values():
            subdevice._set_offline()
        super()._set_offline()

    def _init_hub(self, payload: dict):
        for p_subdevice in payload[mc.KEY_SUBDEVICE]:
            self._subdevice_build(p_subdevice)

    async def async_request_updates(self, epoch: float, namespace: str | None):
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

        needpoll = namespace or (not self._mqtt_active)
        if self._lastupdate_sensor is not None:
            if needpoll or (self._lastupdate_sensor == 0):
                for p in self._build_subdevices_payload(MTS100_ALL_TYPESET, False, 8):
                    await self.async_request(
                        mc.NS_APPLIANCE_HUB_SENSOR_ALL, mc.METHOD_GET, {mc.KEY_ALL: p}
                    )

        if not self._online:
            return
        if self._lastupdate_mts100 is not None:
            if needpoll or (self._lastupdate_mts100 == 0):
                for p in self._build_subdevices_payload(MTS100_ALL_TYPESET, True, 8):
                    await self.async_request(
                        mc.NS_APPLIANCE_HUB_MTS100_ALL, mc.METHOD_GET, {mc.KEY_ALL: p}
                    )
                for p in self._build_subdevices_payload(MTS100_ALL_TYPESET, True, 4):
                    if mc.NS_APPLIANCE_HUB_MTS100_SCHEDULEB in self.descriptor.ability:
                        await self.async_request(
                            mc.NS_APPLIANCE_HUB_MTS100_SCHEDULEB,
                            mc.METHOD_GET,
                            {mc.KEY_SCHEDULE: p},
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

    # interface: self
    def _handle_Appliance_Hub_Sensor_All(self, header: dict, payload: dict):
        self._lastupdate_sensor = self.lastresponse
        self._subdevice_parse(mc.KEY_ALL, payload)

    def _handle_Appliance_Hub_Sensor_TempHum(self, header: dict, payload: dict):
        self._subdevice_parse(mc.KEY_TEMPHUM, payload)

    def _handle_Appliance_Hub_Sensor_Smoke(self, header: dict, payload: dict):
        self._subdevice_parse(mc.KEY_SMOKEALARM, payload)

    def _handle_Appliance_Hub_Sensor_Adjust(self, header: dict, payload: dict):
        self._subdevice_parse(mc.KEY_ADJUST, payload)

    def _handle_Appliance_Hub_Mts100_All(self, header: dict, payload: dict):
        self._lastupdate_mts100 = self.lastresponse
        self._subdevice_parse(mc.KEY_ALL, payload)

    def _handle_Appliance_Hub_Mts100_Mode(self, header: dict, payload: dict):
        self._subdevice_parse(mc.KEY_MODE, payload)

    def _handle_Appliance_Hub_Mts100_Temperature(self, header: dict, payload: dict):
        self._subdevice_parse(mc.KEY_TEMPERATURE, payload)

    def _handle_Appliance_Hub_Mts100_Adjust(self, header: dict, payload: dict):
        self._subdevice_parse(mc.KEY_ADJUST, payload)

    def _handle_Appliance_Hub_Mts100_ScheduleB(self, header: dict, payload: dict):
        self._subdevice_parse(mc.KEY_SCHEDULE, payload)

    def _handle_Appliance_Hub_ToggleX(self, header: dict, payload: dict):
        self._subdevice_parse(mc.KEY_TOGGLEX, payload)

    def _handle_Appliance_Hub_Battery(self, header: dict, payload: dict):
        self._subdevice_parse(mc.KEY_BATTERY, payload)

    def _handle_Appliance_Hub_Online(self, header: dict, payload: dict):
        self._subdevice_parse(mc.KEY_ONLINE, payload)

    def _handle_Appliance_Hub_SubdeviceList(self, header: dict, payload: dict):
        """
        {
            'subdeviceList': {
                'subdevice': [
                    {'id': '120027D21C19', 'status': 1, 'time': 1623423242, 'hardware': '0000', 'firmware': '0000'},
                    {'id': '01008C11', 'status': 0, 'time': 0},
                    {'id': '0100783A', 'status': 0, 'time': 0}
                ],
                'needReply': 1
            }
        }
        """
        p_subdevicelist = payload[mc.KEY_SUBDEVICELIST]
        for p_subdevice in p_subdevicelist[mc.KEY_SUBDEVICE]:
            # TODO: decode subdeviceList
            # actually, the sample payload is reporting status=1 for a device which appears to be offline
            # is it likely unpaired?
            pass

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
                if not hassdevice:
                    return None
                _type = hassdevice.model
            except Exception:
                return None

        if _type in MTS100_ALL_TYPESET:
            self._lastupdate_mts100 = 0
            if mc.NS_APPLIANCE_HUB_MTS100_ADJUST not in self.polling_dictionary:
                self.polling_dictionary[
                    mc.NS_APPLIANCE_HUB_MTS100_ADJUST
                ] = SmartPollingStrategy(mc.NS_APPLIANCE_HUB_MTS100_ADJUST)
        else:
            self._lastupdate_sensor = 0
            if mc.NS_APPLIANCE_HUB_SENSOR_ADJUST not in self.polling_dictionary:
                self.polling_dictionary[
                    mc.NS_APPLIANCE_HUB_SENSOR_ADJUST
                ] = SmartPollingStrategy(mc.NS_APPLIANCE_HUB_SENSOR_ADJUST)
            if mc.NS_APPLIANCE_HUB_TOGGLEX in self.descriptor.ability:
                # this is a status message irrelevant for mts100(s) and
                # other types. If not use an MQTT-PUSH friendly startegy
                if _type not in (mc.TYPE_MS100,):
                    self.polling_dictionary[
                        mc.NS_APPLIANCE_HUB_TOGGLEX
                    ] = SmartPollingStrategy(mc.NS_APPLIANCE_HUB_TOGGLEX)

        if deviceclass := WELL_KNOWN_TYPE_MAP.get(_type):  # type: ignore
            return deviceclass(self, p_subdevice)
        # build something anyway...
        return MerossSubDevice(self, p_subdevice, _type)  # type: ignore

    def _subdevice_parse(self, key: str, payload: dict):
        if isinstance(p_subdevices := payload.get(key), list):
            for p_subdevice in p_subdevices:
                p_id = p_subdevice[mc.KEY_ID]
                if subdevice := self.subdevices.get(p_id):
                    subdevice._parse(key, p_subdevice)
                else:
                    # force a rescan since we discovered a new subdevice
                    # only if it appears this device is online else it
                    # would be a waste since we wouldnt have enough info
                    # to correctly build that
                    if is_device_online(p_subdevice):
                        self.request(*get_default_arguments(mc.NS_APPLIANCE_SYSTEM_ALL))

    def _parse_hub(self, p_hub: dict):
        # This is usually called inside _parse_all as part of the digest parsing
        # Here we'll check the fresh subdevice list against the actual one and
        # eventually manage newly added subdevices or removed ones #119
        # telling the caller to persist the changed configuration (self.needsave)
        subdevices_actual = set(self.subdevices.keys())
        for p_digest in p_hub[mc.KEY_SUBDEVICE]:
            p_id = p_digest.get(mc.KEY_ID)
            if subdevice := self.subdevices.get(p_id):
                subdevices_actual.remove(p_id)
            elif subdevice := self._subdevice_build(p_digest):
                self.needsave = True
            else:
                continue
            subdevice.parse_digest(p_digest)

        if subdevices_actual:
            # now we're left with non-existent (removed) subdevices
            self.needsave = True
            for p_id in subdevices_actual:
                subdevice = self.subdevices[p_id]
                self.warning(
                    "removing subdevice %s(%s) - configuration will be reloaded in 15 sec",
                    subdevice.name,
                    p_id,
                )

            # before reloading we have to be sure configentry data were persisted
            # so we'll wait a bit..
            async def _async_setup_again():
                self._unsub_setup_again = None
                await ApiProfile.hass.config_entries.async_reload(self.config_entry_id)

            self._unsub_setup_again = schedule_async_callback(
                ApiProfile.hass, 15, _async_setup_again
            )

    def _build_subdevices_payload(
        self, types: typing.Collection, included: bool, count: int
    ):
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
                if (subdevice.type in types) == included:
                    payload.append({mc.KEY_ID: subdevice.id})
                    if len(payload) == count:
                        yield payload
                        payload = []
            if payload:
                yield payload
        else:
            yield []


class MerossSubDevice(MerossDeviceBase):
    """
    MerossSubDevice introduces some hybridization in EntityManager:
    (owned) entities will refer to MerossSubDevice effectively as if
    it were a full-fledged device but some EntityManager properties
    are overriden in order to manage ConfigEntry setup/unload since
    MerossSubDevice doesn't actively represent one (it delegates this to
    the owning MerossDeviceHub)
    """

    __slots__ = (
        "hub",
        "type",
        "p_digest",
        "sensor_battery",
        "switch_togglex",
    )

    def __init__(self, hub: MerossDeviceHub, p_digest: dict, _type: str):
        id_ = p_digest[mc.KEY_ID]
        super().__init__(
            id_,
            hub.config_entry_id,
            default_name=get_productnameuuid(_type, id_),
            model=_type,
            via_device=next(iter(hub.deviceentry_id["identifiers"])),
        )
        self.hub = hub
        self.type = _type
        self.p_digest = p_digest
        self.platforms = hub.platforms
        hub.subdevices[id_] = self
        self.sensor_battery = self.build_sensor_c(MLSensor.DeviceClass.BATTERY)
        # this is a generic toggle we'll setup in case the subdevice
        # 'advertises' it and no specialized implementation is in place
        self.switch_togglex: MLSwitch | None = None

    # interface: Loggable
    def log(self, level: int, msg: str, *args, **kwargs):
        self.hub.log(
            level, f"{self.__class__.__name__}({self.name}) {msg}", *args, **kwargs
        )

    def warning(self, msg: str, *args, **kwargs):
        self.hub.warning(
            f"{self.__class__.__name__}({self.name}) {msg}", *args, **kwargs
        )

    # interface: EntityManager
    def generate_unique_id(self, entity: MerossEntity):
        """
        flexible policy in order to generate unique_ids for entities:
        This is an helper needed to better control migrations in code
        which could/would lead to a unique_id change.
        We could put here code checks in order to avoid entity_registry
        migrations
        """
        return f"{self.hub.id}_{entity.id}"

    # interface: MerossDeviceBase
    async def async_shutdown(self):
        self.platforms = {}  # avoid super() clearing the MerossDeviceHub.platforms
        await super().async_shutdown()
        self.hub: MerossDeviceHub = None  # type: ignore
        self.sensor_battery: MLSensor = None  # type: ignore
        self.switch_togglex = None

    async def async_request(
        self,
        namespace: str,
        method: str,
        payload: dict,
        response_callback: ResponseCallbackType | None = None,
    ):
        await self.hub.async_request(namespace, method, payload, response_callback)

    def _get_device_info_name_key(self) -> str:
        return mc.KEY_SUBDEVICENAME

    def _get_internal_name(self) -> str:
        return get_productnameuuid(self.type, self.id)

    # interface: self
    def build_sensor(
        self, entitykey: str, device_class: MLSensor.DeviceClass | None = None
    ):
        return MLSensor(self, self.id, entitykey, device_class)

    def build_sensor_c(self, device_class: MLSensor.DeviceClass):
        return MLSensor(self, self.id, str(device_class), device_class)

    def build_binary_sensor(
        self, entitykey: str, device_class: MLBinarySensor.DeviceClass | None = None
    ):
        return MLBinarySensor(self, self.id, entitykey, device_class)

    def build_binary_sensor_c(self, device_class: MLBinarySensor.DeviceClass):
        return MLBinarySensor(self, self.id, str(device_class), device_class)

    def _parse(self, key: str, payload: dict):
        with self.exception_warning("_parse(%s, %s)", key, str(payload), timeout=14400):
            method = getattr(self, f"_parse_{key}", None)
            if method:
                method(payload)
                return
            # This happens when we still haven't 'normalized' the device structure
            # so we'll euristically generate sensors for device properties
            # This is the case for when we see newer devices and we don't know
            # their payloads and features.
            # as for know we've seen "smokeAlarm" and "doorWindow" subdevices
            # carrying similar payloads structures. We'll be conservative
            # and generate generic sensors for any key, except "lmTime"
            for subkey, subvalue in payload.items():
                if subkey in {mc.KEY_LMTIME, mc.KEY_LMTIME_}:
                    continue
                entitykey = f"{key}_{subkey}"
                sensorattr = f"sensor_{entitykey}"
                sensor: MLSensor
                sensor = getattr(self, sensorattr, None)  # type: ignore
                if not sensor:
                    sensor = self.build_sensor(entitykey)
                    setattr(self, sensorattr, sensor)
                sensor.update_state(subvalue)

    def parse_digest(self, p_digest: dict):
        """
        digest payload (from NS_ALL or HUB digest)
        {
            "id": "160020100486",  # subdev id
            "status": 1,  # online "status"
            "onoff": 0,  # togglex "onoff"
            "lastActiveTime": 1681996722,

            # and a subdev type specific key:
            # sometimes this child payload is the same
            # carried in the NS_SENSOR_ALL for the subdev
            # other times it's different. "ms100" and "mts100x" series
            # valves carries an "ms100" ("mts100x") payload in digest and
            # a "tempHum" ("temperature" and more) payload in NS_SENSOR_ALL)

            "ms100": {
                  "latestTime": 1671039319,
                  "latestTemperature": 95,
                  "latestHumidity": 670,
                  "voltage": 2704
                }

            # or
            "doorWindow": {"status": 0, "lmTime": 1681983460}
        }
        """
        self.p_digest = p_digest
        self._parse_online(p_digest)
        if self._online:
            for _ in (
                self._parse(key, value)
                for key, value in p_digest.items()
                if key
                not in {mc.KEY_ID, mc.KEY_STATUS, mc.KEY_ONOFF, mc.KEY_LASTACTIVETIME}
                and isinstance(value, dict)
            ):
                pass

    def _parse_all(self, p_all: dict):
        # typically parses NS_APPLIANCE_HUB_SENSOR_ALL:
        # generally speaking this payload has a couple of well-known keys
        # plus a set of sensor values like:
        # {
        #     keys appearing in any subdevice type
        #     "id": "..."
        #     "online: {"status": 1, "lastActiveTime": ...}
        #
        #     keys in "ms100"
        #     "temperature": {"latest": value, ...}
        #     "humidity": {"latest": value, ...}
        #
        #     keys in "smokeAlarm"
        #     "smokeAlarm": {"status": value, "interConn": value, "lmtime": ...}
        #
        #     keys in "doorWindow"
        #     "doorWindow": {"status": value, "lmTime": ...}
        # }
        # so we just extract generic sensors where we find 'latest'
        # Luckily enough the key names in Meross will behave consistently in HA
        # at least for 'temperature' and 'humidity' (so far..) also, we divide
        # the value by 10 since that's a correct eurhystic for them (so far..).
        # Specialized subdevices might totally override this...
        self._parse_online(p_all.get(mc.KEY_ONLINE, {}))

        if self._online:
            for _ in (
                self._parse(key, value)
                for key, value in p_all.items()
                if key not in {mc.KEY_ID, mc.KEY_ONLINE} and isinstance(value, dict)
            ):
                pass

    def _parse_adjust(self, p_adjust: dict):
        for p_key, p_value in p_adjust.items():
            if p_key == mc.KEY_ID:
                continue
            number: MLHubAdjustNumber
            if number := getattr(self, f"number_adjust_{p_key}", None):  # type: ignore
                number.update_native_value(p_value)

    def _parse_battery(self, p_battery: dict):
        if self._online:
            self.sensor_battery.update_state(p_battery.get(mc.KEY_VALUE))

    def _parse_online(self, p_online: dict):
        if mc.KEY_STATUS in p_online:
            if p_online[mc.KEY_STATUS] == mc.STATUS_ONLINE:
                if not self._online:
                    self._set_online()
            else:
                if self._online:
                    self._set_offline()

    def _parse_togglex(self, p_togglex: dict):
        if not (switch_togglex := self.switch_togglex):
            self.switch_togglex = switch_togglex = MLSwitch(
                self,
                self.id,
                None,
                MLSwitch.DeviceClass.SWITCH,
                mc.NS_APPLIANCE_HUB_TOGGLEX,
            )
            switch_togglex._attr_entity_category = me.EntityCategory.DIAGNOSTIC
            switch_togglex.key_channel = mc.KEY_ID
        switch_togglex._parse_togglex(p_togglex)


class MS100SubDevice(MerossSubDevice):
    __slots__ = (
        "sensor_temperature",
        "sensor_humidity",
        "number_adjust_temperature",
        "number_adjust_humidity",
    )

    def __init__(self, hub: MerossDeviceHub, p_digest: dict):
        super().__init__(hub, p_digest, mc.TYPE_MS100)
        self.sensor_temperature = self.build_sensor_c(MLSensor.DeviceClass.TEMPERATURE)
        self.sensor_humidity = self.build_sensor_c(MLSensor.DeviceClass.HUMIDITY)
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

    async def async_shutdown(self):
        await super().async_shutdown()
        self.sensor_temperature: MLSensor = None  # type: ignore
        self.sensor_humidity: MLSensor = None  # type: ignore
        self.number_adjust_temperature: MLHubAdjustNumber = None  # type: ignore
        self.number_adjust_humidity: MLHubAdjustNumber = None  # type: ignore

    def _set_online(self):
        super()._set_online()
        self.hub._lastupdate_sensor = 0

    def _parse_humidity(self, p_humidity: dict):
        if isinstance(p_latest := p_humidity.get(mc.KEY_LATEST), int):
            self.sensor_humidity.update_state(p_latest / 10)

    def _parse_ms100(self, p_ms100: dict):
        self._parse_tempHum(p_ms100)

    def _parse_temperature(self, p_temperature: dict):
        if isinstance(p_latest := p_temperature.get(mc.KEY_LATEST), int):
            self.sensor_temperature.update_state(p_latest / 10)

    def _parse_tempHum(self, p_temphum: dict):
        if isinstance(value := p_temphum.get(mc.KEY_LATESTTEMPERATURE), int):
            self.sensor_temperature.update_state(value / 10)
        if isinstance(value := p_temphum.get(mc.KEY_LATESTHUMIDITY), int):
            self.sensor_humidity.update_state(value / 10)


WELL_KNOWN_TYPE_MAP[mc.TYPE_MS100] = MS100SubDevice


class MTS100SubDevice(MerossSubDevice):
    __slots__ = (
        "climate",
        "number_comfort_temperature",
        "number_sleep_temperature",
        "number_away_temperature",
        "number_adjust_temperature",
        "binary_sensor_window",
        "schedule",
        "sensor_temperature",
    )

    def __init__(
        self, hub: MerossDeviceHub, p_digest: dict, _type: str = mc.TYPE_MTS100
    ):
        super().__init__(hub, p_digest, _type)
        from .devices.mts100 import Mts100Climate, Mts100Schedule, Mts100SetPointNumber

        self.climate = Mts100Climate(self)
        self.number_comfort_temperature = Mts100SetPointNumber(
            self.climate, Mts100Climate.PRESET_COMFORT
        )
        self.number_sleep_temperature = Mts100SetPointNumber(
            self.climate, Mts100Climate.PRESET_SLEEP
        )
        self.number_away_temperature = Mts100SetPointNumber(
            self.climate, Mts100Climate.PRESET_AWAY
        )
        self.number_adjust_temperature = MLHubAdjustNumber(
            self,
            mc.KEY_TEMPERATURE,
            mc.NS_APPLIANCE_HUB_MTS100_ADJUST,
            MLHubAdjustNumber.DeviceClass.TEMPERATURE,
            -5,
            5,
            0.1,
        )
        self.binary_sensor_window = self.build_binary_sensor_c(
            MLBinarySensor.DeviceClass.WINDOW
        )
        self.schedule = Mts100Schedule(self.climate)
        self.sensor_temperature = self.build_sensor_c(MLSensor.DeviceClass.TEMPERATURE)

    async def async_shutdown(self):
        await super().async_shutdown()
        self.climate: Mts100Climate = None  # type: ignore
        self.number_comfort_temperature: Mts100SetPointNumber = None  # type: ignore
        self.number_sleep_temperature: Mts100SetPointNumber = None  # type: ignore
        self.number_away_temperature: Mts100SetPointNumber = None  # type: ignore
        self.schedule: Mts100Schedule = None  # type: ignore
        self.binary_sensor_window: MLBinarySensor = None  # type: ignore
        self.sensor_temperature: MLSensor = None  # type: ignore
        self.number_adjust_temperature = None  # type: ignore

    def _set_online(self):
        super()._set_online()
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

    def _parse_mts100(self, p_mts100: dict):
        pass

    def _parse_schedule(self, p_schedule: dict):
        self.schedule._parse_schedule(p_schedule)

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


WELL_KNOWN_TYPE_MAP[mc.TYPE_MTS100] = MTS100SubDevice


class MTS100V3SubDevice(MTS100SubDevice):
    def __init__(self, hub: MerossDeviceHub, p_digest: dict):
        super().__init__(hub, p_digest, mc.TYPE_MTS100V3)

    def _parse_mts100v3(self, p_mts100v3: dict):
        pass


WELL_KNOWN_TYPE_MAP[mc.TYPE_MTS100V3] = MTS100V3SubDevice


class MTS150SubDevice(MTS100SubDevice):
    def __init__(self, hub: MerossDeviceHub, p_digest: dict):
        super().__init__(hub, p_digest, mc.TYPE_MTS150)

    def _parse_mts150(self, p_mts150: dict):
        pass


WELL_KNOWN_TYPE_MAP[mc.TYPE_MTS150] = MTS150SubDevice


class GS559SubDevice(MerossSubDevice):
    STATUS_MAP = {
        17: "error_temperature",
        18: "error_smoke",
        19: "error_battery",
        20: "error_temperature",
        21: "error_smoke",
        22: "error_battery",
        23: "alarm_test",
        24: "alarm_temperature_high",
        25: "alarm_smoke",
        26: "alarm_temperature_high",
        27: "alarm_smoke",
        170: "ok",
    }

    STATUS_ALARM = {23, 24, 25, 26, 27}
    STATUS_ERROR = {17, 18, 19, 20, 21, 22}
    STATUS_MUTED = {20, 21, 22, 26, 27}

    __slots__ = (
        "binary_sensor_alarm",
        "binary_sensor_error",
        "binary_sensor_muted",
        "sensor_status",
        "sensor_interConn",
    )

    def __init__(self, hub: MerossDeviceHub, p_digest: dict):
        super().__init__(hub, p_digest, mc.TYPE_GS559)
        self.sensor_status = self.build_sensor(mc.KEY_STATUS, MLSensor.DeviceClass.ENUM)
        self.sensor_status._attr_translation_key = "smoke_alarm_status"
        self.sensor_interConn = self.build_sensor(
            mc.KEY_INTERCONN, MLSensor.DeviceClass.ENUM
        )
        self.binary_sensor_alarm = self.build_binary_sensor("alarm")
        self.binary_sensor_error = self.build_binary_sensor(
            "error", MLBinarySensor.DeviceClass.PROBLEM
        )
        self.binary_sensor_muted = self.build_binary_sensor("muted")

    async def async_shutdown(self):
        await super().async_shutdown()
        self.binary_sensor_muted: MLBinarySensor = None  # type: ignore
        self.binary_sensor_error: MLBinarySensor = None  # type: ignore
        self.binary_sensor_alarm: MLBinarySensor = None  # type: ignore
        self.sensor_status: MLSensor = None  # type: ignore
        self.sensor_interConn: MLSensor = None  # type: ignore

    def _set_online(self):
        super()._set_online()
        self.hub._lastupdate_sensor = 0

    def _parse_smokeAlarm(self, p_smokealarm: dict):
        if isinstance(value := p_smokealarm.get(mc.KEY_STATUS), int):
            self.binary_sensor_alarm.update_onoff(value in GS559SubDevice.STATUS_ALARM)
            self.binary_sensor_error.update_onoff(value in GS559SubDevice.STATUS_ERROR)
            self.binary_sensor_muted.update_onoff(value in GS559SubDevice.STATUS_MUTED)
            self.sensor_status.update_state(GS559SubDevice.STATUS_MAP.get(value, value))
        if isinstance(value := p_smokealarm.get(mc.KEY_INTERCONN), int):
            self.sensor_interConn.update_state(value)


WELL_KNOWN_TYPE_MAP[mc.TYPE_GS559] = GS559SubDevice
# smokeAlarm devices (mc.TYPE_GS559) are presented as
# mc.KEY_SMOKEALARM in digest(s) so we have to map that too
WELL_KNOWN_TYPE_MAP[mc.KEY_SMOKEALARM] = GS559SubDevice


class MS200SubDevice(MerossSubDevice):
    __slots__ = ("binary_sensor_window",)

    def __init__(self, hub: MerossDeviceHub, p_digest: dict):
        super().__init__(hub, p_digest, mc.TYPE_MS200)
        self.binary_sensor_window = self.build_binary_sensor_c(
            MLBinarySensor.DeviceClass.WINDOW
        )

    async def async_shutdown(self):
        await super().async_shutdown()
        self.binary_sensor_window: MLBinarySensor = None  # type: ignore

    def _set_online(self):
        super()._set_online()
        self.hub._lastupdate_sensor = 0

    def _parse_doorWindow(self, p_doorwindow: dict):
        self.binary_sensor_window.update_onoff(p_doorwindow[mc.KEY_STATUS])


WELL_KNOWN_TYPE_MAP[mc.TYPE_MS200] = MS200SubDevice
# doorWindow devices (mc.TYPE_MS200) are presented as
# mc.KEY_DOORWINDOW in digest(s) so we have to map that too
WELL_KNOWN_TYPE_MAP[mc.KEY_DOORWINDOW] = MS200SubDevice
