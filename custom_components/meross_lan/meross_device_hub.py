from __future__ import annotations

import typing

from . import const as mlc, meross_entity as me
from .binary_sensor import MLBinarySensor
from .calendar import MtsSchedule
from .climate import MtsClimate
from .helpers.namespaces import (
    NamespaceHandler,
    OncePollingStrategy,
    PollingStrategy,
    SmartPollingStrategy,
)
from .meross_device import MerossDevice, MerossDeviceBase
from .merossclient import (
    const as mc,
    get_productnameuuid,
    is_device_online,
    request_get,
)
from .number import MLConfigNumber
from .select import MtsTrackedSensor
from .sensor import (
    MLDiagnosticSensor,
    MLEnumSensor,
    MLHumiditySensor,
    MLNumericSensor,
    MLTemperatureSensor,
)
from .switch import MLSwitch

if typing.TYPE_CHECKING:
    from .devices.mts100 import Mts100Climate
    from .meross_entity import MerossEntity


WELL_KNOWN_TYPE_MAP: dict[str, typing.Callable] = dict(
    {
        # typical entries (they're added on SubDevice declaration)
        # mc.TYPE_MS100: MS100SubDevice,
        # mc.TYPE_MTS100: MTS100SubDevice,
    }
)


class MLHubSensorAdjustNumber(MLConfigNumber):
    namespace = mc.NS_APPLIANCE_HUB_SENSOR_ADJUST
    key_namespace = mc.KEY_ADJUST
    key_channel = mc.KEY_ID

    device_scale = 10

    __slots__ = (
        "native_max_value",
        "native_min_value",
        "native_step",
    )

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
        self.name = f"Adjust {device_class}"
        self.native_min_value = min_value
        self.native_max_value = max_value
        self.native_step = step
        self.native_unit_of_measurement = MLConfigNumber.DEVICECLASS_TO_UNIT_MAP.get(
            device_class
        )
        super().__init__(
            manager,
            manager.id,
            f"config_{self.key_namespace}_{self.key_value}",
            device_class,
        )

    async def async_request(self, device_value):
        # the SET command on NS_APPLIANCE_HUB_SENSOR_ADJUST works by applying
        # the issued value as a 'delta' to the current configured value i.e.
        # 'new adjust value' = 'current adjust value' + 'issued adjust value'
        # Since the native HA interface async_set_native_value wants to set
        # the 'new adjust value' we have to issue the difference against the
        # currently configured one
        return await super().async_request(device_value - self.device_value)


class HubNamespaceHandler(NamespaceHandler):
    """
    This namespace handler must be used to handle all of the Appliance.Hub.xxx namespaces
    since the payload parsing would just be the same where the data are just forwarded to the
    relevant subdevice instance.
    """

    device: typing.Final[MerossDeviceHub]  # type: ignore

    def __init__(self, device: MerossDeviceHub, namespace: str):
        NamespaceHandler.__init__(
            self, device, namespace, handler=self._handle_subdevice
        )

    def _handle_subdevice(self, header, payload):
        """Generalized Hub namespace dispatcher to subdevices"""
        hub = self.device
        subdevices = hub.subdevices
        subdevices_parsed = set()
        for p_subdevice in payload[self.key_namespace]:
            try:
                subdevice_id = p_subdevice[mc.KEY_ID]
                if subdevice_id in subdevices_parsed:
                    hub.log_duplicated_subdevice(subdevice_id)
                else:
                    try:
                        subdevices[subdevice_id]._parse(self.key_namespace, p_subdevice)
                    except KeyError:
                        # force a rescan since we discovered a new subdevice
                        # only if it appears this device is online else it
                        # would be a waste since we wouldnt have enough info
                        # to correctly build that
                        if is_device_online(p_subdevice):
                            self.device.request(request_get(mc.NS_APPLIANCE_SYSTEM_ALL))
                    subdevices_parsed.add(subdevice_id)
            except Exception as exception:
                self.handle_exception(exception, "_handle_subdevice", p_subdevice)


