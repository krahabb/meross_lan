from __future__ import annotations

import typing

from homeassistant.helpers import device_registry

from . import meross_entity as me
from .binary_sensor import MLBinarySensor
from .calendar import MLCalendar
from .climate import MtsClimate
from .const import DOMAIN, PARAM_HUBBATTERY_UPDATE_PERIOD
from .helpers import ApiProfile, PollingStrategy, SmartPollingStrategy
from .meross_device import MerossDevice, MerossDeviceBase
from .merossclient import (  # mEROSS cONST
    const as mc,
    get_default_arguments,
    get_namespacekey,
    get_productnameuuid,
    is_device_online,
)
from .number import MLConfigNumber
from .select import MtsTrackedSensor
from .sensor import MLSensor
from .switch import MLSwitch

if typing.TYPE_CHECKING:
    from .devices.mts100 import Mts100Climate
    from .meross_device import MerossPayloadType, ResponseCallbackType
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


class MLHubSensorAdjustNumber(MLConfigNumber):
    namespace = mc.NS_APPLIANCE_HUB_SENSOR_ADJUST
    key_namespace = mc.KEY_ADJUST
    key_channel = mc.KEY_ID

    def __init__(
        self,
        manager: MerossSubDevice,
        key: str,
        device_class: MLConfigNumber.DeviceClass,
        min_value: float,
        max_value: float,
        step: float,
    ):
        self.key_value = key
        self._attr_native_min_value = min_value
        self._attr_native_max_value = max_value
        self._attr_native_step = step
        self._attr_native_unit_of_measurement = (
            MLConfigNumber.DEVICECLASS_TO_UNIT_MAP.get(device_class)
        )
        self._attr_name = f"Adjust {device_class}"
        super().__init__(
            manager,
            manager.id,
            f"config_{self.key_namespace}_{self.key_value}",
            device_class,
        )

    @property
    def device_scale(self):
        return 10

    async def async_request(self, device_value):
        # the SET command on NS_APPLIANCE_HUB_SENSOR_ADJUST works by applying
        # the issued value as a 'delta' to the current configured value i.e.
        # 'new adjust value' = 'current adjust value' + 'issued adjust value'
        # Since the native HA interface async_set_native_value wants to set
        # the 'new adjust value' we have to issue the difference against the
        # currently configured one
        return await super().async_request(device_value - self.device_value)


class SubDevicePollingStrategy(PollingStrategy):
    """
    This is a strategy for polling (general) subdevices state with special care for messages
    possibly generating huge payloads (see #244). We should avoid this
    poll when the device is MQTT pushing its state
    """

    __slots__ = (
        "_types",
        "_included",
        "_count",
    )

    def __init__(
        self, namespace: str, types: typing.Collection, included: bool, count: int
    ):
        super().__init__(namespace)
        self._types = types
        self._included = included
        self._count = count

    async def poll(self, device: MerossDeviceHub, epoch: float, namespace: str | None):
        if namespace or (not device._mqtt_active) or (self.lastrequest == 0):
            max_queuable = 1
            # for hubs, this payload request might be splitted
            # in order to query a small amount of devices per iteration
            # see #244 for insights
            for p in device._build_subdevices_payload(
                self._types, self._included, self._count
            ):
                # in case we're going through cloud mqtt
                # async_request_smartpoll would check how many
                # polls are standing in queue in order to
                # not burst the meross mqtt. We want to
                # send these requests (in loop) as a whole
                # so, we start with max_queuable == 1 in order
                # to avoid starting when something is already
                # sent in the current poll cycle but then,
                # if we're good to go on the first iteration,
                # we don't want to break this cycle else it
                # would restart (stateless) at the next polling cycle
                if await device.async_request_smartpoll(
                    epoch,
                    self.lastrequest,
                    (
                        self.namespace,
                        mc.METHOD_GET,
                        {get_namespacekey(self.namespace): p},
                    ),
                    cloud_queue_max=max_queuable,
                ):
                    max_queuable = max_queuable + 1

            if max_queuable > 1:
                self.lastrequest = epoch


