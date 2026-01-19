from typing import TYPE_CHECKING, override

from aiohttp import payload

from ... import const as mlc
from ...binary_sensor import MLBinarySensor
from ...button import MLButton
from ...calendar import MtsSchedule
from ...climate import MtsClimate
from ...helpers import device as mld, entity as me
from ...helpers.namespaces import (
    POLLING_STRATEGY_CONF,
    NamespaceHandler,
    NamespaceParser,
    mc,
    mn,
)
from ...merossclient import get_productnameuuid, get_subdevice_key_digest, versiontuple
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
from ...switch import MLSwitch

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
        Unpack,
    )

    from ...helpers.device import Device, DigestInitReturnType, MerossMessage
    from ...helpers.entity import MLEntity
    from ...helpers.meross_profile import DeviceInfoExtType
    from ...helpers.mqtt_profile import MQTTProfile
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
        device_value: float,
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
            device_value=device_value,
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


class HubToggleX(MLSwitch):
    """Generic switch to map Appliance.Hub.ToggleX namespace."""

    ns = mn_h.Appliance_Hub_ToggleX
    ENTITY_KEY = mc.KEY_TOGGLEX


class HubBeep(MLSwitch):
    """Generic switch to map Appliance.Hub.SubDevice.Beep namespace."""

    ns = mn_h.Appliance_Hub_SubDevice_Beep
    ENTITY_KEY = f"{ns.slug}__{MLSwitch.key_value}"


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
        NamespaceHandler.__init__(self, device, ns, handler=self._handle_list)

    def channels_to_poll(self):
        # snapshot of subdevices to query (likely needed with all these asyncs)
        # TODO: remove when finished refactoring Hub parsing
        return tuple(
            self.parsers.keys() if self.parsers else self.device.subdevices.keys()
        )

    def _handle_list(self, message: "MerossMessage"):
        """Generalized Hub namespace dispatcher to subdevices.
        This code is being step-by-step migrated to be complient with
        the base NamespaceHandler implementation where possible.
        Migration will be done ns by ns so we'll have some ns with 'parsers'
        while some other will still work through this generalized handler."""
        hub = self.device
        subdevices = hub.subdevices
        subdevices_parsed = set()
        ns_key = self.ns.key
        key_channel = self.ns.key_channel
        for payload in message.payload[ns_key]:
            try:
                subdevice_id = payload[key_channel]
                if subdevice_id in subdevices_parsed:
                    hub.log_duplicated_subdevice(subdevice_id)
                    continue
                subdevices_parsed.add(subdevice_id)
                # try standard parser first (ns should be migrated to NamespaceHandler.register_parser)
                try:
                    self.parsers[subdevice_id](payload)
                    continue
                except KeyError as ke:
                    if ke.args[0] != subdevice_id:
                        raise
                # fallback to subdevice _hub_parse
                try:
                    subdevices[subdevice_id]._hub_parse(ns_key, payload)
                except KeyError as ke:
                    if ke.args[0] != subdevice_id:
                        raise
                    # force a rescan since we discovered a new subdevice
                    hub.handler_all.polling_epoch_next = 0.0

            except TypeError:
                # This could happen when the main payload is not a list of subdevices
                # and might indicate this namespace is likely devoted to general hub
                # commands/info (something like Appliance.Hub.*)
                self.handler = self._handle_undefined
                self._handle_undefined(message)
            except Exception as exception:
                self.handle_exception(exception, "_handle_list", payload)