class HubChunkedPollingStrategy(PollingStrategy):
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
        self,
        device: MerossDeviceHub,
        namespace: str,
        types: typing.Collection,
        included: bool,
        count: int,
    ):
        PollingStrategy.__init__(self, device, namespace)
        self._types = types
        self._included = included
        self._count = count

    async def async_poll(self, device: MerossDeviceHub, epoch: float):
        if not (device._mqtt_active and self.lastrequest):
            max_queuable = 1
            # for hubs, this payload request might be splitted
            # in order to query a small amount of devices per iteration
            # see #244 for insights
            for p in self._build_subdevices_payload(device.subdevices.values()):
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
                self.request = (
                    self.namespace,
                    mc.METHOD_GET,
                    {self.key_namespace: p},
                )
                self.adjust_size(len(p))
                if await device.async_request_smartpoll(
                    self,
                    epoch,
                    cloud_queue_max=max_queuable,
                ):
                    max_queuable += 1

    async def async_trace(self, device: MerossDeviceHub, protocol: str | None):
        """
        Used while tracing abilities. In general, we use an euristic 'default'
        query but for some 'well known namespaces' we might be better off querying with
        a better structured payload.
        """
        for p in self._build_subdevices_payload(device.subdevices.values()):
            self.request = (
                self.namespace,
                mc.METHOD_GET,
                {self.key_namespace: p},
            )
            self.adjust_size(len(p))
            await super().async_trace(device, protocol)

    def _build_subdevices_payload(self, subdevices: typing.Collection[MerossSubDevice]):
        """
        This generator helps dealing with hubs hosting an high number
        of subdevices: when queried, the response payload might became huge
        with overflow issues likely on the device side (see #244).
        If this is the case, we'll split the request for fewer
        devices at a time. The count param allows some flexibility depending
        on expected payload size but we might have no clue especially for
        bigger payloads like NS_APPLIANCE_HUB_MTS100_SCHEDULEB
        """
        payload = []
        for subdevice in subdevices:
            if (subdevice.type in self._types) == self._included:
                payload.append({mc.KEY_ID: subdevice.id})
                if len(payload) == self._count:
                    yield payload
                    payload = []
        if payload:
            yield payload


