from dataclasses import dataclass
from typing import TYPE_CHECKING, override

from ... import const as mlc
from ...button import Button
from ...helpers import device as mld, entity as mle
from ...helpers.namespaces import NamespaceHandler
from ...merossclient import (
    async_import_module,
    device,
    get_productname,
    get_subdevice_key_digest,
)
from ...merossclient.protocol import const as mc, namespaces as mn
from ...merossclient.protocol.namespaces import hub as mn_h
from ...sensor import SensorParser
from ...switch import SwitchParser

if TYPE_CHECKING:
    from typing import (
        Callable,
        ClassVar,
        Final,
        Iterable,
        Mapping,
        NotRequired,
        Self,
        TypedDict,
        Unpack,
    )

    from homeassistant.helpers import device_registry as dr

    from ...helpers.device import MerossMessage
    from ...helpers.meross_profile import DeviceInfoExtType
    from ...helpers.mqtt_profile import MQTTProfile
    from ...merossclient.cloudapi import SubDeviceInfoType
    from ...merossclient.protocol import types as mt
    from ...merossclient.protocol.namespaces import Namespace


class HubSubIdChannelMixin(mle.ValueParser if TYPE_CHECKING else object):
    """
    Mixin implementation for protocol method 'SET' on hub entities/namespaces backed by a
    subId/channel indexing key pair.
    TODO: migrate subdevice entities channel indexing (needs registry migration).
    Right now we're fixing channel to 0 since hub subdevices seems to not discriminate channels.
    Implementing full support for varying channels per subdevice would need some rework on subdevice
    entities indexing (id and unique_id)and management.
    """

    @override
    async def async_request_value(self, device_value, /):
        await self.async_request_payload(
            {mc.KEY_CHANNEL: 0, self.key_value: device_value}
        )
        self.update_device_value(device_value)


class HubSubIdDeviceCfgMixin(mle.ParserEntity.NamespaceGroupValue):
    """
    Mixin implementation for protocol method 'SET' on 'Appliance.Config.DeviceCfg'.
    """

    init_ns = mn_h.Appliance_Config_DeviceCfg

    @override
    async def async_request_value(self, device_value, /):
        await self.async_request_payload(
            {mc.KEY_CHANNEL: 0, self.key_group: {self.key_value: device_value}}
        )
        self.update_device_value(device_value)


class ApplianceDigestHubHandler(NamespaceHandler):
    """
    Specialized handler for the 'Appliance.Digest.Hub' namespace which is the 'official' way to report
    the hub digest carrying the subdevice list. This handler is used when the namespace is present in
    the device abilities and it is responsible to parse the digest and manage subdevices list
    accordingly (add/remove).
    """

    if TYPE_CHECKING:
        parent: "Hub"  # type: ignore[override]
        digest: "mt.hub.Digest"

    def __init__(self, ns: "Namespace", device: "Hub", /):
        NamespaceHandler.__init__(self, ns, device)
        # We need to make the 'parsers' attribute evaluate to True else
        # the mechanics for digest parsing/polling in base classes will not dispatch to this.
        self.parsers = device.subdevices  # type: ignore

    def _handle(self, message: "MerossMessage"):
        self.digest = message.payload[mc.KEY_HUB]
        self.parent.parse_digest(self.digest)

    def parse_digest(self, digest: "mt.hub.Digest", /):
        self.parent.parse_digest(digest)


def namespace_init_appliance_hub_pairsubdev(ns: "Namespace", device: "Hub", /):
    Button(
        None,
        device,
        device.async_pairsubdev,
        name="Pair Subdevice",
        device_class=Button.DeviceClass.IDENTIFY,
        entity_category=Button.EntityCategory.CONFIG,
    )