class HubMixin(Device if TYPE_CHECKING else object):
    """
    Specialized Device for smart hub(s) like MSH300
    """

    DEVICE_TYPE = mlc.DeviceType.HUB
    NAMESPACES = mn.HUB_NAMESPACES

    DEFAULT_PLATFORMS = mld.Device.DEFAULT_PLATFORMS | {
        MLBinarySensor.PLATFORM: None,
        MLButton.PLATFORM: None,
        MtsSchedule.PLATFORM: None,
        MLConfigNumber.PLATFORM: None,
        MLNumericSensor.PLATFORM: None,
        MLSwitch.PLATFORM: None,
        MtsClimate.PLATFORM: None,
        MtsClimate.TrackSensorSelect.PLATFORM: None,
    }

    TRACE_ABILITY_EXCLUDE = mld.Device.TRACE_ABILITY_EXCLUDE + (
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
        # Usually called by _handle_Appliance_System_All as part of the digest parsing
        # Here we'll check the fresh subdevice list against the actual one and
        # eventually manage newly added subdevices or removed ones #119
        # telling the caller to persist the changed configuration (self.needsave)
        subdevices_actual = set(self.subdevices)
        for p_subdevice_digest in p_hub[mc.KEY_SUBDEVICE]:
            try:
                subdevice_id = p_subdevice_digest[mc.KEY_ID]
                try:
                    subdevice = self.subdevices[subdevice_id]
                    try:
                        subdevices_actual.remove(subdevice_id)
                    except KeyError:
                        # this shouldnt but happened in a trace (#331)
                        self.log_duplicated_subdevice(subdevice_id)
                        continue
                except KeyError:
                    subdevice = self._subdevice_build(p_subdevice_digest)
                    self.needsave = True

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

    def register_parser_subid(
        self,
        parser: "NamespaceParser",
        *nss: "Namespace",
        extra: "mt.MerossPayloadType" = {"channel": 0},
    ):
        ability = self.descriptor.ability
        for ns in nss:
            if ns not in ability:
                continue
            handler = self.get_handler(ns)
            handler.register_parser(parser)
            handler.polling_request_add_channel(parser.channel, extra)

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

    def _subdevice_build(self, p_subdevice: "dict[str, Any]", /) -> "SubDevice":
        # parses the subdevice payload in 'digest' to look for a well-known type
        # and builds accordingly
        try:
            key_digest = get_subdevice_key_digest(p_subdevice)
        except StopIteration:
            # the hub could report incomplete info anytime so beware.
            # this is true when subdevice is offline and hub has no recent info
            # we'll check our device registry for luck
            device_entry = self.api.device_registry.async_get_device(
                identifiers={(mlc.DOMAIN, p_subdevice[mc.KEY_ID])}
            )
            if not device_entry:
                raise Exception("Cannot identify subdevice type")
            model = device_entry.model
            if not model:
                raise Exception("Cannot identify subdevice type")
            model = model.lower()
            if model.startswith(mc.TYPE_MTS):
                return MTSSubDevice(self, p_subdevice, key_digest=model)

            return (
                subdevice_cls(self, p_subdevice)
                for subdevice_cls in SubDevice.DIGEST_MAP.values()
                if subdevice_cls.MODEL == model
            ).__next__()

        if key_digest.lower().startswith(mc.TYPE_MTS):
            return MTSSubDevice(self, p_subdevice, key_digest=key_digest)

        try:
            return SubDevice.DIGEST_MAP[key_digest](self, p_subdevice)
        except KeyError as ke:
            if ke.args[0] != key_digest:
                raise
            # build something anyway...
            return SensorSubDevice(self, p_subdevice, key_digest=key_digest)


class SubDeviceEntity(MLEntity if TYPE_CHECKING else object):
    """
    Mixin class for entities acting as a 'main' entity for a SubDevice.
    This is to allow the ns handling migration to NamespaceHandler
    """

    if TYPE_CHECKING:
        manager: "SubDevice"

    def parse_digest(self, payload: "mt_h.Digest_SubDevice", /):
        """Placeholder for digest parsing. This is installed automatically
        when the SubDevice has a 'main' entity so that when HubMixin calls
        subdevice.parse_digest(...) the call is forwarded to the main entity (self)"""
        self.manager._parse_online(payload)

    def _parse_all(self, payload: "mt_h.Sensor_All", /):
        """
        Heuristic parser for Appliance.Hub.Mts100.All or Appliance.Hub.Sensor.All
        when the SubDevice has a 'main' entity. This is automatically installed by
        the SubDevice machinery and used as a generic fit when no specific parser
        is defined in the (main)entity. It is a more refined version of the same code
        as found in SubDevice._parse_all.
        """
        self.manager._parse_online(payload[mc.KEY_ONLINE])
        if not self.manager.online:
            return

        def _parse_unknown(payload: dict, /):
            NamespaceParser._parse(self, payload)

        _excluded_keys = (mc.KEY_ID, mc.KEY_ONLINE)
        for _ in (
            getattr(self, f"_parse_{key}", _parse_unknown)(value)
            for key, value in payload.items()
            if (type(value) is dict) and (key not in _excluded_keys)
        ):
            pass

    # placeholders/stubs for common ns parsing. These are automatically
    # installed by SubDevice initialization
    def _parse_deviceCfg(self, payload, /):
        pass

    def _parse_togglex(self, payload: "mt_h.ToggleX", /):
        pass


class SubDevice(NamespaceParser, mld.BaseDevice):
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

    class BatterySensor(MLNumericSensor):
        ENTITY_KEY = mc.KEY_BATTERY
        ns = mn_h.Appliance_Hub_Battery
        _attr_device_class = MLNumericSensor.DeviceClass.BATTERY

    if TYPE_CHECKING:

        # BaseDevice overrides
        ns_handlers: Mapping[str, NamespaceHandler]

        # NamespaceParser overrides
        manager: Final[HubMixin]  # the owning hub
        channel: Final[str]

        # self
        class Args(TypedDict):
            key_digest: NotRequired[str]

        DIGEST_MAP: Final[dict[str, type["SubDevice"]]]

        MODEL: ClassVar[str | None]
        """SubDevice model string. This is the commercial name of the subdevice
        like MTS100, MS100, GS559, ...
        """
        KEY_DIGEST: ClassVar[str]
        """Key in the digest payload identifying this subdevice type. Historically
        this has often been the same as MODEL but no hard rule enforces this.
        positive examples are MS100 vs ms100, MTS150 vs mts150, ...
        negative examples are GS559 vs smokeAlarm, MS400 vs waterLeak, ...
        """
        NS_ALL: ClassVar[Namespace | None]
        """Indicates the 'all' namespace for this subdevice type, if any.
        Historically subdevices have been using 'Appliance.Hub.Sensor.All' or
        'Appliance.Hub.Mts100.All' namespaces to report their full state.
        Newer devices seem to be moving away from this pattern so this property
        might be None in some subdevice types."""
        MAIN_ENTITY_CLASS: ClassVar[type[SubDeviceEntity]]

        model: Final[str]

    DEVICE_TYPE = mlc.DeviceType.SUBDEVICE

    DIGEST_MAP = {}

    MODEL = None
    NS_ALL = None

    def __init_subclass__(cls):
        try:
            SubDevice.DIGEST_MAP[cls.KEY_DIGEST] = cls
        except AttributeError:
            # KEY_DIGEST not defined...we need to allow for intermediate classes
            pass

    __slots__ = (
        "async_request",
        "check_device_timezone",
        "ns_handlers",
        "manager",
        "channel",
        "model",
        "p_digest",
    )

    def __init__(self, hub: HubMixin, p_digest: dict, **kwargs: "Unpack[Args]"):
        # this is a very dirty trick/optimization to override some BaseDevice
        # properties/methods that just needs to be forwarded to the hub
        # this way we're short-circuiting that indirection
        self.async_request = hub.async_request
        self.check_device_timezone = hub.check_device_timezone
        self.ns_handlers = hub.ns_handlers
        # these properties are needed to be in place before base class init
        self.manager = hub
        self.channel = id = p_digest[mc.KEY_ID]
        self.model = model = self.MODEL or kwargs.get("key_digest") or "unknown"
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
        try:
            # create the 'main' entity if specified
            main_parser = self.MAIN_ENTITY_CLASS(self, self.id)
        except AttributeError as ae:
            if ae.name != "MAIN_ENTITY_CLASS":
                raise
            main_parser = self
        else:
            hub.register_parser_entity(main_parser)
            self.parse_digest = main_parser.parse_digest

        if self.NS_ALL:
            hub.register_parser(main_parser, self.NS_ALL)
        hub.register_parser_ex(
            main_parser,
            mn_h.Appliance_Config_DeviceCfg,
            mn_h.Appliance_Hub_ToggleX,
        )
        hub.register_parser_entity(SubDevice.BatterySensor(self, self.id))
        hub.register_parser_ex(
            self,
            mn_h.Appliance_Hub_Exception,
            mn_h.Appliance_Hub_Online,
            mn_h.Appliance_Hub_SubDevice_Beep,
            mn_h.Appliance_Hub_SubDevice_Version,
        )

        hub.remove_issue(mlc.ISSUE_HUB_SUBDEVICE_REMOVED, id)

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
        return f"{self.manager.id}_{entity.id}"

    # interface: BaseDevice
    @override
    async def async_shutdown(self):
        await NamespaceParser.async_shutdown(self)
        await mld.BaseDevice.async_shutdown(self)
        del self.check_device_timezone
        del self.async_request
        del self.ns_handlers
        # brutal trick to remove references to sensors _parse methods
        # should they exist (being installed at runtime)
        for _parse_method in [k for k in self.__dict__ if k.startswith("_parse_")]:
            delattr(self, _parse_method)

    @override
    def get_upgrade_payload(self, /) -> "mt_c.Upgrade":
        # start from hub upgrade payload (eventually)
        upgrade_payload = self.manager.get_upgrade_payload()
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
        return self.manager.tz

    @override
    def _get_internal_name(self) -> str:
        return get_productnameuuid(self.model, self.id)

    @override
    def _set_online(self):
        mld.BaseDevice._set_online(self)
        # force a re-poll even on MQTT
        if self.NS_ALL:
            self.ns_handlers[self.NS_ALL].polling_epoch_next = 0.0

    # interface: self
    def update_sub_device_info(self, sub_device_info: "SubDeviceInfoType", /):
        name = sub_device_info.get(mc.KEY_SUBDEVICENAME) or self._get_internal_name()
        if name != self.device_registry_entry.name:
            self.api.device_registry.async_update_device(
                self.device_registry_entry.id, name=name
            )

    def _hub_parse(self, key: str, payload: dict, /):
        """Legacy subdevice parsing system. This will be eventually removed
        in favor of NamespaceHandler/NamespaceParser system.
        This method tries to call a specialized parser for the given key.
        It is actually called by HubMixin when no specialized NamespaceHandler
        is defined for the given namespace."""
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
            # TODO: reconcile this code with the similar implementation in
            # NamespaceHandler for diagnostic sensors dumping
            if not self.manager.create_diagnostic_entities:
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
                        mc.KEY_SUBID,
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
            # other times it's different. "ms100" and "mtsxxx" series
            # valves carries an "ms100" ("mtsxxx") payload in digest and
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
                try:
                    self.ns_handlers[mn_h.Appliance_Hub_ToggleX].parsers[self.id](
                        payload
                    )
                except KeyError:
                    self._parse_togglex(payload)

    def _parse_all(self, payload: dict, /):
        # parser for NS_ALL if no MAIN_ENTITY_CLASS is defined:
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

    def _parse_deviceCfg(self, payload: "mt_h.SubIdPayload", /):
        pass

    def _parse_togglex(self, payload: "mt_h.ToggleX", /):
        # This handler is installed as a fallback when no specialized
        # parser is defined for togglex ns during SubDevice init.
        # Here we just swap-in a HubToggleX entity so that it'll be
        # self-managing from now on.
        self.ns_handlers[mn_h.Appliance_Hub_ToggleX].swap_parsers(
            self,
            HubToggleX(
                self,
                self.id,
                device_value=payload[mc.KEY_ONOFF],
            ),
        )

    def _parse_exception(self, payload, /):
        """{"id": "00000000", "code": 5061}"""
        # TODO: code 5061 seems related to loss of connectivity between the hub
        # and the device. We might put up a binary sensor.
        self.log(self.WARNING, "Received exception payload: %s", str(payload))

    def _parse_online(self, payload: "mt_h._Online", /):
        if payload[mc.KEY_STATUS] == mc.STATUS_ONLINE:
            if not self.online:
                self._set_online()
        else:
            if self.online:
                self._set_offline()

    def _parse_beep(self, payload: "mt_h.SubDevice_Beep", /):
        self.ns_handlers[mn_h.Appliance_Hub_SubDevice_Beep].swap_parsers(
            self,
            HubBeep(
                self,
                self.id,
                name="Beep alarm",
                device_value=payload[mc.KEY_ONOFF],
            ),
        )

    def _parse_version(self, payload: "mt_h.SubDevice_Version", /):
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
    KEY_DIGEST = mc.TYPE_MTS  # just for syntax

    # TODO: this lame import is to be later refactored to use lazy imports
    # whenever subdevices appear in the code.
    from .mts100 import Mts100Climate as MAIN_ENTITY_CLASS


class SensorSubDevice(SubDevice):
    """
    Common class for any sensor-like subdevice (ms100, ms120, ms130, ...).
    """

    NS_ALL = mn_h.Appliance_Hub_Sensor_All


class GS559SubDevice(SensorSubDevice):

    class SmokeAlarmSensor(SubDeviceEntity, MLEnumSensor):
        if TYPE_CHECKING:
            STATUS_MAP: Final
            MUTE_MAP: Final
            STATUS_ALARM: Final[set[int]]
            STATUS_ERROR: Final[set[int]]
            STATUS_MUTED: Final[set[int]]

        ENTITY_KEY = mc.KEY_STATUS
        ns = mn_h.Appliance_Hub_Sensor_Smoke
        key_value = mc.KEY_STATUS

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
            "sensor_interConn",
        )

        def __init__(self, manager: SubDevice, channel):
            self.device_value = (
                None  # TODO: move to MLEnumSensor together with mapping capability
            )
            super().__init__(
                manager, channel, mc.KEY_STATUS, translation_key="smoke_alarm_status"
            )
            self.binary_sensor_alarm = MLBinarySensor(
                manager,
                channel,
                mc.KEY_ALARM,
                device_class=MLBinarySensor.DeviceClass.SAFETY,
            )
            self.binary_sensor_error = MLBinarySensor(
                manager,
                channel,
                mc.KEY_ERROR,
                device_class=MLBinarySensor.DeviceClass.PROBLEM,
            )
            self.binary_sensor_muted = MLBinarySensor(manager, channel, "muted")
            self.sensor_interConn = MLEnumSensor(manager, channel, mc.KEY_INTERCONN)
            MLButton(manager, channel, "button_mute", self.async_mute, name="Mute")
            MLButton(manager, channel, "button_test", self.async_test, name="Test")

        def _parse_smokeAlarm(self, payload: dict, /):
            self.device_value = value = payload[mc.KEY_STATUS]
            self.update_native_value(self.STATUS_MAP.get(value, value))
            self.binary_sensor_alarm.update_native_value(value in self.STATUS_ALARM)
            self.binary_sensor_error.update_native_value(value in self.STATUS_ERROR)
            self.binary_sensor_muted.update_native_value(value in self.STATUS_MUTED)
            try:
                self.sensor_interConn.update_native_value(payload[mc.KEY_INTERCONN])
            except KeyError:
                pass

        async def async_shutdown(self):
            await super().async_shutdown()
            del self.binary_sensor_muted
            del self.binary_sensor_error
            del self.binary_sensor_alarm
            del self.sensor_interConn

        async def async_mute(self, /):
            try:
                await self.async_request_payload(
                    {
                        mc.KEY_STATUS: self.MUTE_MAP.get(self.device_value, 170),
                    }
                )
            except KeyError as e:
                # in case the state is not present in the MUTE_MAP (i.e. not mutable)
                self.log_exception(self.DEBUG, e, "trying to send mute command")

        async def async_test(self, /):
            await self.async_request_payload({mc.KEY_STATUS: 23})

    MODEL = mc.TYPE_GS559
    KEY_DIGEST = mc.KEY_SMOKEALARM
    MAIN_ENTITY_CLASS = SmokeAlarmSensor


