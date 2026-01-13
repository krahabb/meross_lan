from functools import cached_property
from typing import TYPE_CHECKING, override

from ... import const as mlc
from ...binary_sensor import MLBinarySensor
from ...button import MLButton
from ...calendar import MtsSchedule
from ...climate import MtsClimate
from ...helpers import entity as me
from ...helpers.device import BaseDevice, Device
from ...helpers.namespaces import (
    POLLING_STRATEGY_CONF,
    NamespaceHandler,
    NamespaceParser,
    mc,
    mn,
)
from ...merossclient import get_productnameuuid, versiontuple
from ...merossclient.protocol.namespaces import hub as mn_h
from ...number import MLConfigNumber
from ...sensor import (
    MLDiagnosticSensor,
    MLEnumSensor,
    MLHumiditySensor,
    MLLightSensor,
    MLNumericSensor,
    MLTemperatureSensor,
)
from ...switch import MLDeviceSwitch
from .mts100 import Mts100Climate

if TYPE_CHECKING:
    from typing import (
        Any,
        Callable,
        ClassVar,
        Collection,
        Final,
        Mapping,
        NotRequired,
        TypedDict,
    )

    from ...helpers.device import AsyncRequestFunc, DigestInitReturnType, MerossMessage
    from ...helpers.entity import MLEntity
    from ...helpers.meross_profile import (
        DeviceInfoExtType,
        MQTTProfile,
    )
    from ...merossclient.cloudapi import SubDeviceInfoType
    from ...merossclient.protocol import types as mt
    from ...merossclient.protocol.namespaces import Namespace
    from ...merossclient.protocol.types import (
        JsonDict,
        JsonList,
        control as mt_c,
        hub as mt_h,
        sensor as mt_s,
    )

    WELL_KNOWN_TYPE_MAP: Final[dict[str, Callable]]

WELL_KNOWN_TYPE_MAP = dict(
    {
        # typical entries (they're added on SubDevice declaration)
        # mc.TYPE_MS100: MS100SubDevice,
        # mc.TYPE_MTS100: MTS100SubDevice,
    }
)


class HubSensorAdjustNumber(MLConfigNumber):
    ns = mn_h.Appliance_Hub_Sensor_Adjust

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
        /,
    ):
        self.key_value = key
        self.native_min_value = min_value
        self.native_max_value = max_value
        self.native_step = step
        MLConfigNumber.__init__(
            self,
            manager,
            manager.id,
            f"config_{self.ns.key}_{self.key_value}",
            device_class=device_class,
            device_scale=10,
            name=f"Adjust {device_class}",
        )

    @override
    async def async_request_value(self, device_value, /):
        # the SET command on NS_APPLIANCE_HUB_SENSOR_ADJUST works by applying
        # the issued value as a 'delta' to the current configured value i.e.
        # 'new adjust value' = 'current adjust value' + 'issued adjust value'
        # Since the native HA interface async_set_native_value wants to set
        # the 'new adjust value' we have to issue the difference against the
        # currently configured one
        (
            await self.manager.async_request(
                *self.ns.request_set(
                    {self.key_value: device_value - self.device_value}, self.channel
                )
            )
        )
        self.update_device_value(device_value)


class HubToggleX(MLDeviceSwitch):
    """Generic switch to map Appliance.Hub.ToggleX namespace."""

    ENTITY_KEY = mc.KEY_TOGGLEX
    ns = mn_h.Appliance_Hub_ToggleX


class HubBeep(MLDeviceSwitch):
    """Generic switch to map Appliance.Hub.SubDevice.Beep namespace."""

    ns = mn_h.Appliance_Hub_SubDevice_Beep
    ENTITY_KEY = f"{ns.slug}__{MLDeviceSwitch.key_value}"


class HubSubIdChannelMixin(MLEntity if TYPE_CHECKING else object):
    """
    Mixin implementation for protocol method 'SET' on hub entities/namespaces backed by a
    subId/channel indexing key pair.
    TODO: migrate subdevice entities channel indexing (needs registry migration).
    Right now we're fixing channel to 0 since hub subdevices seems to not discriminate channels.
    Implementing full support for varying channels per subdevice would need some rework on subdevice
    entities indexing (id and unique_id)and management.
    """

    if TYPE_CHECKING:
        manager: "SubDevice"

    @override
    async def async_request_value(self, device_value, /):
        (
            await self.manager.async_request(
                *self.ns.request_set(
                    {mc.KEY_CHANNEL: 0, self.key_value: device_value}, self.channel
                )
            )
        )
        self.update_device_value(device_value)


class HubSubIdDeviceCfgMixin(me.MEGroupListChannelMixin):
    """
    Mixin implementation for protocol method 'SET' on 'Appliance.Config.DeviceCfg'.
    """

    if TYPE_CHECKING:
        manager: "SubDevice"

    ns = mn_h.Appliance_Config_DeviceCfg

    @override
    async def async_request_value(self, device_value, /):
        (
            await self.manager.async_request(
                *self.ns.request_set(
                    {mc.KEY_CHANNEL: 0, self.key_group: {self.key_value: device_value}},
                    self.channel,
                )
            )
        )
        self.update_device_value(device_value)


