from typing import TYPE_CHECKING

from .. import const as mlc
from ..binary_sensor import MLBinarySensor
from ..calendar import MtsSchedule
from ..climate import MtsClimate
from ..helpers import entity as me
from ..helpers.device import BaseDevice, Device
from ..helpers.namespaces import NamespaceHandler, NamespaceParser
from ..merossclient import const as mc, get_productnameuuid, namespaces as mn
from ..number import MLConfigNumber
from ..select import MtsTrackedSensor
from ..sensor import (
    MLDiagnosticSensor,
    MLEnumSensor,
    MLHumiditySensor,
    MLLightSensor,
    MLNumericSensor,
    MLTemperatureSensor,
)
from ..switch import MLSwitch

if TYPE_CHECKING:
    from typing import Any, Callable, Collection, Final
    from ..helpers.device import AsyncRequestFunc, DigestInitReturnType
    from ..helpers.entity import MLEntity
    from ..merossclient.cloudapi import SubDeviceInfoType
    from .mts100 import Mts100Climate

    WELL_KNOWN_TYPE_MAP: Final[dict[str, Callable]]

WELL_KNOWN_TYPE_MAP = dict(
    {
        # typical entries (they're added on SubDevice declaration)
        # mc.TYPE_MS100: MS100SubDevice,
        # mc.TYPE_MTS100: MTS100SubDevice,
    }
)


class MLHubSensorAdjustNumber(MLConfigNumber):
    ns = mn.Appliance_Hub_Sensor_Adjust

    __slots__ = (
        "native_max_value",
        "native_min_value",
        "native_step",
    )

    def __init__(
        self,
        manager: "SubDevice",
        key: str,
        device_class: MLConfigNumber.DeviceClass,
        min_value: float,
        max_value: float,
        step: float,
    ):
        self.key_value = key
        self.native_min_value = min_value
        self.native_max_value = max_value
        self.native_step = step
        super().__init__(
            manager,
            manager.id,
            f"config_{self.ns.key}_{self.key_value}",
            device_class,
            device_scale=10,
            name=f"Adjust {device_class}",
        )

    async def async_request_value(self, device_value):
        # the SET command on NS_APPLIANCE_HUB_SENSOR_ADJUST works by applying
        # the issued value as a 'delta' to the current configured value i.e.
        # 'new adjust value' = 'current adjust value' + 'issued adjust value'
        # Since the native HA interface async_set_native_value wants to set
        # the 'new adjust value' we have to issue the difference against the
        # currently configured one
        return await super().async_request_value(device_value - self.device_value)


class MLHubToggle(me.MEListChannelMixin, MLSwitch):
    ns = mn.Appliance_Hub_ToggleX

    # HA core entity attributes:
    entity_category = MLSwitch.EntityCategory.CONFIG


class HubNamespaceHandler(NamespaceHandler):
    """
    This namespace handler must be used to handle all of the Appliance.Hub.xxx namespaces
    since the payload parsing would just be the same where the data are just forwarded to the
    relevant subdevice instance. (TODO) This class could/should be removed in favor of the base class
    indexed parsing but this will need some work...
    """

    device: "HubMixin"

    def __init__(self, device: "HubMixin", ns: "mn.Namespace"):
        NamespaceHandler.__init__(self, device, ns, handler=self._handle_subdevice)

    def _handle_subdevice(self, header, payload):
        """Generalized Hub namespace dispatcher to subdevices"""
        hub = self.device
        subdevices = hub.subdevices
        subdevices_parsed = set()
        key_namespace = self.ns.key
        key_channel = self.key_channel
        for p_subdevice in payload[key_namespace]:
            try:
                subdevice_id = p_subdevice[key_channel]
                if subdevice_id in subdevices_parsed:
                    hub.log_duplicated_subdevice(subdevice_id)
                else:
                    try:
                        subdevices[subdevice_id]._hub_parse(key_namespace, p_subdevice)
                    except KeyError:
                        # force a rescan since we discovered a new subdevice
                        hub.namespace_handlers[
                            mn.Appliance_System_All.name
                        ].polling_epoch_next = 0.0
                    subdevices_parsed.add(subdevice_id)
            except TypeError:
                # This could happen when the main payload is not a list of subdevices
                # and might indicate this namespace is likely devoted to general hub
                # commands/info (something like Appliance.Hub.*)
                self.handler = self._handle_undefined
                self._handle_undefined(header, payload)
            except Exception as exception:
                self.handle_exception(exception, "_handle_subdevice", p_subdevice)