class MerossDeviceHub(MerossDevice):
    """
    Specialized MerossDevice for smart hub(s) like MSH300
    """

    __slots__ = ("subdevices",)

    def __init__(self, descriptor, entry):
        self.subdevices: dict[object, MerossSubDevice] = {}
        super().__init__(descriptor, entry)
        # invoke platform(s) async_setup_entry
        # in order to be able to eventually add entities when they 'pop up'
        # in the hub (see also self.async_add_sensors)
        self.platforms[MLSensor.PLATFORM] = None
        self.platforms[MLBinarySensor.PLATFORM] = None
        self.platforms[MtsClimate.PLATFORM] = None
        self.platforms[MtsTrackedSensor.PLATFORM] = None
        self.platforms[MLHubSensorAdjustNumber.PLATFORM] = None
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

    # interface: self
    def _handle_Appliance_Digest_Hub(self, header: dict, payload: dict):
        self._parse_hub(payload[mc.KEY_HUB])

    def _handle_Appliance_Hub_Sensor_Adjust(self, header: dict, payload: dict):
        self._subdevice_parse(mc.KEY_ADJUST, payload)

    def _handle_Appliance_Hub_Sensor_All(self, header: dict, payload: dict):
        self._subdevice_parse(mc.KEY_ALL, payload)

    def _handle_Appliance_Hub_Sensor_DoorWindow(self, header: dict, payload: dict):
        self._subdevice_parse(mc.KEY_DOORWINDOW, payload)

    def _handle_Appliance_Hub_Sensor_Smoke(self, header: dict, payload: dict):
        self._subdevice_parse(mc.KEY_SMOKEALARM, payload)

    def _handle_Appliance_Hub_Sensor_TempHum(self, header: dict, payload: dict):
        self._subdevice_parse(mc.KEY_TEMPHUM, payload)

    def _handle_Appliance_Hub_Sensor_WaterLeak(self, header: dict, payload: dict):
        self._subdevice_parse(mc.KEY_WATERLEAK, payload)

    def _handle_Appliance_Hub_Mts100_Adjust(self, header: dict, payload: dict):
        self._subdevice_parse(mc.KEY_ADJUST, payload)

    def _handle_Appliance_Hub_Mts100_All(self, header: dict, payload: dict):
        self._subdevice_parse(mc.KEY_ALL, payload)

    def _handle_Appliance_Hub_Mts100_Mode(self, header: dict, payload: dict):
        self._subdevice_parse(mc.KEY_MODE, payload)

    def _handle_Appliance_Hub_Mts100_ScheduleB(self, header: dict, payload: dict):
        self._subdevice_parse(mc.KEY_SCHEDULE, payload)

    def _handle_Appliance_Hub_Mts100_Temperature(self, header: dict, payload: dict):
        self._subdevice_parse(mc.KEY_TEMPERATURE, payload)

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

    def _handle_Appliance_Hub_ToggleX(self, header: dict, payload: dict):
        self._subdevice_parse(mc.KEY_TOGGLEX, payload)

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
                    "removing subdevice %s(%s) - configuration will be reloaded in few sec",
                    subdevice.name,
                    p_id,
                )
            self.schedule_entry_reload()

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

        abilities = self.descriptor.ability
        polling_dictionary = self.polling_dictionary
        if _type in MTS100_ALL_TYPESET:
            if (mc.NS_APPLIANCE_HUB_MTS100_ADJUST in abilities) and not (
                mc.NS_APPLIANCE_HUB_MTS100_ADJUST in polling_dictionary
            ):
                polling_dictionary[
                    mc.NS_APPLIANCE_HUB_MTS100_ADJUST
                ] = SmartPollingStrategy(mc.NS_APPLIANCE_HUB_MTS100_ADJUST)
            if (mc.NS_APPLIANCE_HUB_MTS100_ALL in abilities) and not (
                mc.NS_APPLIANCE_HUB_MTS100_ALL in polling_dictionary
            ):
                polling_dictionary[
                    mc.NS_APPLIANCE_HUB_MTS100_ALL
                ] = SubDevicePollingStrategy(
                    mc.NS_APPLIANCE_HUB_MTS100_ALL, MTS100_ALL_TYPESET, True, 8
                )
            if (mc.NS_APPLIANCE_HUB_MTS100_SCHEDULEB in abilities) and not (
                mc.NS_APPLIANCE_HUB_MTS100_SCHEDULEB in polling_dictionary
            ):
                polling_dictionary[
                    mc.NS_APPLIANCE_HUB_MTS100_SCHEDULEB
                ] = SubDevicePollingStrategy(
                    mc.NS_APPLIANCE_HUB_MTS100_SCHEDULEB, MTS100_ALL_TYPESET, True, 4
                )
        else:
            if (mc.NS_APPLIANCE_HUB_SENSOR_ADJUST in abilities) and not (
                mc.NS_APPLIANCE_HUB_SENSOR_ADJUST in polling_dictionary
            ):
                polling_dictionary[
                    mc.NS_APPLIANCE_HUB_SENSOR_ADJUST
                ] = SmartPollingStrategy(mc.NS_APPLIANCE_HUB_SENSOR_ADJUST)
            if (mc.NS_APPLIANCE_HUB_SENSOR_ALL in abilities) and not (
                mc.NS_APPLIANCE_HUB_SENSOR_ALL in polling_dictionary
            ):
                polling_dictionary[
                    mc.NS_APPLIANCE_HUB_SENSOR_ALL
                ] = SubDevicePollingStrategy(
                    mc.NS_APPLIANCE_HUB_SENSOR_ALL, MTS100_ALL_TYPESET, False, 8
                )
            if (mc.NS_APPLIANCE_HUB_TOGGLEX in abilities) and not (
                mc.NS_APPLIANCE_HUB_TOGGLEX in polling_dictionary
            ):
                # this is a status message irrelevant for mts100(s) and
                # other types. If not use an MQTT-PUSH friendly startegy
                if _type not in (mc.TYPE_MS100,):
                    polling_dictionary[mc.NS_APPLIANCE_HUB_TOGGLEX] = PollingStrategy(
                        mc.NS_APPLIANCE_HUB_TOGGLEX
                    )

        if deviceclass := WELL_KNOWN_TYPE_MAP.get(_type):  # type: ignore
            return deviceclass(self, p_subdevice)
        # build something anyway...
        return MerossSubDevice(self, p_subdevice, _type)  # type: ignore

    def _subdevice_parse(self, key: str, payload: MerossPayloadType):
        for p_subdevice in payload[key]:
            if subdevice := self.subdevices.get(p_subdevice[mc.KEY_ID]):
                subdevice._parse(key, p_subdevice)
            else:
                # force a rescan since we discovered a new subdevice
                # only if it appears this device is online else it
                # would be a waste since we wouldnt have enough info
                # to correctly build that
                if is_device_online(p_subdevice):
                    self.request(*get_default_arguments(mc.NS_APPLIANCE_SYSTEM_ALL))

    def _build_subdevices_payload(
        self, subdevice_types: typing.Collection, included: bool, count: int
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
                if (subdevice.type in subdevice_types) == included:
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
        self.hub = hub
        self.type = _type
        self.p_digest = p_digest
        # base init after setting some key properties needed for logging
        super().__init__(
            id_,
            hub.config_entry_id,
            default_name=get_productnameuuid(_type, id_),
            model=_type,
            via_device=next(iter(hub.deviceentry_id["identifiers"])),
        )
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
        return await self.hub.async_request(
            namespace, method, payload, response_callback
        )

    @property
    def tz(self):
        return self.hub.tz

    def _get_device_info_name_key(self) -> str:
        return mc.KEY_SUBDEVICENAME

    def _get_internal_name(self) -> str:
        return get_productnameuuid(self.type, self.id)

    def _set_online(self):
        super()._set_online()
        # force a re-poll even on MQTT
        _strategy = self.hub.polling_dictionary.get(
            mc.NS_APPLIANCE_HUB_MTS100_ALL
            if self.type in MTS100_ALL_TYPESET
            else mc.NS_APPLIANCE_HUB_SENSOR_ALL
        )
        if _strategy:
            _strategy.lastrequest = 0

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

    def build_binary_sensor_window(self):
        return MLBinarySensor(
            self,
            self.id,
            str(MLBinarySensor.DeviceClass.WINDOW),
            MLBinarySensor.DeviceClass.WINDOW,
        )

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
            # as for now we've seen "smokeAlarm" and "doorWindow" subdevices
            # carrying similar payloads structures. We'll be conservative
            # and generate generic sensors for any key carrying a non structured
            # type (dict or list), except "lmTime" and few others known one
            for subkey, subvalue in payload.items():
                if (
                    subkey
                    in {
                        mc.KEY_ID,
                        mc.KEY_LMTIME,
                        mc.KEY_LMTIME_,
                        mc.KEY_SYNCEDTIME,
                        mc.KEY_LATESTSAMPLETIME,
                    }
                    or isinstance(subvalue, list)
                    or isinstance(subvalue, dict)
                ):
                    continue
                entitykey = f"{key}_{subkey}"
                sensor = self.entities.get(f"{self.id}_{entitykey}")
                if not sensor:
                    sensor = self.build_sensor(entitykey)
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
            number: MLHubSensorAdjustNumber
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
        self.number_adjust_temperature = MLHubSensorAdjustNumber(
            self,
            mc.KEY_TEMPERATURE,
            MLHubSensorAdjustNumber.DeviceClass.TEMPERATURE,
            -5,
            5,
            0.1,
        )
        self.number_adjust_humidity = MLHubSensorAdjustNumber(
            self,
            mc.KEY_HUMIDITY,
            MLHubSensorAdjustNumber.DeviceClass.HUMIDITY,
            -20,
            20,
            1,
        )

    async def async_shutdown(self):
        await super().async_shutdown()
        self.sensor_temperature: MLSensor = None  # type: ignore
        self.sensor_humidity: MLSensor = None  # type: ignore
        self.number_adjust_temperature: MLHubSensorAdjustNumber = None  # type: ignore
        self.number_adjust_humidity: MLHubSensorAdjustNumber = None  # type: ignore

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
# there's a new temp/hum sensor in town (MS100FH - see #303)
# and it is likely presented as tempHum in digest
# (need confirmation from device tracing though)
WELL_KNOWN_TYPE_MAP[mc.KEY_TEMPHUM] = MS100SubDevice


class MTS100SubDevice(MerossSubDevice):
    __slots__ = (
        "climate",
        "sensor_temperature",
    )

    def __init__(
        self, hub: MerossDeviceHub, p_digest: dict, _type: str = mc.TYPE_MTS100
    ):
        super().__init__(hub, p_digest, _type)
        from .devices.mts100 import Mts100Climate

        self.climate = Mts100Climate(self)
        self.sensor_temperature = self.build_sensor_c(MLSensor.DeviceClass.TEMPERATURE)
        self.sensor_temperature._attr_entity_registry_enabled_default = False

    async def async_shutdown(self):
        await super().async_shutdown()
        self.climate: Mts100Climate = None  # type: ignore
        self.sensor_temperature: MLSensor = None  # type: ignore

    def _parse_all(self, p_all: dict):
        self._parse_online(p_all.get(mc.KEY_ONLINE, {}))

        climate = self.climate

        climate.scheduleBMode = p_all.get(mc.KEY_SCHEDULEBMODE)

        if isinstance(p_mode := p_all.get(mc.KEY_MODE), dict):
            climate._mts_mode = p_mode.get(mc.KEY_STATE)

        if isinstance(p_togglex := p_all.get(mc.KEY_TOGGLEX), dict):
            climate._mts_onoff = p_togglex.get(mc.KEY_ONOFF)

        if isinstance(p_temperature := p_all.get(mc.KEY_TEMPERATURE), dict):
            climate._parse_temperature(p_temperature)
        else:
            climate.update_mts_state()

    def _parse_adjust(self, p_adjust: dict):
        self.climate.number_adjust_temperature.update_native_value(
            p_adjust[mc.KEY_TEMPERATURE]
        )

    def _parse_mode(self, p_mode: dict):
        climate = self.climate
        climate._mts_mode = p_mode.get(mc.KEY_STATE)
        climate.update_mts_state()

    def _parse_mts100(self, p_mts100: dict):
        pass

    def _parse_schedule(self, p_schedule: dict):
        self.climate.schedule._parse_schedule(p_schedule)

    def _parse_temperature(self, p_temperature: dict):
        self.climate._parse_temperature(p_temperature)

    def _parse_togglex(self, p_togglex: dict):
        climate = self.climate
        climate._mts_onoff = p_togglex.get(mc.KEY_ONOFF)
        climate.update_mts_state()


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
        self.binary_sensor_alarm = self.build_binary_sensor(
            "alarm", MLBinarySensor.DeviceClass.SAFETY
        )
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
        self.binary_sensor_window = self.build_binary_sensor_window()

    async def async_shutdown(self):
        await super().async_shutdown()
        self.binary_sensor_window: MLBinarySensor = None  # type: ignore

    def _parse_doorWindow(self, p_doorwindow: dict):
        self.binary_sensor_window.update_onoff(p_doorwindow[mc.KEY_STATUS])


WELL_KNOWN_TYPE_MAP[mc.TYPE_MS200] = MS200SubDevice
# doorWindow devices (mc.TYPE_MS200) are presented as
# mc.KEY_DOORWINDOW in digest(s) so we have to map that too
WELL_KNOWN_TYPE_MAP[mc.KEY_DOORWINDOW] = MS200SubDevice


class MS400SubDevice(MerossSubDevice):
    __slots__ = ("binary_sensor_waterleak",)

    def __init__(self, hub: MerossDeviceHub, p_digest: dict):
        super().__init__(hub, p_digest, mc.TYPE_MS400)
        self.binary_sensor_waterleak = self.build_binary_sensor(
            mc.KEY_WATERLEAK, MLBinarySensor.DeviceClass.SAFETY
        )

    async def async_shutdown(self):
        await super().async_shutdown()
        self.binary_sensor_waterleak: MLBinarySensor = None  # type: ignore

    def _parse_waterLeak(self, p_waterleak: dict):
        self.binary_sensor_waterleak.update_onoff(p_waterleak[mc.KEY_LATESTWATERLEAK])


WELL_KNOWN_TYPE_MAP[mc.TYPE_MS400] = MS400SubDevice
# waterLeak devices (mc.TYPE_MS400) are presented as
# mc.KEY_WATERLEAK in digest(s) so we have to map that too
WELL_KNOWN_TYPE_MAP[mc.KEY_WATERLEAK] = MS400SubDevice