class MS100SubDevice(SensorSubDevice):

    class MS100Sensor(SubDeviceEntity, MLTemperatureSensor):

        ns = mn_h.Appliance_Hub_Sensor_TempHum

        _attr_device_scale = 10

        __slots__ = ("sensor_humidity",)

        def __init__(self, manager: SubDevice, channel: str):
            super().__init__(manager, channel)
            self.sensor_humidity = MLHumiditySensor(manager, channel)
            manager.manager.register_parser_ex(
                self, mn_h.Appliance_Hub_Sensor_Adjust, mn_h.Appliance_Hub_Sensor_Latest
            )

        async def async_shutdown(self):
            await super().async_shutdown()
            del self.sensor_humidity

        @override
        def parse_digest(self, payload: "mt_h.Digest_ms100", /):
            manager = self.manager
            manager._parse_online(payload)
            if manager.online:
                digest = payload[manager.KEY_DIGEST]
                self._update_sensors(
                    digest[mc.KEY_LATESTTEMPERATURE], digest[mc.KEY_LATESTHUMIDITY]
                )

        def _parse_adjust(self, payload: "mt_h.Sensor_Adjust"):
            manager = self.manager
            manager.ns_handlers[mn_h.Appliance_Hub_Sensor_Adjust].swap_parsers(
                self,
                HubSensorAdjustNumber(
                    manager,
                    mc.KEY_TEMPERATURE,
                    HubSensorAdjustNumber.DeviceClass.TEMPERATURE,
                    -5,
                    5,
                    0.1,
                    payload[mc.KEY_TEMPERATURE],
                ),
                HubSensorAdjustNumber(
                    manager,
                    mc.KEY_HUMIDITY,
                    HubSensorAdjustNumber.DeviceClass.HUMIDITY,
                    -20,
                    20,
                    1,
                    payload[mc.KEY_HUMIDITY],
                ),
            )

        def _parse_all(self, payload: "mt_h.Sensor_All_ms100", /):
            self.manager._parse_online(payload[mc.KEY_ONLINE])
            if self.available:
                self._update_sensors(
                    payload[mc.KEY_TEMPERATURE][mc.KEY_LATEST],
                    payload[mc.KEY_HUMIDITY][mc.KEY_LATEST],
                )

        def _parse_latest(self, payload: "mt_h.Sensor_Latest"):
            self._update_sensors(
                payload[mc.KEY_TEMPERATURE]["sample"],
                payload[mc.KEY_HUMIDITY]["sample"],
            )

        def _parse_tempHum(self, payload: "mt_h.Sensor_TempHum"):
            self._update_sensors(
                payload[mc.KEY_LATESTTEMPERATURE], payload[mc.KEY_LATESTHUMIDITY]
            )

        def _update_sensors(self, temperature: int, humidity: int):
            self.update_device_value(temperature)
            self.sensor_humidity.update_device_value(humidity)

        def _update_sensors_adjust(self, temperature: int, humidity: int):
            # when a temp/hum reading changes we're smartly requesting
            # the adjust sooner than scheduled in case the change
            # was due to an adjustment. This method is dynamically installed
            # by _parse_adjust when we have confirtmation that ns_adjust is
            # delivering for this device.
            _poll_adjust = bool(self.update_device_value(temperature))
            _poll_adjust |= bool(self.sensor_humidity.update_device_value(humidity))
            if _poll_adjust:
                handler = self.manager.ns_handlers[mn_h.Appliance_Hub_Sensor_Adjust]
                if handler.lastrequest < (self.manager.manager.lastresponse - 30):
                    handler.polling_epoch_next = 0.0

    MODEL = mc.TYPE_MS100
    KEY_DIGEST = mc.TYPE_MS100
    MAIN_ENTITY_CLASS = MS100Sensor