class HubChunkedNamespaceHandler(HubNamespaceHandler):
    """
    This is a strategy for polling (general) subdevices state with special care for messages
    possibly generating huge payloads (see #244).
    The strategy itself will poll the namespace on every cycle if no MQTT active
    When MQTT active we rely on states PUSHES in general but we'll also poll
    from time to time (see POLLING_STRATEGY_CONF for the relevant namespaces)
    """

    __slots__ = (
        "_models",
        "_included",
        "_count",
    )

    def __init__(
        self,
        device: "HubMixin",
        ns: "mn.Namespace",
        models: "Collection",
        included: bool,
        count: int,
    ):
        HubNamespaceHandler.__init__(self, device, ns)
        self._models = models
        self._included = included
        self._count = count
        self.polling_strategy = HubChunkedNamespaceHandler.async_poll_chunked  # type: ignore

    async def async_poll_chunked(self):
        device = self.device
        if (not device._mqtt_active) or (
            device._polling_epoch >= self.polling_epoch_next
        ):
            max_queuable = 1
            # for hubs, this payload request might be splitted
            # in order to query a small amount of devices per iteration
            # see #244 for insights
            for p in self._build_subdevices_payload():
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
                self.polling_request_set(p)
                if await device.async_request_smartpoll(
                    self,
                    cloud_queue_max=max_queuable,
                ):
                    max_queuable += 1

    async def async_trace(self, async_request_func: "AsyncRequestFunc"):
        for p in self._build_subdevices_payload():
            self.polling_request_set(p)
            await HubNamespaceHandler.async_trace(self, async_request_func)

    def _build_subdevices_payload(self):
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
        key_channel = self.key_channel
        for subdevice in self.device.subdevices.values():
            if (subdevice.model in self._models) == self._included:
                payload.append({key_channel: subdevice.id})
                if len(payload) == self._count:
                    yield payload
                    payload = []
        if payload:
            yield payload