class MerossDeviceHub(MerossDevice):
    """
    Specialized MerossDevice for smart hub(s) like MSH300
    """

    DEFAULT_PLATFORMS = MerossDevice.DEFAULT_PLATFORMS | {
        MLBinarySensor.PLATFORM: None,
        MtsSchedule.PLATFORM: None,
        MLConfigNumber.PLATFORM: None,
        MLNumericSensor.PLATFORM: None,
        MLSwitch.PLATFORM: None,
        MtsClimate.PLATFORM: None,
        MtsTrackedSensor.PLATFORM: None,
    }

    __slots__ = ("subdevices",)

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

    def _create_handler(self, namespace: str):
        match namespace.split("."):
            case (_, "Hub", "SubdeviceList"):
                return NamespaceHandler(
                    self, namespace, handler=self._handle_Appliance_Hub_SubdeviceList
                )
            case (_, "Hub", *_):
                return HubNamespaceHandler(self, namespace)
        return super()._create_handler(namespace)

    def _init_hub(self, digest: dict):
        self.subdevices: dict[object, MerossSubDevice] = {}
        for p_subdevice_digest in digest[mc.KEY_SUBDEVICE]:
            try:
                subdevice_id = p_subdevice_digest[mc.KEY_ID]
                if subdevice_id in self.subdevices:
                    self.log_duplicated_subdevice(subdevice_id)
                else:
                    self._subdevice_build(p_subdevice_digest)
            except Exception as exception:
                self.log_exception(self.WARNING, exception, "_init_hub")

    def _parse_hub(self, p_hub: dict):
        # This is usually called inside _parse_all as part of the digest parsing
        # Here we'll check the fresh subdevice list against the actual one and
        # eventually manage newly added subdevices or removed ones #119
        # telling the caller to persist the changed configuration (self.needsave)
        subdevices_actual = set(self.subdevices.keys())
        for p_subdevice_digest in p_hub[mc.KEY_SUBDEVICE]:
            try:
                subdevice_id = p_subdevice_digest[mc.KEY_ID]
                if subdevice_id in self.subdevices:
                    subdevice = self.subdevices[subdevice_id]
                    if subdevice_id in subdevices_actual:
                        subdevices_actual.remove(subdevice_id)
                    else:  # this shouldnt but happened in a trace (#331)
                        self.log_duplicated_subdevice(subdevice_id)
                elif subdevice := self._subdevice_build(p_subdevice_digest):
                    self.needsave = True
                else:
                    continue
                subdevice.parse_digest(p_subdevice_digest)
            except Exception as exception:
                self.log_exception(self.WARNING, exception, "_parse_hub")

        if subdevices_actual:
            # now we're left with non-existent (removed) subdevices
            self.needsave = True
            for subdevice_id in subdevices_actual:
                subdevice = self.subdevices[subdevice_id]
                self.log(
                    self.WARNING,
                    "Removing subdevice %s (id:%s) - configuration will be reloaded in few sec",
                    subdevice.name,
                    subdevice_id,
                )
            self.schedule_entry_reload()

    # interface: self
    def log_duplicated_subdevice(self, subdevice_id: str):
        self.log(
            self.CRITICAL,
            "Subdevice %s (id:%s) appears twice in device data. Shouldn't happen",
            self.subdevices[subdevice_id].name,
            subdevice_id,
            timeout=604800,  # 1 week
        )

    def _handle_Appliance_Digest_Hub(self, header: dict, payload: dict):
        self._parse_hub(payload[mc.KEY_HUB])

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
                hassdevice = self.get_device_registry().async_get_device(
                    identifiers={(mlc.DOMAIN, p_subdevice[mc.KEY_ID])}
                )
                if not hassdevice:
                    return None
                _type = hassdevice.model
            except Exception:
                return None

        polling_strategies = self.polling_strategies
        abilities = self.descriptor.ability
        if _type in mc.MTS100_ALL_TYPESET:
            if (mc.NS_APPLIANCE_HUB_MTS100_ALL not in polling_strategies) and (
                mc.NS_APPLIANCE_HUB_MTS100_ALL in abilities
            ):
                HubChunkedPollingStrategy(
                    self, mc.NS_APPLIANCE_HUB_MTS100_ALL, mc.MTS100_ALL_TYPESET, True, 8
                )
            if (mc.NS_APPLIANCE_HUB_MTS100_SCHEDULEB not in polling_strategies) and (
                mc.NS_APPLIANCE_HUB_MTS100_SCHEDULEB in abilities
            ):
                HubChunkedPollingStrategy(
                    self,
                    mc.NS_APPLIANCE_HUB_MTS100_SCHEDULEB,
                    mc.MTS100_ALL_TYPESET,
                    True,
                    4,
                )
            if mc.NS_APPLIANCE_HUB_MTS100_ADJUST in polling_strategies:
                polling_strategies[mc.NS_APPLIANCE_HUB_MTS100_ADJUST].increment_size()
            elif mc.NS_APPLIANCE_HUB_MTS100_ADJUST in abilities:
                SmartPollingStrategy(
                    self, mc.NS_APPLIANCE_HUB_MTS100_ADJUST, item_count=1
                )
        else:
            if (mc.NS_APPLIANCE_HUB_SENSOR_ALL not in polling_strategies) and (
                mc.NS_APPLIANCE_HUB_SENSOR_ALL in abilities
            ):
                HubChunkedPollingStrategy(
                    self,
                    mc.NS_APPLIANCE_HUB_SENSOR_ALL,
                    mc.MTS100_ALL_TYPESET,
                    False,
                    8,
                )
            if mc.NS_APPLIANCE_HUB_SENSOR_ADJUST in polling_strategies:
                polling_strategies[mc.NS_APPLIANCE_HUB_SENSOR_ADJUST].increment_size()
            elif mc.NS_APPLIANCE_HUB_SENSOR_ADJUST in abilities:
                SmartPollingStrategy(
                    self, mc.NS_APPLIANCE_HUB_SENSOR_ADJUST, item_count=1
                )
            if (mc.NS_APPLIANCE_HUB_TOGGLEX not in polling_strategies) and (
                mc.NS_APPLIANCE_HUB_TOGGLEX in abilities
            ):
                # this is a status message irrelevant for mts100(s) and
                # other types. If not use an MQTT-PUSH friendly startegy
                if _type not in (mc.TYPE_MS100,):
                    PollingStrategy(self, mc.NS_APPLIANCE_HUB_TOGGLEX)

        if mc.NS_APPLIANCE_HUB_TOGGLEX in polling_strategies:
            polling_strategies[mc.NS_APPLIANCE_HUB_TOGGLEX].increment_size()
        if mc.NS_APPLIANCE_HUB_BATTERY in polling_strategies:
            polling_strategies[mc.NS_APPLIANCE_HUB_BATTERY].increment_size()
        elif mc.NS_APPLIANCE_HUB_BATTERY in abilities:
            SmartPollingStrategy(self, mc.NS_APPLIANCE_HUB_BATTERY, item_count=1)
        if mc.NS_APPLIANCE_HUB_SUBDEVICE_VERSION in polling_strategies:
            polling_strategies[mc.NS_APPLIANCE_HUB_SUBDEVICE_VERSION].increment_size()
        elif mc.NS_APPLIANCE_HUB_SUBDEVICE_VERSION in abilities:
            OncePollingStrategy(
                self, mc.NS_APPLIANCE_HUB_SUBDEVICE_VERSION, item_count=1
            )

        if deviceclass := WELL_KNOWN_TYPE_MAP.get(_type):  # type: ignore
            return deviceclass(self, p_subdevice)
        # build something anyway...
        return MerossSubDevice(self, p_subdevice, _type)  # type: ignore


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
        "build_request",
        "async_request_raw",
        "async_request",
        "check_device_timezone",
        "hub",
        "type",
        "p_digest",
        "sensor_battery",
        "switch_togglex",
    )

    def __init__(self, hub: MerossDeviceHub, p_digest: dict, _type: str):
        # this is a very dirty trick/optimization to override some MerossDeviceBase
        # properties/methods that just needs to be forwarded to the hub
        # this way we're short-circuiting that indirection
        self.build_request = hub.build_request
        self.async_request_raw = hub.async_request_raw
        self.async_request = hub.async_request
        self.check_device_timezone = hub.check_device_timezone
        # these properties are needed to be in place before base class init
        self.hub = hub
        self.type = _type
        self.p_digest = p_digest
        id = p_digest[mc.KEY_ID]
        super().__init__(
            id,
            config_entry_id=hub.config_entry_id,
            logger=hub,
            default_name=get_productnameuuid(_type, id),
            model=_type,
            via_device=next(iter(hub.deviceentry_id["identifiers"])),
        )
        self.platforms = hub.platforms
        hub.subdevices[id] = self
        self.sensor_battery = self.build_sensor_c(MLNumericSensor.DeviceClass.BATTERY)
        # this is a generic toggle we'll setup in case the subdevice
        # 'advertises' it and no specialized implementation is in place
        self.switch_togglex: MLSwitch | None = None

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
        await super().async_shutdown()
        self.check_device_timezone = None  # type: ignore
        self.async_request = None  # type: ignore
        self.async_request_raw = None  # type: ignore
        self.build_request = None  # type: ignore
        self.hub: MerossDeviceHub = None  # type: ignore
        self.sensor_battery: MLNumericSensor = None  # type: ignore
        self.switch_togglex = None

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
        self.hub.polling_strategies[
            (
                mc.NS_APPLIANCE_HUB_MTS100_ALL
                if self.type in mc.MTS100_ALL_TYPESET
                else mc.NS_APPLIANCE_HUB_SENSOR_ALL
            )
        ].lastrequest = 0

    # interface: self
    def build_enum_sensor(self, entitykey: str):
        return MLEnumSensor(self, self.id, entitykey)

    def build_sensor(
        self, entitykey: str, device_class: MLNumericSensor.DeviceClass | None = None
    ):
        return MLNumericSensor(self, self.id, entitykey, device_class)

    def build_sensor_c(self, device_class: MLNumericSensor.DeviceClass):
        return MLNumericSensor(self, self.id, str(device_class), device_class)

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
            # so we'll (entually) euristically generate sensors for device properties
            # This is the case for when we see newer devices and we don't know
            # their payloads and features.
            # as for now we've seen "smokeAlarm" and "doorWindow" subdevices
            # carrying similar payloads structures. We'll be conservative
            # by not 'exploiting' lists in payloads since they usually carry
            # historic data or so
            if not self.hub.create_diagnostic_entities:
                return

            def _parse_dict(parent_key: str, parent_dict: dict):
                for subkey, subvalue in parent_dict.items():
                    if isinstance(subvalue, dict):
                        _parse_dict(f"{parent_key}_{subkey}", subvalue)
                        continue
                    if isinstance(subvalue, list):
                        _parse_list()
                        continue
                    if subkey in {
                        mc.KEY_ID,
                        mc.KEY_LMTIME,
                        mc.KEY_LMTIME_,
                        mc.KEY_SYNCEDTIME,
                        mc.KEY_LATESTSAMPLETIME,
                    }:
                        continue
                    entitykey = f"{parent_key}_{subkey}"
                    try:
                        self.entities[f"{self.id}_{entitykey}"].update_native_value(
                            subvalue
                        )
                    except KeyError:
                        MLDiagnosticSensor(
                            self,
                            self.id,
                            entitykey,
                            native_value=subvalue,
                        )

            def _parse_list():
                pass

            _parse_dict(key, payload)

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
            if mc.KEY_ONOFF in p_digest:
                self._parse_togglex(p_digest)

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
            number: MLHubSensorAdjustNumber | None
            if number := getattr(self, f"number_adjust_{p_key}", None):
                number.update_device_value(p_value)

    def _parse_battery(self, p_battery: dict):
        if self._online:
            self.sensor_battery.update_native_value(p_battery[mc.KEY_VALUE])

    def _parse_exception(self, p_exception: dict):
        """{"id": "00000000", "code": 5061}"""
        self.log(self.WARNING, "Received exception payload: %s", str(p_exception))

    def _parse_online(self, p_online: dict):
        if mc.KEY_STATUS in p_online:
            if p_online[mc.KEY_STATUS] == mc.STATUS_ONLINE:
                if not self._online:
                    self._set_online()
            else:
                if self._online:
                    self._set_offline()

    def _parse_togglex(self, p_togglex: dict):
        """{"id": "00000000", "onoff": 0, ...}"""
        # might come from parse_digest or from Appliance.Hub.ToggleX
        # in any case we're just interested to the "onoff" key
        if switch_togglex := self.switch_togglex:
            switch_togglex.update_onoff(p_togglex[mc.KEY_ONOFF])
        else:
            self.switch_togglex = switch_togglex = MLSwitch(
                self,
                self.id,
                None,
                MLSwitch.DeviceClass.SWITCH,
                onoff=p_togglex[mc.KEY_ONOFF],
                namespace=mc.NS_APPLIANCE_HUB_TOGGLEX,
            )
            switch_togglex.entity_category = me.EntityCategory.CONFIG
            switch_togglex.key_channel = mc.KEY_ID

    def _parse_version(self, p_version: dict):
        """{"id": "00000000", "hardware": "1.1.5", "firmware": "5.1.8"}"""
        if device_registry_entry := self.device_registry_entry:
            kwargs = {}
            if mc.KEY_HARDWARE in p_version:
                hw_version = p_version[mc.KEY_HARDWARE]
                if hw_version != device_registry_entry.hw_version:
                    kwargs["hw_version"] = hw_version
            if mc.KEY_FIRMWARE in p_version:
                sw_version = p_version[mc.KEY_FIRMWARE]
                if sw_version != device_registry_entry.sw_version:
                    kwargs["sw_version"] = sw_version
            if kwargs:
                self.get_device_registry().async_update_device(
                    device_registry_entry.id, **kwargs
                )