class Hub(mld.Device):
    """
    Specialized Device for smart hub(s) like MSH300
    """

    if TYPE_CHECKING:
        subdevices: dict[str, "SubDevice"]

    NAMESPACES = mn.HUB_NAMESPACES

    # we can safely rewrite base class __dict__ here
    # since, once configured, these are no harm when used by a generic non-hub device.
    mld.Device.NAMESPACE_INIT.update(
        {
            mn_h.Appliance_Digest_Hub: ApplianceDigestHubHandler,
            mn_h.Appliance_Hub_PairSubDev: namespace_init_appliance_hub_pairsubdev,
        }
    )
    mld.Device.NAMESPACE_IGNORE = mld.Device.NAMESPACE_IGNORE + (
        mn_h.Appliance_Hub_ExtraInfo,
        mn_h.Appliance_Hub_SubdeviceList,
    )
    mld.Device.TRACE_ABILITY_EXCLUDE = mld.Device.TRACE_ABILITY_EXCLUDE + (
        mn_h.Appliance_Hub_Exception,
        mn_h.Appliance_Hub_Report,
    )

    # We cannot define slots in this subclass because of how we install
    # this at runtime over a standard Device class instance (already created)
    # Any slot should then be declared in Device.
    # __slots__ = ("subdevices",)

    @override
    async def async_init(self, /):
        # This is a trick to dynamically mixin the Hub capabilities
        # into the device instance. Historically we were mixing Hub
        # as a subclass of the device class at ConfigEntry load time in ComponentApi
        # but this new approach requires less coding.
        # BEWARE: this works if we don't need special __init__ logic in Hub
        # because the instance is already initialized here and we're called in the
        # context of Device.async_init method. This happens rather soon but surely
        # after Device.__init__
        # Also, the base Device class mixed-in might be different at test time since it gets mocked
        # so we have to dynamically create a new class on the fly. and ensure Hub is not
        # overriding any mocked attribute (see test.helpers.ConfigEntryMocker.ManagerMock)
        self.subdevices = {}
        # Check for unbinded subdevices which are 'still' in the device_registry
        registry_subdevices: dict[str, "mld.dr.DeviceEntry"] = {}
        for (
            device_entry
        ) in self.parent.device_registry.devices.get_devices_for_config_entry_id(
            self.config_entry.entry_id
        ):
            # The caveat here is to detect if a subdev has been re-binded to
            # a different hub (so a different config_entry). We need to be sure
            # we're removing a surely unused device.
            # To be honest, I don't know about the 'integrity' enforced in DeviceRegistry
            # at any rate, a subdev is always unique in dev_reg since we use the
            # subdev "Id" as an identifier.
            if device_entry.via_device_id == self.device_entry.id:
                # checking 'via_device_id' should be enough to ensure
                # the device hasn't been re-binded
                for identifiers in device_entry.identifiers:
                    if identifiers[0] == mlc.DOMAIN:
                        registry_subdevices[identifiers[1]] = device_entry

        if mn_h.Appliance_Digest_Hub not in self.descriptor.ability:
            # We don't have the 'official' digest carrying ns so we have to intercept
            # ns_all in order to have a chance to parse the hub digest
            self.handler_all.handler = self._handle_Appliance_System_All

        for p_subdevice_digest in self.descriptor.digest[mc.KEY_HUB][mc.KEY_SUBDEVICE]:
            try:
                subdevice_id = p_subdevice_digest[mc.KEY_ID]
                if subdevice_id in self.subdevices:
                    self.subdevices[subdevice_id].log_duplicated()
                    continue

                await SubDevice.async_build(self, p_subdevice_digest)
                try:
                    del registry_subdevices[subdevice_id]
                except KeyError:
                    pass
            except Exception as exception:
                self.log_exception(
                    self.WARNING,
                    exception,
                    "hub digest scan (subdevice digest: %s)",
                    _payload=p_subdevice_digest,
                )

        for subdevice_id, device_entry in registry_subdevices.items():
            self.create_issue(
                mlc.ISSUE_HUB_SUBDEVICE_REMOVED,
                subdevice_id,
                severity=self.IssueSeverity.WARNING,
                translation_placeholders={
                    "device_name": device_entry.name or "unknown device"
                },
            )

        await super().async_init()

    @override
    def get_device_entry(self, channel, /):
        if not channel:
            return self.device_entry
        return self.subdevices[channel].device_entry

    @override
    def update_device_info(
        self, device_info: "DeviceInfoExtType", profile: "MQTTProfile", /
    ):
        super().update_device_info(device_info, profile)
        # propagate device info to subdevices
        for sub_device_info in device_info.get("__subDeviceInfo", []):
            try:
                subdevice = self.subdevices[sub_device_info["subDeviceId"]]
            except KeyError:
                continue
            else:
                name = sub_device_info.get(mc.KEY_SUBDEVICENAME) or get_productname(
                    subdevice.model
                )
                if name != subdevice.device_entry.name:
                    self.parent.device_registry.async_update_device(
                        subdevice.device_entry.id, name=name
                    )

    # interface: self
    async def async_pairsubdev(self, /):
        await self.async_request(*mn_h.Appliance_Hub_PairSubDev.request_set())

    def parse_digest(self, p_hub: "mt.hub.Digest", /):
        # Usually called by _handle_Appliance_System_All as part of the digest parsing
        # Here we'll check the fresh subdevice list against the actual one and
        # eventually manage newly added subdevices or removed ones #119
        subdevices = dict(self.subdevices)
        for p_subdevice_digest in p_hub[mc.KEY_SUBDEVICE]:
            try:
                subdevice_id = p_subdevice_digest[mc.KEY_ID]
                try:
                    subdevice = subdevices.pop(subdevice_id)
                except KeyError:
                    if subdevice_id in self.subdevices:
                        # this shouldnt but happened in a trace (#331)
                        self.subdevices[subdevice_id].log_duplicated()
                    else:  # full reload to cleanly add a new subdevice
                        # TODO: we could likely add on the fly (just register newly added entities)
                        # without reloading but we still should save the new digest in config entry.
                        self.schedule_entry_update(True)
                else:
                    subdevice.parse_digest(p_subdevice_digest)
            except Exception as exception:
                self.log_exception(
                    self.WARNING, exception, "parse_digest(%s)", p_subdevice_digest
                )

        if subdevices:
            # now we're left with non-existent (removed) subdevices
            self.schedule_entry_update(False)
            for subdevice in subdevices.values():
                self.log(
                    self.WARNING,
                    "%s (id:%s) unregistered from hub",
                    subdevice.display_name,
                    subdevice.id,
                )
                self.create_issue(
                    mlc.ISSUE_HUB_SUBDEVICE_REMOVED,
                    subdevice.id,
                    severity=self.IssueSeverity.WARNING,
                    translation_placeholders={"device_name": subdevice.display_name},
                )
                self.create_task(
                    subdevice.async_subdevice_shutdown(),
                    f"{subdevice.__class__.__name__}.async_subdevice_shutdown()",
                    eager_start=True,
                )

    def _handle_Appliance_System_All(self, message: MerossMessage, /):
        super()._handle_Appliance_System_All(message)
        self.parse_digest(self.descriptor.digest[mc.KEY_HUB])