class HubMixin(Device if TYPE_CHECKING else object):
    """
    Specialized Device for smart hub(s) like MSH300
    """

    NAMESPACES = mn.HUB_NAMESPACES

    DEFAULT_PLATFORMS = Device.DEFAULT_PLATFORMS | {
        MLBinarySensor.PLATFORM: None,
        MtsSchedule.PLATFORM: None,
        MLConfigNumber.PLATFORM: None,
        MLNumericSensor.PLATFORM: None,
        MLSwitch.PLATFORM: None,
        MtsClimate.PLATFORM: None,
        MtsTrackedSensor.PLATFORM: None,
    }

    # interface: EntityManager
    def managed_entities(self, platform):
        entities = super().managed_entities(platform)
        for subdevice in self.subdevices.values():
            entities.extend(subdevice.managed_entities(platform))
        return entities

    # interface: Device
    async def async_shutdown(self):
        await super().async_shutdown()
        for subdevice in self.subdevices.values():
            await subdevice.async_shutdown()
        self.subdevices.clear()

    def _set_offline(self):
        for subdevice in self.subdevices.values():
            subdevice._set_offline()
        super()._set_offline()

    def get_type(self) -> mlc.DeviceType:
        return mlc.DeviceType.HUB

    def _create_handler(self, ns: "mn.Namespace"):
        _handler = getattr(self, f"_handle_{ns.name.replace('.', '_')}", None)
        if _handler:
            return NamespaceHandler(
                self,
                ns,
                handler=_handler,
            )
        elif ns.is_hub_namespace:
            # TODO: this rule is failable since it's not only about is_hub and is_sensor
            # but in general for any namespace which would need special processing for Hub
            # which is different from the common device namespaces.
            # In current implementation (5.5.1) this should be related to the namespace being
            # collected in HUB_NAMESPACES.
            # TODO: For better design we should start getting away from hub parsing general
            # mechanics and migrate to using the standard dispatching api in NamespaceHandler
            # by registering subdevices (or directly subdevice entities) as sinks.
            return HubNamespaceHandler(self, ns)
        else:
            return super()._create_handler(ns)

    def _parse_hub(self, p_hub: dict):
        # This is usually called inside _parse_all as part of the digest parsing
        # Here we'll check the fresh subdevice list against the actual one and
        # eventually manage newly added subdevices or removed ones #119
        # telling the caller to persist the changed configuration (self.needsave)
        subdevices_actual = set(self.subdevices)
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
            # needsave=True might need some time querying abilities before
            # actually saving. We'll let some time to complete
            self.schedule_reload(5)

    # interface: self
    def log_duplicated_subdevice(self, subdevice_id: object):
        self.log(
            self.CRITICAL,
            "Subdevice %s (id:%s) appears twice in device data. Shouldn't happen",
            self.subdevices[subdevice_id].name,
            subdevice_id,
            timeout=604800,  # 1 week
        )

    def setup_chunked_handler(self, ns: mn.Namespace, is_mts100: bool, count: int):
        if (ns.name not in self.namespace_handlers) and (
            ns.name in self.descriptor.ability
        ):
            HubChunkedNamespaceHandler(
                self, ns, mc.MTS100_ALL_TYPESET, is_mts100, count
            )

    def setup_simple_handler(self, ns: mn.Namespace):
        try:
            self.namespace_handlers[ns.name].polling_response_size_inc()
        except KeyError:
            if ns.name in self.descriptor.ability:
                HubNamespaceHandler(self, ns)

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

    def _subdevice_build(self, p_subdevice: "dict[str, Any]"):
        # parses the subdevice payload in 'digest' to look for a well-known type
        # and builds accordingly
        model = None
        for p_key, p_value in p_subdevice.items():
            if type(p_value) is dict:
                model = p_key
                break
        else:
            # the hub could report incomplete info anytime so beware.
            # this is true when subdevice is offline and hub has no recent info
            # we'll check our device registry for luck
            try:
                hassdevice = self.api.device_registry.async_get_device(
                    identifiers={(mlc.DOMAIN, p_subdevice[mc.KEY_ID])}
                )
                if not hassdevice:
                    return None
                model = hassdevice.model
                assert model
            except Exception:
                return None

        try:
            return WELL_KNOWN_TYPE_MAP[model](self, p_subdevice)
        except:
            # build something anyway...
            return SubDevice(self, p_subdevice, model)  # type: ignore