class MS100FSubDevice(MS100SubDevice):
    """A variant of MS100SubDevice for the MS100FH device"""

    MODEL = mc.TYPE_MS100F
    KEY_DIGEST = mc.KEY_TEMPHUM


class MS130SubDevice(MS100SubDevice):

    class MS130Sensor(MS100SubDevice.MS100Sensor):

        _attr_device_scale = 100

        __slots__ = ("sensor_light",)

        def __init__(self, manager: SubDevice, channel):
            super().__init__(manager, channel)
            self.sensor_light = MLLightSensor(manager, channel)
            manager.manager.register_parser_subid(
                self,
                mn_h.Appliance_Control_Sensor_LatestX,
                extra={"channel": 0, "data": ["light", "temp", "humi"]},
            )

        async def async_shutdown(self):
            await super().async_shutdown()
            del self.sensor_light

        @override
        def parse_digest(self, payload: "mt_h.Digest_ms130", /):
            manager = self.manager
            manager._parse_online(payload)
            if manager.online:
                digest = payload[mc.KEY_TEMPHUMI]
                self._update_sensors(digest[mc.KEY_TEMP], digest[mc.KEY_HUMI])

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
                "tempUnit": 1  # 1 °C - 2 °F
                }
            }
            """
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
            entity: MLNumericSensor
            for key, entity in {
                mc.KEY_TEMP: self,
                mc.KEY_HUMI: self.sensor_humidity,
                mc.KEY_LIGHT: self.sensor_light,
            }.items():
                try:
                    entity.update_device_value(p_data[key][0][mc.KEY_VALUE])
                except:
                    pass

    MODEL = mc.TYPE_MS130
    KEY_DIGEST = mc.KEY_TEMPHUMI
    MAIN_ENTITY_CLASS = MS130Sensor


class MS200SubDevice(SensorSubDevice):

    class DoorWindowSensor(SubDeviceEntity, MLBinarySensor):
        ENTITY_KEY = MLBinarySensor.DeviceClass.WINDOW
        ns = mn_h.Appliance_Hub_Sensor_DoorWindow
        key_value = mc.KEY_STATUS

        _attr_device_class = MLBinarySensor.DeviceClass.WINDOW

        _parse_doorWindow = MLBinarySensor._parse

    MODEL = mc.TYPE_MS200
    KEY_DIGEST = mc.KEY_DOORWINDOW
    MAIN_ENTITY_CLASS = DoorWindowSensor


class MS400SubDevice(SensorSubDevice):

    class WaterLeakSensor(SubDeviceEntity, MLBinarySensor):
        ENTITY_KEY = mc.KEY_WATERLEAK
        ns = mn_h.Appliance_Hub_Sensor_WaterLeak
        key_value = mc.KEY_LATESTWATERLEAK

        _attr_device_class = MLBinarySensor.DeviceClass.SAFETY

    MODEL = mc.TYPE_MS400
    KEY_DIGEST = mc.KEY_WATERLEAK
    MAIN_ENTITY_CLASS = WaterLeakSensor


class MST100SubDevice(SubDevice):

    if TYPE_CHECKING:
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

    class WateringDurationNumber(HubSubIdDeviceCfgMixin, MLConfigNumber):
        """Number to set watering duration."""

        ENTITY_KEY = mc.KEY_DURATION
        key_group = "mstCfg"
        key_value = "dura"

        # HA core entity attributes:
        _attr_device_class = MLConfigNumber.DEVICE_CLASS_DURATION
        _attr_native_unit_of_measurement = MLConfigNumber.hac.UnitOfTime.SECONDS
        native_max_value = 86400  # 1 day max duration (no real info just guessing)
        native_min_value = 1

    class MstSwitch(SubDeviceEntity, HubSubIdChannelMixin, MLSwitch):
        """Switch to turn on/off the MST valve."""

        ENTITY_KEY = mc.KEY_ONOFF
        ns = mn_h.Appliance_Control_Water
        native_on = 1
        native_off = 2

        # TODO: define _attr_name in MLEntity base class

        __slots__ = ("number_duration",)

        def __init__(self, manager, channel):
            super().__init__(manager, channel, mc.KEY_ONOFF, name="Watering")
            self.number_duration = MST100SubDevice.WateringDurationNumber(
                manager,
                channel,
                mc.KEY_DURATION,
                name="Watering duration",
            )

        async def async_shutdown(self):
            await super().async_shutdown()
            del self.number_duration

        @override
        def _parse_deviceCfg(self, payload: "MST100SubDevice.DeviceCfg", /):
            self.number_duration._parse(payload)

        def _parse_water(self, payload: "MST100SubDevice.Water", /):
            self.update_device_value(payload[mc.KEY_ONOFF])

    MODEL = mc.TYPE_MST100
    KEY_DIGEST = mc.KEY_MST
    MAIN_ENTITY_CLASS = MstSwitch


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
                continue

            device._subdevice_build(p_subdevice_digest)
            try:
                del registry_subdevices[subdevice_id]
            except KeyError:
                pass
        except Exception as exception:
            device.log_exception(
                device.WARNING,
                exception,
                "digest_init_hub (payload: %s)",
                device.loggable_dict_str(p_subdevice_digest),
            )

    for subdevice_id, device_entry in registry_subdevices.items():
        device.create_issue(
            mlc.ISSUE_HUB_SUBDEVICE_REMOVED,
            subdevice_id,
            severity=device.IssueSeverity.WARNING,
            translation_placeholders={"device_name": device_entry.name},
        )

    return device._parse_hub, ()


POLLING_STRATEGY_CONF.update(
    {
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
            NamespaceHandler.async_poll_chunked,
        ),
        mn_h.Appliance_Hub_Mts100_ScheduleB: (
            mlc.PARAM_CLOUDMQTT_UPDATE_PERIOD,
            mlc.PARAM_CLOUDMQTT_UPDATE_PERIOD,
            mlc.PARAM_HEADER_SIZE,
            500,
            NamespaceHandler.async_poll_chunked,
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
            NamespaceHandler.async_poll_chunked,
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
)