class SubDevice(mld.BaseDevice, device.SubDevice, mle.ParserEntity):
    """
    Class for a physical subdevice registered with a Hub device.
    This class acts as the 'main entity' behavior (thermostat for mts valves or a in
    general a sensor for the msXXX line of subdevices like ms100 or so).
    """

    if TYPE_CHECKING:
        NS_HUB: ClassVar[Iterable[Namespace]]
        """Namespaces to be registered for this subdevice."""
        parent: Final[Hub]  # type: ignore[override]
        channel: Final[str]  # type: ignore[override]
        """SubDevice Identifier."""
        device_entry: Final[dr.DeviceEntry]  # type: ignore[override]
        model: Final[str]

    @dataclass(frozen=True, eq=False, slots=True)
    class DigestDef:
        module_name: str
        class_name: str
        model: str

    DIGEST_MAP = {
        mc.KEY_SMOKEALARM: DigestDef("ms", mc.TYPE_GS559, mc.TYPE_GS559),
        mc.TYPE_MS100: DigestDef("ms", mc.TYPE_MS100, mc.TYPE_MS100),
        mc.KEY_TEMPHUM: DigestDef("ms", mc.TYPE_MS100, mc.TYPE_MS100F),
        mc.KEY_TEMPHUMI: DigestDef("ms", mc.TYPE_MS130, mc.TYPE_MS130),
        mc.KEY_DOORWINDOW: DigestDef("ms", mc.TYPE_MS200, mc.TYPE_MS200),
        mc.KEY_WATERLEAK: DigestDef("ms", mc.TYPE_MS400, mc.TYPE_MS400),
        mc.TYPE_MST100: DigestDef(mc.KEY_MST, mc.TYPE_MST100, mc.TYPE_MST100),
        mc.TYPE_MST200: DigestDef(mc.KEY_MST, mc.TYPE_MST200, mc.TYPE_MST200),
        mc.TYPE_MTS100: DigestDef("mts", mc.TYPE_MTS100V3, mc.TYPE_MTS100),
        mc.TYPE_MTS100V3: DigestDef("mts", mc.TYPE_MTS100V3, mc.TYPE_MTS100V3),
        mc.TYPE_MTS150: DigestDef("mts", mc.TYPE_MTS150, mc.TYPE_MTS150),
        mc.TYPE_MTS150P: DigestDef("mts", mc.TYPE_MTS150, mc.TYPE_MTS150P),
    }

    NS_HUB = (
        mn_h.Appliance_Hub_Exception,
        mn_h.Appliance_Hub_Online,
        mn_h.Appliance_Hub_SubDevice_Beep,
        mn_h.Appliance_Hub_SubDevice_Version,
    )

    # This is used by SubDevice init together with NS_HUB for automatic ns registrations.
    # It is likely to be overriden in actual implementations but the default
    # is set just for the case where we instantiate a SubDevice without specialization.
    init_ns = mn_h.Appliance_Hub_ToggleX

    __SLOTS__ = (
        "key_digest",
        "model",
    )

    @staticmethod
    async def async_build(hub: Hub, digest_subdevice: "mt.hub.Digest_SubDevice", /):
        # parses the subdevice payload in 'digest' to look for a well-known type
        # and builds accordingly
        subid = digest_subdevice[mc.KEY_ID]
        hub.remove_issue(mlc.ISSUE_HUB_SUBDEVICE_REMOVED, subid)
        try:
            try:
                key_digest = get_subdevice_key_digest(digest_subdevice)
            except StopIteration:
                # the hub could report incomplete info anytime so beware.
                # this is true when subdevice is offline and hub has no recent info
                # we'll check our device registry for luck.
                # The relationship between model and key_digest is not 1:1 so
                # we need to accurately map the correct class.
                device_entry = hub.parent.device_registry.async_get_device(
                    identifiers={(mlc.DOMAIN, subid)}
                )
                assert device_entry, (
                    "Device entry not found for subdevice with id %s" % subid
                )
                model = device_entry.model
                assert model
                model = model.lower()
                key_digest, digest_init_def = (
                    (_key, _def)
                    for _key, _def in SubDevice.DIGEST_MAP.items()
                    if _def.model == model
                ).__next__()
            else:
                # dirty patch looking for 'better times'
                if key_digest == mc.KEY_MST:
                    if "waDet" in digest_subdevice[key_digest]:  # type: ignore
                        digest_init_def = SubDevice.DIGEST_MAP[mc.TYPE_MST200]
                    else:
                        digest_init_def = SubDevice.DIGEST_MAP[mc.TYPE_MST100]
                else:
                    digest_init_def = SubDevice.DIGEST_MAP[key_digest]

            subdev_module = await async_import_module(
                f".devices.hub.{digest_init_def.module_name}",
                hub.NAMESPACE_INIT_PACKAGE,
            )
            subdevice_class = getattr(subdev_module, digest_init_def.class_name)
            model = digest_init_def.model
            if TYPE_CHECKING:
                assert issubclass(subdevice_class, SubDevice)
            return subdevice_class(subid, hub, key_digest, model)
        except Exception as e:
            hub.log_exception(
                hub.WARNING,
                e,
                "detecting subdevice model for %s. Proceeding with fall-back",
                _payload=digest_subdevice,
            )
            return SubDevice(subid, hub, key_digest, key_digest)

    def __init_subclass__(cls):
        super().__init_subclass__()
        if slots := cls._calc_slots():
            cls.__slots__ = slots
        # By default, the digest key (key_digest) has the same payload as the default
        # parser (i.e. _parse) so we can just point the digest parsing to the default one.
        if "_parse_digest_" not in cls.__dict__:
            cls._parse_digest_ = cls._parse

    def __init__(
        self,
        subid: str,
        hub: Hub,
        key_digest: str,
        model: str,
        /,
        **kwargs,
    ):
        assert (
            subid not in hub.subdevices
        ), f"Subdevice with id {subid} already exists in hub {hub.display_name}"
        hub.subdevices[subid] = self
        self.key_digest = key_digest
        self.model = model
        super().__init__(
            subid,
            hub,
            device_entry=hub.parent.device_registry.async_get_or_create(
                config_entry_id=hub.config_entry.entry_id,
                manufacturer=mc.MANUFACTURER,
                name=get_productname(model),
                model=model,
                via_device=next(iter(hub.device_entry.identifiers)),
                identifiers={(mlc.DOMAIN, subid)},
            ),
            **kwargs,
        )
        hub.register_parser_ex(self, self.init_ns, *self.NS_HUB)
        hub.get_handler(mn_h.Appliance_Hub_Battery).register_parser(
            SensorParser(
                subid,
                hub,
                ns=mn_h.Appliance_Hub_Battery,
                device_class=SensorParser.DeviceClass.BATTERY,
                entity_key=mc.KEY_BATTERY,
            )
        )
        # TODO: add the update entity

    def shutdown(self):
        super().shutdown()
        for _parse_method in tuple(
            _p for _p in self.__dict__ if _p.startswith("_parse_")
        ):
            delattr(self, _parse_method)
        del self.parent.subdevices[self.channel]

    # interface: BaseDevice
    @property
    @override
    def entities(self) -> "Mapping[object, mle.Entity]":
        return {
            id: entity
            for id, entity in self.parent.entities.items()
            if entity.channel == self.channel
        }

    @property
    @override
    def display_name(self) -> str:
        return (
            self.device_entry.name_by_user
            or self.device_entry.name
            or get_productname(self.model)
        )

    @property
    @override  # interface: PhysicalDevice
    def firmware_version(self, /) -> str:
        return self.device_entry.sw_version or self.latest_version[mc.KEY_VERSION]

    @override  # interface: SubDevice
    def _parse_unknown_(self, nh: NamespaceHandler, payload: dict, /):
        if self.parent.create_diagnostic_entities:
            # since we're parsing an unknown namespace, our euristic about
            # the key_namespace might be wrong so we use another euristic
            if not nh.polling_strategy:
                nh.polling_strategy = NamespaceHandler.async_poll_diagnostic
            # Here we should decide between ns.key and ns.slug_end as the parent_key
            # for structured parsing. For reference, consider the standard NamespaceHandler
            # implementation in helpers/namespaces.py where both values are concatenated.
            # Using ns.key should be more consistent with how the Hub subdevices
            # usually report their payloads in *.All and *.Digest.
            self.parent.parse_undefined_dict(nh.id.key, payload, self.channel)
        else:
            self.log(
                self.DEBUG,
                "Handler undefined for namespace:%s payload:%s",
                nh.id,
                _payload=payload,
                timeout=14400,
            )

    # interface: ParserEntity
    @override
    def set_unavailable(self):
        if self.is_connected:
            self.on_disconnect()
        super().set_unavailable()

    # interface: self
    async def async_subdevice_shutdown(self):
        """Dedicated method for subdevice 'standalone' shutdown when we want to just remove this
        and its related entities without affecting the whole hub device. This is useful when
        we discover a subdevice has been removed from the hub and we need to cleanup accordingly.
        """
        for entity in tuple(
            _entity
            for _entity in self.parent.entities.values()
            if _entity.channel == self.channel
        ):
            await entity.async_shutdown()

    def update_sub_device_info(self, sub_device_info: "SubDeviceInfoType", /):
        name = sub_device_info.get(mc.KEY_SUBDEVICENAME) or get_productname(self.model)
        if name != self.device_entry.name:
            self.parent.parent.device_registry.async_update_device(
                self.device_entry.id, name=name
            )

    def parse_digest(self, payload: "mt.hub.Digest_SubDevice", /):
        """
        Heuristic/Generalized parser for subdevice digest payloads.
        This is called by HubMixin when parsing the hub digest either in Appliance.System.All
        or in Appliance.Digest.Hub namespaces.
        This base implementation is slightly optimized towards currently known subdevices
        since the digest structure is straightforward. A more generic implementation is
        defined in UnknownSubDevice.

        Example payload:
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
        if self.is_connected:
            self._parse_digest_(payload[self.key_digest])

    def _parse_digest_(self, payload, /):
        self._hub_parse(self.key_digest, payload)

    def _parse_all(self, payload: dict, /):
        """
        Heuristic parser for Appliance.Hub.Mts100.All or Appliance.Hub.Sensor.All
        when the SubDevice doesn't have a SubDeviceEntity specialization. This is
        automatically invoked by _hub_parse.
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
        so we just extract generic sensors where we find 'latest'
        Luckily enough some key names in Meross should consistently map
        to correct device_classes in HA at least for 'temperature' and 'humidity'.
        Another caveat would be the 'device_scale'.
        """

        self._parse_online(payload[mc.KEY_ONLINE])
        if self.is_connected:
            _excluded_keys = (mc.KEY_ID, mc.KEY_ONLINE)
            for _ in (
                self._hub_parse(key, value)
                for key, value in payload.items()
                if (type(value) is dict) and (key not in _excluded_keys)
            ):
                pass

    def _parse_exception(self, payload, /):
        """{"id": "00000000", "code": 5061}"""
        # TODO: code 5061 seems related to loss of connectivity between the hub
        # and the device. We might put up a binary sensor.
        self.log(self.WARNING, "Received exception payload: %s", str(payload))

    def _parse_online(self, payload: "mt.hub._Online", /):
        # Availability for entities is managed also at Hub connect/disconnect.
        # Here we only forward availability when we suppose it's needed.
        # At hub connection every entity is made available (but state is not flushed until a
        # message is received for that entity) while at hub disconnection every entity is made unavailable.
        if payload[mc.KEY_STATUS] == mc.STATUS_ONLINE:
            if not self.is_connected:
                self.on_connect()
                if not self.available:
                    for entity in self.entities.values():
                        entity.set_available()
        else:
            if self.available:
                for entity in self.entities.values():
                    entity.set_unavailable()

    def _parse_beep(self, payload: "mt.hub.SubDevice_Beep", /):
        self.handlers[mn_h.Appliance_Hub_SubDevice_Beep].swap_parsers(
            self,
            self.parent.add_entity(
                SwitchParser(
                    self.channel,
                    self.parent,
                    ns=mn_h.Appliance_Hub_SubDevice_Beep,
                    entity_key=(
                        f"{mn_h.Appliance_Hub_SubDevice_Beep.slug}__{SwitchParser.init_key_value}"
                    ),
                    name="Beep alarm",
                    device_value=payload[mc.KEY_ONOFF],
                )
            ),
        )

    def _parse_version(self, payload: "mt.hub.SubDevice_Version", /):
        device_entry = self.device_entry
        kwargs = {}
        hw_version = payload[mc.KEY_HARDWARE]
        if hw_version != device_entry.hw_version:
            kwargs["hw_version"] = hw_version
        sw_version = payload[mc.KEY_FIRMWARE]
        if sw_version != device_entry.sw_version:
            kwargs["sw_version"] = sw_version
        if kwargs:
            self.parent.parent.device_registry.async_update_device(
                device_entry.id, **kwargs
            )

    def _hub_parse(self, key: str, payload: dict, /):
        """Heuristic subdevice parsing system. This will be eventually removed
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
            if self.parent.create_diagnostic_entities:
                self.parent.parse_undefined_dict(key, payload, self.channel)
        except Exception as exception:
            self.log_exception(
                self.WARNING,
                exception,
                "_hub_parse(%s, %s)",
                key,
                str(payload),
                timeout=14400,
            )