class SubDevice(NamespaceParser, BaseDevice):
    """
    SubDevice introduces some hybridization in EntityManager:
    (owned) entities will refer to SubDevice effectively as if
    it were a full-fledged device but some EntityManager properties
    are overriden in order to manage ConfigEntry setup/unload since
    SubDevice doesn't actively represent one (it delegates this to
    the owning Hub).
    Inheriting from NamespaceParser allows this class to be registered
    as a parser for any namespace where the list payload indexing is carried
    over the key "id" (typical for hub namespaces - even though these namespaces
    are actually already custom handled in HubNamespaceHandler). This added
    flexibility is now necessary to allow for some new 'exotic' design (see
    ms130-Appliance.Control.Sensor.LatestX)
    """

    NAMESPACES = mn.HUB_NAMESPACES

    __slots__ = (
        "async_request",
        "check_device_timezone",
        "hub",
        "model",
        "p_digest",
        "sub_device_info",
        "sensor_battery",
        "switch_togglex",
    )

    def __init__(self, hub: HubMixin, p_digest: dict, model: str):
        # this is a very dirty trick/optimization to override some BaseDevice
        # properties/methods that just needs to be forwarded to the hub
        # this way we're short-circuiting that indirection
        self.async_request = hub.async_request
        self.check_device_timezone = hub.check_device_timezone
        # these properties are needed to be in place before base class init
        self.hub = hub
        self.model = model
        self.p_digest = p_digest
        self.sub_device_info = None
        id = p_digest[mc.KEY_ID]
        super().__init__(
            id,
            api=hub.api,
            hass=hub.hass,
            config_entry=hub.config_entry,
            logger=hub,
            name=get_productnameuuid(model, id),
            model=model,
            via_device=next(iter(hub.deviceentry_id["identifiers"])),
        )
        self.platforms = hub.platforms
        hub.subdevices[id] = self
        self.sensor_battery = MLNumericSensor(
            self, self.id, mc.KEY_BATTERY, MLNumericSensor.DeviceClass.BATTERY
        )
        # this is a generic toggle we'll setup in case the subdevice
        # 'advertises' it and no specialized implementation is in place
        self.switch_togglex: MLSwitch | None = None

        hub.setup_simple_handler(mn.Appliance_Hub_Battery)
        hub.setup_simple_handler(mn.Appliance_Hub_ToggleX)
        hub.setup_simple_handler(mn.Appliance_Hub_SubDevice_Version)
        if model not in mc.MTS100_ALL_TYPESET:
            hub.setup_chunked_handler(mn.Appliance_Hub_Sensor_All, False, 8)

    # interface: EntityManager
    def generate_unique_id(self, entity: "MLEntity"):
        """
        flexible policy in order to generate unique_ids for entities:
        This is an helper needed to better control migrations in code
        which could/would lead to a unique_id change.
        We could put here code checks in order to avoid entity_registry
        migrations
        """
        return f"{self.hub.id}_{entity.id}"

    # interface: BaseDevice
    async def async_shutdown(self):
        await NamespaceParser.async_shutdown(self)
        await BaseDevice.async_shutdown(self)
        self.check_device_timezone = None  # type: ignore
        self.async_request = None  # type: ignore
        self.hub: HubMixin = None  # type: ignore
        self.sensor_battery: MLNumericSensor = None  # type: ignore
        self.switch_togglex = None

    @property
    def tz(self):
        return self.hub.tz

    def get_type(self) -> mlc.DeviceType:
        return mlc.DeviceType.SUBDEVICE

    def _get_internal_name(self) -> str:
        return get_productnameuuid(self.model, self.id)

    def _set_online(self):
        super()._set_online()
        # force a re-poll even on MQTT
        self.hub.namespace_handlers[
            (
                mn.Appliance_Hub_Mts100_All.name
                if self.model in mc.MTS100_ALL_TYPESET
                else mn.Appliance_Hub_Sensor_All.name
            )
        ].polling_epoch_next = 0.0

    # interface: self
    def build_binary_sensor_window(self):
        return MLBinarySensor(
            self,
            self.id,
            str(MLBinarySensor.DeviceClass.WINDOW),
            MLBinarySensor.DeviceClass.WINDOW,
        )

    def update_sub_device_info(self, sub_device_info: "SubDeviceInfoType"):
        self.sub_device_info = sub_device_info
        name = sub_device_info.get(mc.KEY_SUBDEVICENAME) or self._get_internal_name()
        if name != self.device_registry_entry.name:
            self.api.device_registry.async_update_device(
                self.device_registry_entry.id, name=name
            )

    def _hub_parse(self, key: str, payload: dict):
        try:
            getattr(self, f"_parse_{key}")(payload)
        except AttributeError:
            # This happens when we still haven't 'normalized' the device structure
            # so we'll (eventually) euristically generate sensors for device properties
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
                    if type(subvalue) is dict:
                        _parse_dict(f"{parent_key}_{subkey}", subvalue)
                        continue
                    if type(subvalue) is list:
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

        except Exception as exception:
            self.log_exception(
                self.WARNING,
                exception,
                "_hub_parse(%s, %s)",
                key,
                str(payload),
                timeout=14400,
            )

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
        if self.online:
            for _ in (
                self._hub_parse(key, value)
                for key, value in p_digest.items()
                if (
                    key
                    not in {
                        mc.KEY_ID,
                        mc.KEY_STATUS,
                        mc.KEY_ONOFF,
                        mc.KEY_LASTACTIVETIME,
                    }
                )
                and (type(value) is dict)
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
        #     keys in "ms130"
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

        if self.online:
            for _ in (
                self._hub_parse(key, value)
                for key, value in p_all.items()
                if (key not in {mc.KEY_ID, mc.KEY_ONLINE}) and (type(value) is dict)
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
        if self.online:
            self.sensor_battery.update_native_value(p_battery[mc.KEY_VALUE])

    def _parse_exception(self, p_exception: dict):
        """{"id": "00000000", "code": 5061}"""
        # TODO: code 5061 seems related to loss of connectivity between the hub
        # and the device. We might put up a binary sensor.
        self.log(self.WARNING, "Received exception payload: %s", str(p_exception))

    def _parse_online(self, p_online: dict):
        if mc.KEY_STATUS in p_online:
            if p_online[mc.KEY_STATUS] == mc.STATUS_ONLINE:
                if not self.online:
                    self._set_online()
            else:
                if self.online:
                    self._set_offline()

    def _parse_togglex(self, p_togglex: dict):
        """{"id": "00000000", "onoff": 0, ...}"""
        # might come from parse_digest or from Appliance.Hub.ToggleX
        # in any case we're just interested to the "onoff" key
        try:
            self.switch_togglex.update_onoff(p_togglex[mc.KEY_ONOFF])  # type: ignore
        except AttributeError:
            self.switch_togglex = MLHubToggle(
                self,
                self.id,
                mc.KEY_TOGGLEX,
                MLSwitch.DeviceClass.SWITCH,
                device_value=p_togglex[mc.KEY_ONOFF],
            )

    def _parse_version(self, p_version: dict):
        """{"id": "00000000", "hardware": "1.1.5", "firmware": "5.1.8"}"""
        device_registry_entry = self.device_registry_entry
        kwargs = {}
        hw_version = p_version[mc.KEY_HARDWARE]
        if hw_version != device_registry_entry.hw_version:
            kwargs["hw_version"] = hw_version
        sw_version = p_version[mc.KEY_FIRMWARE]
        if sw_version != device_registry_entry.sw_version:
            kwargs["sw_version"] = sw_version
        if kwargs:
            self.api.device_registry.async_update_device(
                device_registry_entry.id, **kwargs
            )


class MTS100SubDevice(SubDevice):
    __slots__ = ("climate",)

    def __init__(self, hub: HubMixin, p_digest: dict, model: str = mc.TYPE_MTS100):
        super().__init__(hub, p_digest, model)
        from .mts100 import Mts100Climate

        self.climate = Mts100Climate(self)
        hub.setup_chunked_handler(mn.Appliance_Hub_Mts100_All, True, 8)
        hub.setup_chunked_handler(mn.Appliance_Hub_Mts100_ScheduleB, True, 4)
        hub.setup_simple_handler(mn.Appliance_Hub_Mts100_Adjust)

    async def async_shutdown(self):
        await super().async_shutdown()
        self.climate: "Mts100Climate" = None  # type: ignore

    def _parse_all(self, p_all: dict):
        self._parse_online(p_all.get(mc.KEY_ONLINE, {}))

        climate = self.climate

        if mc.KEY_SCHEDULEBMODE in p_all:
            climate.update_scheduleb_mode(p_all[mc.KEY_SCHEDULEBMODE])

        if p_mode := p_all.get(mc.KEY_MODE):
            climate._mts_mode = p_mode[mc.KEY_STATE]

        if p_togglex := p_all.get(mc.KEY_TOGGLEX):
            climate._mts_onoff = p_togglex[mc.KEY_ONOFF]

        if p_temperature := p_all.get(mc.KEY_TEMPERATURE):
            climate._parse_temperature(p_temperature)
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
        # typically called by SubDevice.parse_digest
        # when parsing Appliance.System.All
        pass

    def _parse_schedule(self, p_schedule: dict):
        self.climate.schedule._parse(p_schedule)

    def _parse_temperature(self, p_temperature: dict):
        self.climate._parse_temperature(p_temperature)

    def _parse_togglex(self, p_togglex: dict):
        climate = self.climate
        climate._mts_onoff = p_togglex[mc.KEY_ONOFF]
        climate.flush_state()


WELL_KNOWN_TYPE_MAP[mc.TYPE_MTS100] = MTS100SubDevice


class MTS100V3SubDevice(MTS100SubDevice):
    def __init__(self, hub: HubMixin, p_digest: dict):
        super().__init__(hub, p_digest, mc.TYPE_MTS100V3)

    def _parse_mts100v3(self, p_mts100v3: dict):
        # typically called by SubDevice.parse_digest
        # when parsing Appliance.System.All
        pass


WELL_KNOWN_TYPE_MAP[mc.TYPE_MTS100V3] = MTS100V3SubDevice


class MTS150SubDevice(MTS100SubDevice):
    def __init__(self, hub: HubMixin, p_digest: dict):
        super().__init__(hub, p_digest, mc.TYPE_MTS150)

    def _parse_mts150(self, p_mts150: dict):
        """{"mode": 3,"currentSet": 60,"updateMode": 3,"updateTemp": 175,
        "motorCurLocation": 0,"motorStartCtr": 883,"motorTotalPath": 69852}"""
        # typically called by SubDevice.parse_digest
        # when parsing Appliance.System.All
        pass


WELL_KNOWN_TYPE_MAP[mc.TYPE_MTS150] = MTS150SubDevice


class MTS150PSubDevice(MTS100SubDevice):
    def __init__(self, hub: HubMixin, p_digest: dict):
        super().__init__(hub, p_digest, mc.TYPE_MTS150P)

    def _parse_mts150p(self, p_mts150p: dict):
        """{"mode": 3,"currentSet": 60,"updateMode": 3,"updateTemp": 175,
        "motorCurLocation": 0,"motorStartCtr": 883,"motorTotalPath": 69852}"""
        # typically called by SubDevice.parse_digest
        # when parsing Appliance.System.All
        pass


WELL_KNOWN_TYPE_MAP[mc.TYPE_MTS150P] = MTS150PSubDevice


class GS559MuteToggle(me.MEListChannelMixin, MLSwitch):
    ns = mn.Appliance_Hub_Sensor_Smoke
    key_value: str = mc.KEY_INTERCONN

    # HA core entity attributes:


class GS559SubDevice(SubDevice):
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
        # "sensor_interConn",
        "switch_interConn",
    )

    def __init__(self, hub: HubMixin, p_digest: dict):
        super().__init__(hub, p_digest, mc.TYPE_GS559)
        self.sensor_status = MLEnumSensor(
            self, self.id, mc.KEY_STATUS, translation_key="smoke_alarm_status"
        )
        # self.sensor_interConn = MLEnumSensor(self, self.id, mc.KEY_INTERCONN)
        self.switch_interConn = GS559MuteToggle(
            self, self.id, mc.KEY_INTERCONN, MLSwitch.DeviceClass.SWITCH
        )
        self.binary_sensor_alarm = MLBinarySensor(
            self, self.id, "alarm", MLBinarySensor.DeviceClass.SAFETY
        )
        self.binary_sensor_error = MLBinarySensor(
            self, self.id, "error", MLBinarySensor.DeviceClass.PROBLEM
        )
        self.binary_sensor_muted = MLBinarySensor(self, self.id, "muted")

    async def async_shutdown(self):
        await super().async_shutdown()
        self.binary_sensor_muted: MLBinarySensor = None  # type: ignore
        self.binary_sensor_error: MLBinarySensor = None  # type: ignore
        self.binary_sensor_alarm: MLBinarySensor = None  # type: ignore
        self.sensor_status: MLEnumSensor = None  # type: ignore
        self.switch_interConn: GS559MuteToggle = None  # type: ignore
        # self.sensor_interConn: MLEnumSensor = None  # type: ignore

    def _parse_smokeAlarm(self, p_smokealarm: dict):
        if mc.KEY_STATUS in p_smokealarm:
            value = p_smokealarm[mc.KEY_STATUS]
            self.binary_sensor_alarm.update_onoff(value in GS559SubDevice.STATUS_ALARM)
            self.binary_sensor_error.update_onoff(value in GS559SubDevice.STATUS_ERROR)
            self.binary_sensor_muted.update_onoff(value in GS559SubDevice.STATUS_MUTED)
            self.sensor_status.update_native_value(
                GS559SubDevice.STATUS_MAP.get(value, value)
            )
        if mc.KEY_INTERCONN in p_smokealarm:
            # self.sensor_interConn.update_native_value(p_smokealarm[mc.KEY_INTERCONN])
            self.switch_interConn.update_onoff(p_smokealarm[mc.KEY_INTERCONN])


WELL_KNOWN_TYPE_MAP[mc.TYPE_GS559] = GS559SubDevice
# smokeAlarm devices (mc.TYPE_GS559) are presented as
# mc.KEY_SMOKEALARM in digest(s) so we have to map that too
WELL_KNOWN_TYPE_MAP[mc.KEY_SMOKEALARM] = GS559SubDevice


class MS100SubDevice(SubDevice):
    __slots__ = (
        "sensor_temperature",
        "sensor_humidity",
        "number_adjust_temperature",
        "number_adjust_humidity",
    )

    def __init__(self, hub: HubMixin, p_digest: dict):
        super().__init__(hub, p_digest, mc.TYPE_MS100)
        self.sensor_temperature = MLTemperatureSensor(self, self.id, device_scale=10)
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
        hub.setup_simple_handler(mn.Appliance_Hub_Sensor_Adjust)

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
        # typically called by SubDevice.parse_digest
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
        if sensor.update_device_value(device_value):
            strategy = self.hub.namespace_handlers[mn.Appliance_Hub_Sensor_Adjust.name]
            if strategy.lastrequest < (self.hub.lastresponse - 30):
                strategy.polling_epoch_next = 0.0


WELL_KNOWN_TYPE_MAP[mc.TYPE_MS100] = MS100SubDevice
# there's a new temp/hum sensor in town (MS100FH - see #303)
# and it is likely presented as tempHum in digest
# (need confirmation from device tracing though)
WELL_KNOWN_TYPE_MAP[mc.KEY_TEMPHUM] = MS100SubDevice


class MS130SubDevice(SubDevice):
    __slots__ = (
        "sensor_humidity",
        "sensor_light",
        "sensor_temperature",
    )

    def __init__(self, hub: HubMixin, p_digest: dict):
        super().__init__(hub, p_digest, mc.TYPE_MS130)
        self.sensor_humidity = MLHumiditySensor(self, self.id)
        self.sensor_temperature = MLTemperatureSensor(self, self.id, device_scale=100)
        self.sensor_light = MLLightSensor(self, self.id)
        # This hybrid ns should have LIST_C structure in Hub(s)
        ns = mn.Hub_Control_Sensor_LatestX
        try:
            handler = hub.namespace_handlers[ns.name]
        except KeyError:
            handler = (
                HubNamespaceHandler(hub, ns)
                if ns.name in hub.descriptor.ability
                else None
            )
        if handler:
            handler.polling_request_add_channel(self.id)

    async def async_shutdown(self):
        await super().async_shutdown()
        self.sensor_light: MLNumericSensor = None  # type: ignore
        self.sensor_temperature: MLNumericSensor = None  # type: ignore
        self.sensor_humidity: MLNumericSensor = None  # type: ignore

    def _parse_humidity(self, p_humidity: dict):
        """parser for Appliance.Hub.Sensor.All:
        {
        ...
        "humidity": {
                "latest": 711,
                "latestSampleTime": 1722219198,
                "max": 1000,
                "min": 0
              },
        ...
        }
        """
        self.sensor_humidity.update_device_value(p_humidity[mc.KEY_LATEST])

    def _parse_temperature(self, p_temperature: dict):
        """parser for Appliance.Hub.Sensor.All:
        {
        ...
        "temperature": {
                "latest": 1772,
                "latestSampleTime": 1722219198,
                "max": 600,
                "min": -200
              },
        ...
        }
        """
        self.sensor_temperature.update_device_value(p_temperature[mc.KEY_LATEST])

    def _parse_tempHumi(self, p_temphumi: dict):
        """parser for digest carried "tempHumi": {"latestTime": 1722219198, "temp": 1772, "humi": 711}"""
        self.sensor_temperature.update_device_value(p_temphumi[mc.KEY_TEMP])
        self.sensor_humidity.update_device_value(p_temphumi[mc.KEY_HUMI])

    def _parse_togglex(self, p_togglex: dict):
        # avoid the base class creating a toggle entity
        # since we're pretty sure ms130 doesn't have one
        pass

    def _parse_latest(self, p_latest: dict):
        """parser for Appliance.Control.Sensor.LatestX:
        {
            "latest": [
                {
                    "data": {
                        "light": [{"value": 220, "timestamp": 1722349685}],
                        "temp": [{"value": 2134, "timestamp": 1722349685}],
                        "humi": [{"value": 670, "timestamp": 1722349685}],
                    },
                    "channel": 0,
                    "subId": "1A00694ACBC7",
                }
            ]
        }
        """
        p_data = p_latest[mc.KEY_DATA]
        try:
            self.sensor_light.update_device_value(p_data[mc.KEY_LIGHT][0][mc.KEY_VALUE])
        except:
            pass
        try:
            self.sensor_temperature.update_device_value(
                p_data[mc.KEY_TEMP][0][mc.KEY_VALUE]
            )
        except:
            pass
        try:
            self.sensor_humidity.update_device_value(
                p_data[mc.KEY_HUMI][0][mc.KEY_VALUE]
            )
        except:
            pass


WELL_KNOWN_TYPE_MAP[mc.TYPE_MS130] = MS130SubDevice
WELL_KNOWN_TYPE_MAP[mc.KEY_TEMPHUMI] = MS130SubDevice


class MS200SubDevice(SubDevice):
    __slots__ = ("binary_sensor_window",)

    def __init__(self, hub: HubMixin, p_digest: dict):
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


class MS400SubDevice(SubDevice):
    __slots__ = ("binary_sensor_waterleak",)

    def __init__(self, hub: HubMixin, p_digest: dict):
        super().__init__(hub, p_digest, mc.TYPE_MS400)
        self.binary_sensor_waterleak = MLBinarySensor(
            self, self.id, mc.KEY_WATERLEAK, MLBinarySensor.DeviceClass.SAFETY
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


def digest_init_hub(device: "HubMixin", digest) -> "DigestInitReturnType":

    device.subdevices = {}
    for p_subdevice_digest in digest[mc.KEY_SUBDEVICE]:
        try:
            subdevice_id = p_subdevice_digest[mc.KEY_ID]
            if subdevice_id in device.subdevices:
                device.log_duplicated_subdevice(subdevice_id)
            else:
                device._subdevice_build(p_subdevice_digest)
        except Exception as exception:
            device.log_exception(device.WARNING, exception, "digest_init_hub")

    return device._parse_hub, ()