class HubNamespaceHandler(NamespaceHandler):
    """
    This namespace handler must be used to handle all of the Appliance.Hub.xxx namespaces
    since the payload parsing would just be the same where the data are just forwarded to the
    relevant subdevice instance. (TODO) This class could/should be removed in favor of the base class
    indexed parsing but this will need some work...
    """

    device: "HubMixin"

    def __init__(self, device: "HubMixin", ns: "Namespace"):
        NamespaceHandler.__init__(self, device, ns, handler=self._handle_subdevice)

    def _handle_subdevice(self, message: "MerossMessage"):
        """Generalized Hub namespace dispatcher to subdevices"""
        hub = self.device
        subdevices = hub.subdevices
        subdevices_parsed = set()
        key_namespace = self.ns.key
        key_channel = self.ns.key_channel
        for p_subdevice in message.payload[key_namespace]:
            try:
                subdevice_id = p_subdevice[key_channel]
                if subdevice_id in subdevices_parsed:
                    hub.log_duplicated_subdevice(subdevice_id)
                else:
                    try:
                        subdevices[subdevice_id]._hub_parse(key_namespace, p_subdevice)
                    except KeyError:
                        # force a rescan since we discovered a new subdevice
                        hub.ns_handlers[mn.Appliance_System_All].polling_epoch_next = (
                            0.0
                        )
                    subdevices_parsed.add(subdevice_id)
            except TypeError:
                # This could happen when the main payload is not a list of subdevices
                # and might indicate this namespace is likely devoted to general hub
                # commands/info (something like Appliance.Hub.*)
                self.handler = self._handle_undefined
                self._handle_undefined(message)
            except Exception as exception:
                self.handle_exception(exception, "_handle_subdevice", p_subdevice)