class MS100SubDevice(MerossSubDevice):
    __slots__ = (
        "sensor_temperature",
        "sensor_humidity",
        "number_adjust_temperature",
        "number_adjust_humidity",
    )

    def __init__(self, hub: MerossDeviceHub, p_digest: dict):
        super().__init__(hub, p_digest, mc.TYPE_MS100)
        self.sensor_temperature = MLTemperatureSensor(self, self.id)
        self.sensor_humidity = MLHumiditySensor(self, self.id)
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
        self.sensor_temperature: MLNumericSensor = None  # type: ignore
        self.sensor_humidity: MLNumericSensor = None  # type: ignore
        self.number_adjust_temperature: MLHubSensorAdjustNumber = None  # type: ignore
        self.number_adjust_humidity: MLHubSensorAdjustNumber = None  # type: ignore

    def _parse_humidity(self, p_humidity: dict):
        if mc.KEY_LATEST in p_humidity:
            self._update_sensor(self.sensor_humidity, p_humidity[mc.KEY_LATEST])

    def _parse_ms100(self, p_ms100: dict):
        # typically called by MerossSubDevice.parse_digest
        # when parsing Appliance.System.All
        self._parse_tempHum(p_ms100)

    def _parse_temperature(self, p_temperature: dict):
        if mc.KEY_LATEST in p_temperature:
            self._update_sensor(self.sensor_temperature, p_temperature[mc.KEY_LATEST])

    def _parse_tempHum(self, p_temphum: dict):
        if mc.KEY_LATESTTEMPERATURE in p_temphum:
            self._update_sensor(
                self.sensor_temperature, p_temphum[mc.KEY_LATESTTEMPERATURE]
            )
        if mc.KEY_LATESTHUMIDITY in p_temphum:
            self._update_sensor(self.sensor_humidity, p_temphum[mc.KEY_LATESTHUMIDITY])

    def _parse_togglex(self, p_togglex: dict):
        # avoid the base class creating a toggle entity
        # since we're pretty sure ms100 doesn't have one
        pass

    def _update_sensor(self, sensor: MLNumericSensor, device_value):
        # when a temp/hum reading changes we're smartly requesting
        # the adjust sooner than scheduled in case the change
        # was due to an adjustment
        if sensor.update_native_value(device_value / 10):
            strategy = self.hub.polling_strategies[mc.NS_APPLIANCE_HUB_SENSOR_ADJUST]
            if strategy.lastrequest < (self.hub.lastresponse - 30):
                strategy.lastrequest = 0


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
        self.sensor_temperature = MLTemperatureSensor(self, self.id)
        self.sensor_temperature.entity_registry_enabled_default = False

    async def async_shutdown(self):
        await super().async_shutdown()
        self.climate: Mts100Climate = None  # type: ignore
        self.sensor_temperature: MLNumericSensor = None  # type: ignore

    def _parse_all(self, p_all: dict):
        self._parse_online(p_all.get(mc.KEY_ONLINE, {}))

        climate = self.climate

        if mc.KEY_SCHEDULEBMODE in p_all:
            climate.update_scheduleb_mode(p_all[mc.KEY_SCHEDULEBMODE])

        if isinstance(p_mode := p_all.get(mc.KEY_MODE), dict):
            climate._mts_mode = p_mode[mc.KEY_STATE]

        if isinstance(p_togglex := p_all.get(mc.KEY_TOGGLEX), dict):
            climate._mts_onoff = p_togglex[mc.KEY_ONOFF]

        if isinstance(p_temperature := p_all.get(mc.KEY_TEMPERATURE), dict):
            climate._parse(p_temperature)
        else:
            climate.flush_state()

    def _parse_adjust(self, p_adjust: dict):
        self.climate.number_adjust_temperature.update_device_value(
            p_adjust[mc.KEY_TEMPERATURE]
        )

    def _parse_mode(self, p_mode: dict):
        climate = self.climate
        climate._mts_mode = p_mode[mc.KEY_STATE]
        climate.flush_state()

    def _parse_mts100(self, p_mts100: dict):
        # typically called by MerossSubDevice.parse_digest
        # when parsing Appliance.System.All
        pass

    def _parse_schedule(self, p_schedule: dict):
        self.climate.schedule._parse(p_schedule)

    def _parse_temperature(self, p_temperature: dict):
        self.climate._parse(p_temperature)

    def _parse_togglex(self, p_togglex: dict):
        climate = self.climate
        climate._mts_onoff = p_togglex[mc.KEY_ONOFF]
        climate.flush_state()