# Here we need to disable polling for ns which are already carried in hub 'digest' or 'sensor_all'
NamespaceHandler.POLLING_CONFIG_MAP.update(
    {
        mn_h.Appliance_Config_DeviceCfg: NamespaceHandler.POLLING_CONFIG_CONFIGURATION,
        mn_h.Appliance_Control_Water: NamespaceHandler.POLLING_CONFIG_DEFAULT,
        mn_h.Appliance_Control_Sensor_LatestX: NamespaceHandler.POLLING_CONFIG_FASTSENSOR,
        mn_h.Appliance_Hub_Battery: (
            3600,
            mlc.PARAM_CLOUD_UPDATE_PERIOD,
            NamespaceHandler.async_poll_smart,
        ),
        mn_h.Appliance_Hub_Exception: NamespaceHandler.POLLING_CONFIG_NONE,
        mn_h.Appliance_Hub_Online: NamespaceHandler.POLLING_CONFIG_NONE,
        mn_h.Appliance_Hub_ToggleX: NamespaceHandler.POLLING_CONFIG_NONE,
        mn_h.Appliance_Hub_SubDevice_Beep: NamespaceHandler.POLLING_CONFIG_CONFIGURATION,
        mn_h.Appliance_Hub_SubDevice_Version: NamespaceHandler.POLLING_CONFIG_ONCE,
        mn_h.Appliance_Hub_Mts100_Adjust: NamespaceHandler.POLLING_CONFIG_CONFIGURATION,
        mn_h.Appliance_Hub_Mts100_All: (
            0,
            mlc.PARAM_CLOUD_UPDATE_PERIOD,
            NamespaceHandler.async_poll_chunked,
        ),
        mn_h.Appliance_Hub_Mts100_Mode: NamespaceHandler.POLLING_CONFIG_NONE,
        mn_h.Appliance_Hub_Mts100_ScheduleB: (
            mlc.PARAM_CONFIG_UPDATE_PERIOD,
            mlc.PARAM_CLOUD_UPDATE_PERIOD,
            NamespaceHandler.async_poll_chunked,
        ),
        mn_h.Appliance_Hub_Mts100_Temperature: NamespaceHandler.POLLING_CONFIG_NONE,
        mn_h.Appliance_Hub_Sensor_Adjust: NamespaceHandler.POLLING_CONFIG_CONFIGURATION,
        mn_h.Appliance_Hub_Sensor_All: (
            0,
            mlc.PARAM_CLOUD_UPDATE_PERIOD,
            NamespaceHandler.async_poll_chunked,
        ),
        mn_h.Appliance_Hub_Sensor_DoorWindow: NamespaceHandler.POLLING_CONFIG_NONE,
        mn_h.Appliance_Hub_Sensor_Smoke: NamespaceHandler.POLLING_CONFIG_NONE,
        mn_h.Appliance_Hub_Sensor_TempHum: NamespaceHandler.POLLING_CONFIG_NONE,
        mn_h.Appliance_Hub_Sensor_WaterLeak: NamespaceHandler.POLLING_CONFIG_NONE,
    }
)