class HubChunkedNamespaceHandler(HubNamespaceHandler):
    """
    This is a strategy for polling (general) subdevices state with special care for messages
    possibly generating huge payloads (see #244).
    The strategy itself will poll the namespace on every cycle if no MQTT active
    When MQTT active we rely on states PUSHES in general but we'll also poll
    from time to time (see POLLING_STRATEGY_CONF for the relevant namespaces).
    TODO# refactor this code to take care of multiple requests size limits (or in
    general device buffer estimated sizes) so that we can adapt the chunking size
    to the available buffers and not use a fixed preset. Also,
    since we're refactoring the message handling routing in NamespaceHandler/Device
    the current way of building the payloads doesnt work anymore since the device
    is maintaining a list of namespaces to query and not a list of actual payloads
    like it was before.
    """

    __slots__ = (
        "_models",
        "_included",
        "_count",
    )

    def __init__(
        self,
        device: "HubMixin",
        ns: "Namespace",
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
                self._polling_request_set(p)
                if await device.async_request_smartpoll(
                    self,
                    cloud_queue_max=max_queuable,
                ):
                    max_queuable += 1
                    # TODO# refactor this code to take care of new async_pol/request semantics
                    # Right now, this 'flush' should do the job but is strongly inefficient.
                    if device._multiple_requests:
                        await device._async_multiple_requests_flush()

    async def async_trace(self, async_request_func: "AsyncRequestFunc"):
        for p in self._build_subdevices_payload():
            self._polling_request_set(p)
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
        key_channel = self.ns.key_channel
        for subdevice in self.device.subdevices.values():
            if (subdevice.model in self._models) == self._included:
                payload.append({key_channel: subdevice.id})
                if len(payload) == self._count:
                    yield payload
                    payload = []
        if payload:
            yield payload

    def _polling_request_set(self, payload: list | dict, /):
        self.polling_request = (
            self.ns,
            mc.METHOD_GET,
            {self.ns.key: payload},
        )
        self.polling_response_size = (
            self.polling_response_base_size
            + self.polling_response_item_size
            * (len(payload) if type(payload) is list else 1)
        )


class HubMixin(Device if TYPE_CHECKING else object):
    """
    Specialized Device for smart hub(s) like MSH300
    """

    DEVICE_TYPE = mlc.DeviceType.HUB
    NAMESPACES = mn.HUB_NAMESPACES

    DEFAULT_PLATFORMS = Device.DEFAULT_PLATFORMS | {
        MLBinarySensor.PLATFORM: None,
        MLButton.PLATFORM: None,
        MtsSchedule.PLATFORM: None,
        MLConfigNumber.PLATFORM: None,
        MLNumericSensor.PLATFORM: None,
        MLDeviceSwitch.PLATFORM: None,
        MtsClimate.PLATFORM: None,
        MtsClimate.TrackSensorSelect.PLATFORM: None,
    }

    TRACE_ABILITY_EXCLUDE = Device.TRACE_ABILITY_EXCLUDE + (
        mn_h.Appliance_Hub_Exception,
        mn_h.Appliance_Hub_Report,
        mn_h.Appliance_Hub_SubdeviceList,
        *(ns for ns in mn.HUB_NAMESPACES.values() if not ns.can_query),
    )

    @override
    async def async_shutdown(self):
        for subdevice in self.subdevices.values():
            await subdevice.async_shutdown()
        self.subdevices.clear()
        await super().async_shutdown()

    @override
    def managed_entities(self, platform, /):
        entities = super().managed_entities(platform)
        for subdevice in self.subdevices.values():
            entities.extend(subdevice.managed_entities(platform))
        return entities

    @override
    def _set_offline(self):
        for subdevice in self.subdevices.values():
            subdevice._set_offline()
        super()._set_offline()

    @override
    def _create_handler(self, ns: "Namespace", /):
        _handler = getattr(self, f"_handle_{ns.replace('.', '_')}", None)
        if _handler:
            return NamespaceHandler(
                self,
                ns,
                handler=_handler,
            )
        elif ns.key_channel is mc.KEY_ID:
            # This rule states that the payload is a list of subdevices indexed by 'id'.
            # Newer devices (2024) started using namespaces/payload indexed by 'subid'
            # and 'channel'. These will be handled by the base class NamespaceHandler
            # using SubDevice/Entity as NamespaceParser.
            return HubNamespaceHandler(self, ns)
        else:
            return super()._create_handler(ns)

    def _parse_hub(self, p_hub: dict, /):
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
                subdevice = self.subdevices.pop(subdevice_id)
                self.log(
                    self.WARNING,
                    "%s (id:%s) unregistered from hub",
                    subdevice.name,
                    subdevice_id,
                )
                if subdevice.online:
                    subdevice._set_offline()
                self.async_create_task(
                    subdevice.async_shutdown(),
                    f"{subdevice.__class__.__name__}.async_shutdown()",
                )
                self.create_issue(
                    mlc.ISSUE_HUB_SUBDEVICE_REMOVED,
                    subdevice_id,
                    severity=self.IssueSeverity.WARNING,
                    translation_placeholders={"device_name": subdevice.name},
                )

    @override
    def update_device_info(
        self, device_info: "DeviceInfoExtType", profile: "MQTTProfile", /
    ):
        super().update_device_info(device_info, profile)
        # propagate device info to subdevices
        for sub_device_info in device_info.get("__subDeviceInfo", []):
            try:
                self.subdevices[sub_device_info["subDeviceId"]].update_sub_device_info(
                    sub_device_info
                )
            except KeyError:
                continue

    # interface: self
    def log_duplicated_subdevice(self, subdevice_id: str, /):
        self.log(
            self.CRITICAL,
            "Subdevice %s (id:%s) appears twice in device data. Shouldn't happen",
            self.subdevices[subdevice_id].name,
            subdevice_id,
            timeout=604800,  # 1 week
        )

    def setup_chunked_handler(self, ns: "Namespace", is_mts100: bool, count: int, /):
        if (ns not in self.ns_handlers) and (ns in self.descriptor.ability):
            HubChunkedNamespaceHandler(
                self, ns, mc.MTS100_ALL_TYPESET, is_mts100, count
            )

    def setup_simple_handlers(self, *nss: "Namespace"):
        ability = self.descriptor.ability
        for ns in nss:
            try:
                self.ns_handlers[ns].polling_response_size_inc()
            except KeyError:
                if ns in ability:
                    HubNamespaceHandler(self, ns)

    def setup_subid_handlers(
        self,
        subdevice: "SubDevice",
        *nss: "Namespace",
        extra: "mt.MerossPayloadType" = {"channel": 0},
    ):
        ability = self.descriptor.ability
        for ns in nss:
            if ns not in ability:
                continue
            handler = self.get_handler(ns)
            handler.register_parser(subdevice)
            handler.polling_request_add_channel(subdevice.channel, extra)

    def _handle_Appliance_Digest_Hub(self, message: "MerossMessage", /):
        self._parse_hub(message.payload[mc.KEY_HUB])

    def _handle_Appliance_Hub_ExtraInfo(self, message: "MerossMessage", /):
        """TODO: decode
        {
          "extraInfo": {
            "upgradeSubDevs": [
              {
                "type": "ms200"
              },
              {
                "type": "mts150p"
              },
              {
                "type": "ms130"
              },
              {
                "type": "ms120"
              }
            ]
          }
        }
        """
        pass

    def _handle_Appliance_Hub_SubdeviceList(self, message: "MerossMessage", /):
        """TODO: decode
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
        pass

    def _subdevice_build(self, p_subdevice: "dict[str, Any]", /):
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
                device_entry = self.api.device_registry.async_get_device(
                    identifiers={(mlc.DOMAIN, p_subdevice[mc.KEY_ID])}
                )
                if not device_entry:
                    return None
                model = device_entry.model
                assert model
            except Exception:
                return None

        if model.lower().startswith("mts"):
            return MTSSubDevice(self, p_subdevice, model)

        try:
            return WELL_KNOWN_TYPE_MAP[model](self, p_subdevice)
        except:
            # build something anyway...
            return SubDevice(self, p_subdevice, model)


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
    ms130-Appliance.Control.Sensor.LatestX).
    TODO:
    - some ns are only handled across a subset of devices. For example *.ToggleX
    is not meaningful everywhere and so does *.Beep.
    We could think of a map between hub ns and subdevice type in order to
    fix what works where. This map could also be dynamic if we wish to
    update it along the way...
    TODO# re-implement SubDevice as a Mixin MLEntity with dedidated overrides for
    hub interaction.
    This is particularly helpful for those subdevices implementing a single 'main entity'
    feature like Mts which are already centered around a single entity (climate).
    This refactor could be done in steps where start with those subdevices which clearly
    expose a single entity (Mts, smoke detector, water leak sensor) and later move to
    more complex ones.
    This refactor would also streamline moving the custom ns handling in HubMixin/SubDevice
    to a more natural NamespaceHandler implementation and also remove the need for
    BaseDevice inheritance here.
    """

    if TYPE_CHECKING:
        NS_ALL: ClassVar[Namespace]  # to be set in subclasses
        hub: Final[HubMixin]
        channel: Final[str]
        model: Final[str]
        p_digest: JsonDict
        sensor_battery: Final[MLNumericSensor]

    DEVICE_TYPE = mlc.DeviceType.SUBDEVICE

    __slots__ = (
        "async_request",
        "check_device_timezone",
        "ns_handlers",
        "hub",
        "channel",
        "model",
        "p_digest",
        "sensor_battery",
    )

    def __init__(self, hub: HubMixin, p_digest: dict, model: str, /):
        # this is a very dirty trick/optimization to override some BaseDevice
        # properties/methods that just needs to be forwarded to the hub
        # this way we're short-circuiting that indirection
        self.async_request = hub.async_request
        self.check_device_timezone = hub.check_device_timezone
        self.ns_handlers = hub.ns_handlers
        # these properties are needed to be in place before base class init
        self.hub = hub
        self.channel = id = p_digest[mc.KEY_ID]
        self.model = model
        self.p_digest = p_digest
        super().__init__(
            id,
            api=hub.api,
            hass=hub.hass,
            config_entry=hub.config_entry,
            name=get_productnameuuid(model, id),
            model=model,
            via_device=next(iter(hub.deviceentry_id["identifiers"])),
            logger=hub,
        )
        self.platforms = hub.platforms
        hub.subdevices[id] = self
        self.sensor_battery = MLNumericSensor(
            self,
            self.id,
            mc.KEY_BATTERY,
            device_class=MLNumericSensor.DeviceClass.BATTERY,
        )
        hub.setup_simple_handlers(
            mn_h.Appliance_Hub_Battery,
            mn_h.Appliance_Hub_ToggleX,
            mn_h.Appliance_Hub_SubDevice_Beep,
            mn_h.Appliance_Hub_SubDevice_Version,
        )
        hub.setup_subid_handlers(self, mn_h.Appliance_Config_DeviceCfg)
        hub.remove_issue(mlc.ISSUE_HUB_SUBDEVICE_REMOVED, id)

    # interface: NamespaceParser
    @cached_property
    def handler_ns(self) -> "NamespaceHandler":
        # TODO: define a more consistent interface
        return self.hub.get_handler(self.ns)

    # interface: EntityManager
    @override
    def generate_unique_id(self, entity: "MLEntity", /):
        """
        flexible policy in order to generate unique_ids for entities:
        This is an helper needed to better control migrations in code
        which could/would lead to a unique_id change.
        We could put here code checks in order to avoid entity_registry
        migrations
        """
        return f"{self.hub.id}_{entity.id}"

    # interface: BaseDevice
    @override
    async def async_shutdown(self):
        await NamespaceParser.async_shutdown(self)
        await BaseDevice.async_shutdown(self)
        del self.check_device_timezone
        del self.async_request
        del self.ns_handlers
        del self.hub  # type: ignore
        del self.sensor_battery  # type: ignore
        # brutal trick to remove references to sensors _parse methods
        # should they exist (being installed at runtime)
        for _parse_method in [k for k in self.__dict__ if k.startswith("_parse_")]:
            delattr(self, _parse_method)

    @override
    def get_upgrade_payload(self, /) -> "mt_c.Upgrade":
        # start from hub upgrade payload (eventually)
        upgrade_payload = self.hub.get_upgrade_payload()
        latest_version = self.latest_version
        if versiontuple(latest_version[mc.KEY_VERSION]) > versiontuple(
            self.device_registry_entry.sw_version or latest_version[mc.KEY_VERSION]
        ):
            upgrade_payload["subdev"] = [
                {
                    "devid": self.id,
                    mc.KEY_URL: latest_version[mc.KEY_URL],
                    mc.KEY_MD5: latest_version[mc.KEY_MD5],
                }
            ]
        return upgrade_payload

    @override
    def get_upgrade_info(self, /):
        return (
            self.device_registry_entry.sw_version,
            self.latest_version.get(mc.KEY_VERSION),
            self.latest_version.get(mc.KEY_DESCRIPTION),
        )

    @property
    @override
    def tz(self):
        return self.hub.tz

    @override
    def _get_internal_name(self) -> str:
        return get_productnameuuid(self.model, self.id)

    @override
    def _set_online(self):
        BaseDevice._set_online(self)
        # force a re-poll even on MQTT
        self.ns_handlers[self.NS_ALL].polling_epoch_next = 0.0

    # interface: self
    def update_sub_device_info(self, sub_device_info: "SubDeviceInfoType", /):
        name = sub_device_info.get(mc.KEY_SUBDEVICENAME) or self._get_internal_name()
        if name != self.device_registry_entry.name:
            self.api.device_registry.async_update_device(
                self.device_registry_entry.id, name=name
            )

    def _hub_parse(self, key: str, payload: dict, /):
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

    def parse_digest(self, payload, /):
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
        self.p_digest = payload
        self._parse_online(payload)
        if self.online:
            _excluded_keys = (
                mc.KEY_ID,
                mc.KEY_STATUS,
                mc.KEY_ONOFF,
                mc.KEY_LASTACTIVETIME,
            )
            for _ in (
                self._hub_parse(key, value)
                for key, value in payload.items()
                if (type(value) is dict) and (key not in _excluded_keys)
            ):
                pass
            if mc.KEY_ONOFF in payload:
                self._parse_togglex(payload)

    def _parse_all(self, payload: dict, /):
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
        self._parse_online(payload.get(mc.KEY_ONLINE, {}))

        if self.online:
            _excluded_keys = (mc.KEY_ID, mc.KEY_ONLINE)
            for _ in (
                self._hub_parse(key, value)
                for key, value in payload.items()
                if (type(value) is dict) and (key not in _excluded_keys)
            ):
                pass

    def _parse_battery(self, payload: "mt_h.Battery", /):
        if self.online:
            self.sensor_battery.update_native_value(payload[mc.KEY_VALUE])

    def _parse_deviceCfg(self, payload: "mt_h.SubIdPayload", /):
        pass

    def _parse_exception(self, payload, /):
        """{"id": "00000000", "code": 5061}"""
        # TODO: code 5061 seems related to loss of connectivity between the hub
        # and the device. We might put up a binary sensor.
        self.log(self.WARNING, "Received exception payload: %s", str(payload))

    def _parse_online(self, payload, /):
        if payload[mc.KEY_STATUS] == mc.STATUS_ONLINE:
            if not self.online:
                self._set_online()
        else:
            if self.online:
                self._set_offline()

    def _parse_togglex(self, payload: "mt_h.ToggleX", /):
        self._parse_togglex = HubToggleX(
            self,
            self.id,
            device_value=payload[mc.KEY_ONOFF],
        )._parse

    def _parse_alarm(self, payload: "mt_h.Beep", /):
        # likely working in mts150 and/or GS559
        self._parse_alarm = HubBeep(
            self,
            self.id,
            name="Beep alarm",
            device_value=payload[mc.KEY_ONOFF],
        )._parse

    def _parse_version(self, payload: "mt_h.Version", /):
        device_registry_entry = self.device_registry_entry
        kwargs = {}
        hw_version = payload[mc.KEY_HARDWARE]
        if hw_version != device_registry_entry.hw_version:
            kwargs["hw_version"] = hw_version
        sw_version = payload[mc.KEY_FIRMWARE]
        if sw_version != device_registry_entry.sw_version:
            kwargs["sw_version"] = sw_version
        if kwargs:
            self.api.device_registry.async_update_device(
                device_registry_entry.id, **kwargs
            )


class MTSSubDevice(SubDevice):
    """Common class for any mts-like subdevice (mts100, mts150, mts150p, ...)."""

    NS_ALL = mn_h.Appliance_Hub_Mts100_All

    def __init__(self, hub: HubMixin, p_digest: dict, model: str):
        SubDevice.__init__(self, hub, p_digest, model)
        hub.setup_chunked_handler(mn_h.Appliance_Hub_Mts100_All, True, 8)
        hub.setup_chunked_handler(mn_h.Appliance_Hub_Mts100_ScheduleB, True, 4)
        hub.setup_simple_handlers(
            mn_h.Appliance_Hub_Mts100_Adjust,
            mn_h.Appliance_Hub_Mts100_Mode,
            mn_h.Appliance_Hub_Mts100_Temperature,
        )
        climate = Mts100Climate(self)
        self._parse_all = climate._parse_all
        self._parse_adjust = climate.number_adjust_temperature._parse
        self._parse_mode = climate._parse_mode
        self._parse_schedule = climate.schedule._parse
        self._parse_temperature = climate._parse_temperature
        self._parse_togglex = climate._parse_togglex


class SensorSubDevice(SubDevice):
    """
    Common class for any sensor-like subdevice (ms100, ms120, ms130, ...).
    """

    NS_ALL = mn_h.Appliance_Hub_Sensor_All

    def __init__(self, hub: HubMixin, p_digest: dict, model: str, /):
        SubDevice.__init__(self, hub, p_digest, model)
        hub.setup_chunked_handler(mn_h.Appliance_Hub_Sensor_All, False, 8)


class GS559SubDevice(SensorSubDevice):

    if TYPE_CHECKING:
        binary_sensor_alarm: MLBinarySensor
        binary_sensor_error: MLBinarySensor
        binary_sensor_muted: MLBinarySensor
        sensor_status: MLEnumSensor
        sensor_interConn: MLEnumSensor
        _smokealarm_status: int | None

    ns = mn_h.Appliance_Hub_Sensor_Smoke

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
    MUTE_MAP = {17: 20, 18: 21, 19: 22, 24: 26, 25: 27, None: 170}
    STATUS_ALARM = {23, 24, 25, 26, 27}
    STATUS_ERROR = {17, 18, 19, 20, 21, 22}
    STATUS_MUTED = {20, 21, 22, 26, 27}

    __slots__ = (
        "binary_sensor_alarm",
        "binary_sensor_error",
        "binary_sensor_muted",
        "sensor_status",
        "sensor_interConn",
        "_smokealarm_status",
    )

    def __init__(self, hub: HubMixin, p_digest: dict, /):
        SensorSubDevice.__init__(self, hub, p_digest, mc.TYPE_GS559)
        self.binary_sensor_alarm = MLBinarySensor(
            self, self.id, "alarm", device_class=MLBinarySensor.DeviceClass.SAFETY
        )
        self.binary_sensor_error = MLBinarySensor(
            self, self.id, "error", device_class=MLBinarySensor.DeviceClass.PROBLEM
        )
        self.binary_sensor_muted = MLBinarySensor(self, self.id, "muted")
        MLButton(
            self, self.id, "button_mute", self._async_button_mute_press, name="Mute"
        )
        MLButton(
            self, self.id, "button_test", self._async_button_test_press, name="Test"
        )
        self.sensor_status = MLEnumSensor(
            self, self.id, mc.KEY_STATUS, translation_key="smoke_alarm_status"
        )
        self.sensor_interConn = MLEnumSensor(self, self.id, mc.KEY_INTERCONN)
        self._smokealarm_status = None

    async def async_shutdown(self):
        await SensorSubDevice.async_shutdown(self)
        del self.binary_sensor_muted
        del self.binary_sensor_error
        del self.binary_sensor_alarm
        del self.sensor_status
        del self.sensor_interConn

    def _parse_smokeAlarm(self, p_smokealarm: dict, /):
        self._smokealarm_status = value = p_smokealarm[mc.KEY_STATUS]
        self.binary_sensor_alarm.update_native_value(
            value in GS559SubDevice.STATUS_ALARM
        )
        self.binary_sensor_error.update_native_value(
            value in GS559SubDevice.STATUS_ERROR
        )
        self.binary_sensor_muted.update_native_value(
            value in GS559SubDevice.STATUS_MUTED
        )
        self.sensor_status.update_native_value(
            GS559SubDevice.STATUS_MAP.get(value, value)
        )
        try:
            self.sensor_interConn.update_native_value(p_smokealarm[mc.KEY_INTERCONN])
        except KeyError:
            pass

    @override
    def _parse_togglex(self, p_togglex: dict, /):
        # avoid the base class creating a toggle entity
        # since we're pretty sure gs559 doesn't have any funcionality here
        # (https://github.com/krahabb/meross_lan/discussions/6#discussioncomment-15234566)
        pass

    async def _async_button_mute_press(self, /):
        try:
            await self.async_request_payload(
                {
                    mc.KEY_STATUS: GS559SubDevice.MUTE_MAP.get(
                        self._smokealarm_status, 170
                    ),
                }
            )
        except KeyError as e:
            # in case the state is not present in the MUTE_MAP (i.e. not mutable)
            self.log_exception(self.DEBUG, e, "trying to send mute command")

    async def _async_button_test_press(self, /):
        await self.async_request_payload({mc.KEY_STATUS: 23})


WELL_KNOWN_TYPE_MAP[mc.TYPE_GS559] = GS559SubDevice
# smokeAlarm devices (mc.TYPE_GS559) are presented as
# mc.KEY_SMOKEALARM in digest(s) so we have to map that too
WELL_KNOWN_TYPE_MAP[mc.KEY_SMOKEALARM] = GS559SubDevice


class MS100SubDevice(SensorSubDevice):
    __slots__ = (
        "sensor_temperature",
        "sensor_humidity",
        "number_adjust_temperature",
        "number_adjust_humidity",
    )

    def __init__(self, hub: HubMixin, p_digest: dict):
        SensorSubDevice.__init__(self, hub, p_digest, mc.TYPE_MS100)
        self.sensor_temperature = MLTemperatureSensor(self, self.id, device_scale=10)
        self.sensor_humidity = MLHumiditySensor(self, self.id)
        self.number_adjust_temperature = HubSensorAdjustNumber(
            self,
            mc.KEY_TEMPERATURE,
            HubSensorAdjustNumber.DeviceClass.TEMPERATURE,
            -5,
            5,
            0.1,
        )
        self.number_adjust_humidity = HubSensorAdjustNumber(
            self,
            mc.KEY_HUMIDITY,
            HubSensorAdjustNumber.DeviceClass.HUMIDITY,
            -20,
            20,
            1,
        )
        hub.setup_simple_handlers(mn_h.Appliance_Hub_Sensor_Adjust)

    async def async_shutdown(self):
        await SensorSubDevice.async_shutdown(self)
        del self.sensor_temperature
        del self.sensor_humidity
        del self.number_adjust_temperature
        del self.number_adjust_humidity

    def _parse_adjust(self, p_adjust: dict):
        self.number_adjust_temperature.update_device_value(p_adjust[mc.KEY_TEMPERATURE])
        self.number_adjust_humidity.update_device_value(p_adjust[mc.KEY_HUMIDITY])

    def _parse_humidity(self, p_humidity: dict):
        self._update_sensor(self.sensor_humidity, p_humidity[mc.KEY_LATEST])

    def _parse_ms100(self, p_ms100: dict):
        # typically called by SubDevice.parse_digest
        # when parsing Appliance.System.All
        self._parse_tempHum(p_ms100)

    def _parse_temperature(self, p_temperature: dict):
        self._update_sensor(self.sensor_temperature, p_temperature[mc.KEY_LATEST])

    def _parse_tempHum(self, p_temphum: dict):
        self._update_sensor(
            self.sensor_temperature, p_temphum[mc.KEY_LATESTTEMPERATURE]
        )
        self._update_sensor(self.sensor_humidity, p_temphum[mc.KEY_LATESTHUMIDITY])

    @override
    def _parse_togglex(self, p_togglex: dict):
        # avoid the base class creating a toggle entity
        # since we're pretty sure ms100 doesn't have one
        pass

    def _update_sensor(self, sensor: MLNumericSensor, device_value):
        # when a temp/hum reading changes we're smartly requesting
        # the adjust sooner than scheduled in case the change
        # was due to an adjustment
        if sensor.update_device_value(device_value):
            handler = self.ns_handlers[mn_h.Appliance_Hub_Sensor_Adjust]
            if handler.lastrequest < (self.hub.lastresponse - 30):
                handler.polling_epoch_next = 0.0


WELL_KNOWN_TYPE_MAP[mc.TYPE_MS100] = MS100SubDevice
# there's a new temp/hum sensor in town (MS100FH - see #303)
# and it is likely presented as tempHum in digest
# (need confirmation from device tracing though)
WELL_KNOWN_TYPE_MAP[mc.KEY_TEMPHUM] = MS100SubDevice


class MS130SubDevice(SensorSubDevice):
    __slots__ = (
        "sensor_humidity",
        "sensor_light",
        "sensor_temperature",
    )

    def __init__(self, hub: HubMixin, p_digest: dict, /):
        SensorSubDevice.__init__(self, hub, p_digest, mc.TYPE_MS130)
        self.sensor_humidity = MLHumiditySensor(self, self.id, device_scale=100)
        self.sensor_temperature = MLTemperatureSensor(self, self.id, device_scale=100)
        self.sensor_light = MLLightSensor(self, self.id)
        hub.setup_subid_handlers(
            self,
            mn_h.Appliance_Control_Sensor_LatestX,
            extra={"channel": 0, "data": ["light", "temp", "humi"]},
        )

    async def async_shutdown(self):
        await SensorSubDevice.async_shutdown(self)
        del self.sensor_light
        del self.sensor_temperature
        del self.sensor_humidity

    @override
    def _parse_deviceCfg(self, payload: "mt_h.SubIdPayload", /):
        """TODO: implement entities
        {
            "calibrateCfg": {
            "temp": 0,
            "humi": 0
            },
            "timeCfg": {
            "am": 2
            },
            "ms130Cfg": {
            "bl": {
                "bri": 2,
                "lv": 4,
                "sleep": 10
            }
            },
            "channel": 0,
            "subId": "1A00694ACBC7",
            "unitCfg": {
            "tempUnit": 1
            }
        }
        """
        pass

    def _parse_humidity(self, payload, /):
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
        self.sensor_humidity.update_device_value(payload[mc.KEY_LATEST])

    def _parse_temperature(self, payload, /):
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
        self.sensor_temperature.update_device_value(payload[mc.KEY_LATEST])

    def _parse_tempHumi(self, payload: dict, /):
        """parser for digest carried "tempHumi": {"latestTime": 1722219198, "temp": 1772, "humi": 711}"""
        self.sensor_temperature.update_device_value(payload[mc.KEY_TEMP])
        self.sensor_humidity.update_device_value(payload[mc.KEY_HUMI])

    @override
    def _parse_togglex(self, payload: dict, /):
        # avoid the base class creating a toggle entity
        # since we're pretty sure ms130 doesn't have one
        pass

    def _parse_latestx(self, payload: "mt_s.LatestXResponse_C", /):
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
        p_data = payload[mc.KEY_DATA]
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


class MS200SubDevice(SensorSubDevice):

    def __init__(self, hub: HubMixin, p_digest: dict, /):
        SensorSubDevice.__init__(self, hub, p_digest, mc.TYPE_MS200)
        binary_sensor_window = MLBinarySensor(
            self,
            self.id,
            str(MLBinarySensor.DeviceClass.WINDOW),
            device_class=MLBinarySensor.DeviceClass.WINDOW,
        )
        binary_sensor_window.key_value = mc.KEY_STATUS
        self._parse_doorWindow = binary_sensor_window._parse


WELL_KNOWN_TYPE_MAP[mc.TYPE_MS200] = MS200SubDevice
# doorWindow devices (mc.TYPE_MS200) are presented as
# mc.KEY_DOORWINDOW in digest(s) so we have to map that too
WELL_KNOWN_TYPE_MAP[mc.KEY_DOORWINDOW] = MS200SubDevice


class MS400SubDevice(SensorSubDevice):

    def __init__(self, hub: HubMixin, p_digest: dict, /):
        SensorSubDevice.__init__(self, hub, p_digest, mc.TYPE_MS400)
        binary_sensor_waterleak = MLBinarySensor(
            self,
            self.id,
            mc.KEY_WATERLEAK,
            device_class=MLBinarySensor.DeviceClass.SAFETY,
        )
        binary_sensor_waterleak.key_value = mc.KEY_LATESTWATERLEAK
        self._parse_waterLeak = binary_sensor_waterleak._parse


WELL_KNOWN_TYPE_MAP[mc.TYPE_MS400] = MS400SubDevice
# waterLeak devices (mc.TYPE_MS400) are presented as
# mc.KEY_WATERLEAK in digest(s) so we have to map that too
WELL_KNOWN_TYPE_MAP[mc.KEY_WATERLEAK] = MS400SubDevice


class MST100SubDevice(SensorSubDevice):

    class WateringDurationNumber(HubSubIdDeviceCfgMixin, MLConfigNumber):
        """Number to set watering duration."""

        key_group = "mstCfg"
        key_value = "dura"

        # HA core entity attributes:
        _attr_device_class = MLConfigNumber.DEVICE_CLASS_DURATION
        _attr_native_unit_of_measurement = MLConfigNumber.hac.UnitOfTime.SECONDS
        native_max_value = 86400  # 1 day max duration (no real info just guessing)
        native_min_value = 1

    class OnOffSwitch(HubSubIdChannelMixin, MLDeviceSwitch):
        """Switch to turn on/off watering."""

        ns = mn_h.Appliance_Control_Water
        native_on = 1
        native_off = 2

    if TYPE_CHECKING:
        number_duration: WateringDurationNumber
        switch_water_onoff: OnOffSwitch

        # Appliance.Config.DeviceCfg payload structure
        class DeviceCfg_mstCfg_calibration(TypedDict):
            waCon: int  # water consumption
            onoff: int
            lmTime: int

        class DeviceCfg_mstCfg(TypedDict):
            dura: int  # duration of watering in seconds
            wfm: int  # water flow measurement
            calibration: "MST100SubDevice.DeviceCfg_mstCfg_calibration"

        class DeviceCfg(mt_h.SubIdPayload):
            mstCfg: "MST100SubDevice.DeviceCfg_mstCfg"

        # Appliance.Control.Water payload structure
        class Water(mt_h.SubIdPayload):
            dura: NotRequired[int]  # duration in seconds
            onoff: int  # 1: on, 2: off

    __slots__ = (
        "number_duration",
        "switch_water_onoff",
    )

    def __init__(self, hub: HubMixin, p_digest: dict):
        SensorSubDevice.__init__(self, hub, p_digest, mc.TYPE_MST100)
        self.number_duration = MST100SubDevice.WateringDurationNumber(
            self,
            self.id,
            mc.KEY_DURATION,
            name="Watering duration",
        )
        self.switch_water_onoff = MST100SubDevice.OnOffSwitch(
            self,
            self.id,
            mc.KEY_ONOFF,
            name="Watering",
        )
        hub.setup_subid_handlers(self, mn_h.Appliance_Control_Water)

    async def async_shutdown(self):
        await SensorSubDevice.async_shutdown(self)
        del self.number_duration
        del self.switch_water_onoff

    @override
    def _parse_deviceCfg(self, payload: "DeviceCfg", /):
        self.number_duration._parse(payload)

    def _parse_water(self, payload: "Water", /):
        self.switch_water_onoff.update_device_value(payload[mc.KEY_ONOFF])

    @override
    def _parse_togglex(self, payload: dict, /):
        # avoid the base class creating a toggle entity
        # since we're pretty sure ms130 doesn't have one
        pass


WELL_KNOWN_TYPE_MAP[mc.TYPE_MST100] = MST100SubDevice
WELL_KNOWN_TYPE_MAP[mc.KEY_MST] = MST100SubDevice


def digest_init_hub(device: "HubMixin", digest, /) -> "DigestInitReturnType":

    # Check for unbinded subdevices which are 'still' in the device_registry
    registry_subdevices = {}
    for (
        device_entry
    ) in device.api.device_registry.devices.get_devices_for_config_entry_id(
        device.config_entry.entry_id
    ):
        # The caveat here is to detect if a subdev has been re-binded to
        # a different hub (so a different config_entry). We need to be sure
        # we're removing a surely unused device.
        # To be honest, I don't know about the 'integrity' enforced in DeviceRegistry
        # at any rate, a subdev is always unique in dev_reg since we use the
        # subdev "Id" as an identifier.
        if device_entry.via_device_id == device.device_registry_entry.id:
            # checking 'via_device_id' should be enough to ensure
            # the device hasn't been re-binded
            for identifiers in device_entry.identifiers:
                if identifiers[0] == mlc.DOMAIN:
                    registry_subdevices[identifiers[1]] = device_entry

    device.subdevices = {}
    for p_subdevice_digest in digest[mc.KEY_SUBDEVICE]:
        try:
            subdevice_id = p_subdevice_digest[mc.KEY_ID]
            if subdevice_id in device.subdevices:
                device.log_duplicated_subdevice(subdevice_id)
            else:
                device._subdevice_build(p_subdevice_digest)
                try:
                    del registry_subdevices[subdevice_id]
                except KeyError:
                    pass
        except Exception as exception:
            device.log_exception(device.WARNING, exception, "digest_init_hub")

    for subdevice_id, device_entry in registry_subdevices.items():
        device.create_issue(
            mlc.ISSUE_HUB_SUBDEVICE_REMOVED,
            subdevice_id,
            severity=device.IssueSeverity.WARNING,
            translation_placeholders={"device_name": device_entry.name},
        )

    return device._parse_hub, ()


POLLING_STRATEGY_CONF |= {
    mn_h.Appliance_Config_DeviceCfg: (
        mlc.PARAM_CONFIG_UPDATE_PERIOD,
        mlc.PARAM_CLOUDMQTT_UPDATE_PERIOD,
        mlc.PARAM_HEADER_SIZE,
        100,
        NamespaceHandler.async_poll_smart,
    ),
    mn_h.Appliance_Control_Sensor_LatestX: (
        mlc.PARAM_SENSOR_SLOW_UPDATE_PERIOD,
        mlc.PARAM_CLOUDMQTT_UPDATE_PERIOD,
        mlc.PARAM_HEADER_SIZE,
        220,
        NamespaceHandler.async_poll_smart,
    ),
    mn_h.Appliance_Control_Water: (
        0,
        0,
        mlc.PARAM_HEADER_SIZE,
        50,
        NamespaceHandler.async_poll_default,
    ),
    mn_h.Appliance_Hub_Battery: (
        3600,
        mlc.PARAM_CLOUDMQTT_UPDATE_PERIOD,
        mlc.PARAM_HEADER_SIZE,
        40,
        NamespaceHandler.async_poll_smart,
    ),
    mn_h.Appliance_Hub_Mts100_Adjust: (
        mlc.PARAM_CLOUDMQTT_UPDATE_PERIOD,
        mlc.PARAM_CLOUDMQTT_UPDATE_PERIOD,
        mlc.PARAM_HEADER_SIZE,
        40,
        NamespaceHandler.async_poll_smart,
    ),
    mn_h.Appliance_Hub_Mts100_All: (
        mlc.PARAM_HEARTBEAT_PERIOD,
        mlc.PARAM_CLOUDMQTT_UPDATE_PERIOD,
        mlc.PARAM_HEADER_SIZE,
        350,
        None,  # HubChunkedNamespaceHandler.async_poll_chunked
    ),
    mn_h.Appliance_Hub_Mts100_ScheduleB: (
        mlc.PARAM_CLOUDMQTT_UPDATE_PERIOD,
        mlc.PARAM_CLOUDMQTT_UPDATE_PERIOD,
        mlc.PARAM_HEADER_SIZE,
        500,
        None,  # HubChunkedNamespaceHandler.async_poll_chunked
    ),
    mn_h.Appliance_Hub_Sensor_Adjust: (
        mlc.PARAM_CLOUDMQTT_UPDATE_PERIOD,
        mlc.PARAM_CLOUDMQTT_UPDATE_PERIOD,
        mlc.PARAM_HEADER_SIZE,
        60,
        NamespaceHandler.async_poll_smart,
    ),
    mn_h.Appliance_Hub_Sensor_All: (
        mlc.PARAM_HEARTBEAT_PERIOD,
        mlc.PARAM_CLOUDMQTT_UPDATE_PERIOD,
        mlc.PARAM_HEADER_SIZE,
        250,
        None,  # HubChunkedNamespaceHandler.async_poll_chunked
    ),
    mn_h.Appliance_Hub_SubDevice_Beep: (
        0,
        mlc.PARAM_CLOUDMQTT_UPDATE_PERIOD,
        mlc.PARAM_HEADER_SIZE,
        35,
        NamespaceHandler.async_poll_default,
    ),
    mn_h.Appliance_Hub_SubDevice_Version: (
        0,
        mlc.PARAM_CLOUDMQTT_UPDATE_PERIOD,
        mlc.PARAM_HEADER_SIZE,
        55,
        NamespaceHandler.async_poll_once,
    ),
    mn_h.Appliance_Hub_ToggleX: (
        0,
        0,
        mlc.PARAM_HEADER_SIZE,
        35,
        NamespaceHandler.async_poll_default,
    ),
}