WELL_KNOWN_TYPE_MAP[mc.TYPE_MTS100] = MTS100SubDevice


class MTS100V3SubDevice(MTS100SubDevice):
    def __init__(self, hub: MerossDeviceHub, p_digest: dict):
        super().__init__(hub, p_digest, mc.TYPE_MTS100V3)

    def _parse_mts100v3(self, p_mts100v3: dict):
        # typically called by MerossSubDevice.parse_digest
        # when parsing Appliance.System.All
        pass


WELL_KNOWN_TYPE_MAP[mc.TYPE_MTS100V3] = MTS100V3SubDevice


class MTS150SubDevice(MTS100SubDevice):
    def __init__(self, hub: MerossDeviceHub, p_digest: dict):
        super().__init__(hub, p_digest, mc.TYPE_MTS150)

    def _parse_mts150(self, p_mts150: dict):
        """{"mode": 3,"currentSet": 60,"updateMode": 3,"updateTemp": 175,
        "motorCurLocation": 0,"motorStartCtr": 883,"motorTotalPath": 69852}"""
        # typically called by MerossSubDevice.parse_digest
        # when parsing Appliance.System.All
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
        self.sensor_status: MLEnumSensor = self.build_enum_sensor(mc.KEY_STATUS)
        self.sensor_status.translation_key = "smoke_alarm_status"
        self.sensor_interConn: MLEnumSensor = self.build_enum_sensor(mc.KEY_INTERCONN)
        self.binary_sensor_alarm: MLBinarySensor = self.build_binary_sensor(
            "alarm", MLBinarySensor.DeviceClass.SAFETY
        )
        self.binary_sensor_error: MLBinarySensor = self.build_binary_sensor(
            "error", MLBinarySensor.DeviceClass.PROBLEM
        )
        self.binary_sensor_muted: MLBinarySensor = self.build_binary_sensor("muted")

    async def async_shutdown(self):
        await super().async_shutdown()
        self.binary_sensor_muted = None  # type: ignore
        self.binary_sensor_error = None  # type: ignore
        self.binary_sensor_alarm = None  # type: ignore
        self.sensor_status = None  # type: ignore
        self.sensor_interConn = None  # type: ignore

    def _parse_smokeAlarm(self, p_smokealarm: dict):
        if isinstance(value := p_smokealarm.get(mc.KEY_STATUS), int):
            self.binary_sensor_alarm.update_onoff(value in GS559SubDevice.STATUS_ALARM)
            self.binary_sensor_error.update_onoff(value in GS559SubDevice.STATUS_ERROR)
            self.binary_sensor_muted.update_onoff(value in GS559SubDevice.STATUS_MUTED)
            self.sensor_status.update_native_value(
                GS559SubDevice.STATUS_MAP.get(value, value)
            )
        if isinstance(value := p_smokealarm.get(mc.KEY_INTERCONN), int):
            self.sensor_interConn.update_native_value(value)


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
