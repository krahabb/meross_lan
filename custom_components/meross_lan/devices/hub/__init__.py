from functools import cached_property
from typing import TYPE_CHECKING, override

from ... import const as mlc
from ...binary_sensor import MLBinarySensor
from ...button import MLButton
from ...calendar import MtsSchedule
from ...climate import MtsClimate
from ...helpers import device as mld, entity as me
from ...helpers.namespaces import (
    POLLING_STRATEGY_CONF,
    NamespaceHandler,
    VoidNamespaceHandler,
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
        Iterable,
        Mapping,
        NotRequired,
        Self,
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


class HubBeep(MLSwitch):
    """Generic switch to map Appliance.Hub.SubDevice.Beep namespace."""

    ns = mn_h.Appliance_Hub_SubDevice_Beep
    ENTITY_KEY = f"{ns.slug}__{MLSwitch.key_value}"

    _attr_name = "Beep alarm"


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

    def _handle_list(self, message: "MerossMessage"):
        """Generalized Hub namespace dispatcher to subdevices.
        This code is being step-by-step migrated to be complient with
        the base NamespaceHandler implementation where possible.
        Migration will be done ns by ns so we'll have some ns with 'parsers'
        while some other will still work through this generalized handler."""
        hub = self.device
        entities = hub.entities
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

                # try default parsing mechanics
                try:
                    self.parsers[subdevice_id](payload)
                    continue
                except KeyError as ke:
                    if ke.args[0] != subdevice_id:
                        raise
                # fallback
                try:
                    entities[subdevice_id]._hub_parse(ns_key, payload)
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

    if TYPE_CHECKING:
        # entities now contains both MLEntity and SubDevice
        # so we just override this to make the linter happy
        entities: Final[dict[str, "SubDevice"]]  # type: ignore[override]

    DEVICE_TYPE = mlc.DeviceType.HUB
    NAMESPACES = mn.HUB_NAMESPACES

    # TODO: skip caching add_entity callback and directly access core component method
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
    )

    @override
    def managed_entities(self, platform, /):
        entities = super().managed_entities(platform)
        for subdevice in self.subdevices:
            entities.extend(subdevice.managed_entities(platform))
        return entities

    @override
    def _create_handler(self, ns: "Namespace", /):
        if ns.key_channel is mc.KEY_ID:
            # This rule states that the payload is a list of subdevices indexed by 'id'.
            # Newer devices (2024) started using namespaces/payload indexed by 'subid'
            # and 'channel'. These will be handled by the base class NamespaceHandler
            # using SubDevice/Entity as NamespaceParser.
            return HubNamespaceHandler(self, ns)
        else:
            return super()._create_handler(ns)

    @override
    def update_device_info(
        self, device_info: "DeviceInfoExtType", profile: "MQTTProfile", /
    ):
        super().update_device_info(device_info, profile)
        # propagate device info to subdevices
        for sub_device_info in device_info.get("__subDeviceInfo", []):
            try:
                self.entities[sub_device_info["subDeviceId"]].update_sub_device_info(
                    sub_device_info
                )
            except KeyError:
                continue

    # interface: self
    @property
    def subdevices(self, /):
        return (
            entity for entity in self.entities.values() if entity.__class__ is SubDevice
        )

    def log_duplicated_subdevice(self, subdevice_id: str, /):
        self.log(
            self.CRITICAL,
            "Subdevice %s (id:%s) appears twice in device data. Shouldn't happen",
            self.entities[subdevice_id].display_name,
            subdevice_id,
            timeout=604800,  # 1 week
        )

    def _subdevice_build(self, p_subdevice: "mt_h.Digest_SubDevice", /):
        # parses the subdevice payload in 'digest' to look for a well-known type
        # and builds accordingly
        subid = p_subdevice[mc.KEY_ID]
        self.remove_issue(mlc.ISSUE_HUB_SUBDEVICE_REMOVED, subid)
        try:
            key_digest = get_subdevice_key_digest(p_subdevice)
            if key_digest.startswith(mc.TYPE_MTS):
                entity_class = Mts100Climate
            else:
                entity_class = SubDeviceEntity.DIGEST_MAP.get(key_digest)
        except StopIteration:
            # the hub could report incomplete info anytime so beware.
            # this is true when subdevice is offline and hub has no recent info
            # we'll check our device registry for luck.
            # The relationship between model and key_digest is not 1:1 so
            # we need to accurately map the correct class.
            device_entry = self.api.device_registry.async_get_device(
                identifiers={(mlc.DOMAIN, subid)}
            )
            if not device_entry:
                raise Exception("Cannot identify subdevice type")
            key_digest = device_entry.model
            if not key_digest:
                raise Exception("Cannot identify subdevice type")
            key_digest = key_digest.lower()
            if key_digest.startswith(mc.TYPE_MTS):
                entity_class = Mts100Climate
            else:
                entity_class = (
                    entity_class
                    for entity_class in SubDeviceEntity.DIGEST_MAP.values()
                    if entity_class.MODEL == key_digest
                ).__next__()

        return SubDevice(self, subid, key_digest, entity_class)


class SubDevice(mld.BaseDevice, MLNumericSensor):
    """
    Class for a physical subdevice registered with a Hub device.
    This class acts as a 'container' for the actual entities implemented for the device
    but also implements a default entity (battery level) since this seems pretty universal.
    This nevertheless adds NamespaceParser behavior for this class so that we can handle
    subdevice-specific namespaces and digest parsing in this context.
    The SubDevice will appear in the containing Hub entities container together with
    some other general entities (almost appearing in any other device) strictly related to
    the Hub device itself (like signal level or so).
    """

    if TYPE_CHECKING:
        # overrides
        manager: "HubMixin"

        # self
        NS_SUBDEVICE: ClassVar[Iterable[Namespace]]
        model: Final[str]

    DEVICE_TYPE = mlc.DeviceType.SUBDEVICE

    # MLNumericSensor attributes
    # ENTITY_KEY = mc.KEY_BATTERY
    _attr_device_class = MLNumericSensor.DeviceClass.BATTERY

    NS_SUBDEVICE = (
        mn_h.Appliance_Hub_Battery,
        mn_h.Appliance_Hub_Exception,
        mn_h.Appliance_Hub_Online,
        mn_h.Appliance_Hub_SubDevice_Beep,
        mn_h.Appliance_Hub_SubDevice_Version,
    )

    __slots__ = (
        (
            "async_request",
            "key_digest",
            "model",
        )
        + mld.BaseDevice.__SLOTS__
        + mld.mlm.EntityManager.__SLOTS__
    )

    def __init__(
        self,
        hub: HubMixin,
        subid: str,
        key_digest: str,
        entity_class: "type[SubDeviceEntity] | None",
        /,
    ):
        # fix some base attributes...TODO: this needs to be better addressed
        self.platforms = hub.platforms
        self.async_request = hub.async_request
        self.ns_handlers = hub.ns_handlers
        # In order to keep compatibility with existing code
        # until we find a clear solution for id/channel/entity_key
        # we save subid for safe use whenever we need a 'clear' device subid
        self.key_digest = key_digest
        self.model = model = (entity_class and entity_class.MODEL) or key_digest
        super().__init__(
            hub,
            subid,
            device_entry=hub.api.device_registry.async_get_or_create(
                config_entry_id=hub.config_entry.entry_id,
                manufacturer=mc.MANUFACTURER,
                name=get_productnameuuid(model, subid),
                model=model,
                via_device=next(iter(hub.device_entry_ids["identifiers"])),
                identifiers={(mlc.DOMAIN, subid)},
            ),
        )
        hub.register_parser_ex(self, *self.NS_SUBDEVICE)
        if entity_class:
            subdev_entity = entity_class(self, subid)
            self._digest_parse = subdev_entity._parse
            hub.register_parser_ex(subdev_entity, entity_class.ns, *entity_class.NS_HUB)

    @override
    async def async_shutdown(self):
        # fool the python inheritance pattern
        await super().async_shutdown()
        del self.async_request
        del self.ns_handlers

    # interface: EntityManager
    @property
    @override
    def display_name(self) -> str:
        return (
            self.device_entry.name_by_user
            or self.device_entry.name
            or get_productnameuuid(self.model, self.id)
        )

    @override
    def generate_unique_id(self, entity: me.MLEntity, /):
        return f"{self.manager.id}_{entity.id}"

    # interface: BaseDevice
    @override
    def get_upgrade_payload(self, /) -> "mt_c.Upgrade":
        # start from hub upgrade payload (eventually)
        upgrade_payload = self.manager.get_upgrade_payload()
        latest_version = self.latest_version
        if versiontuple(latest_version[mc.KEY_VERSION]) > versiontuple(
            self.device_entry.sw_version or latest_version[mc.KEY_VERSION]
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
            self.device_entry.sw_version,
            self.latest_version.get(mc.KEY_VERSION),
            self.latest_version.get(mc.KEY_DESCRIPTION),
        )

    @property
    @override
    def tz(self):
        return self.manager.tz

    # interface: MLEntity
    @cached_property
    @override
    def unique_id(self) -> str:
        # temporary fix to keep the embedded battery sensor unique_id
        # compatible with previous layout
        return f"{self.manager.id}_{self.id}_battery"

    @override
    def set_available(self):
        self._set_online()
        super().set_available()

    @override
    def set_unavailable(self):
        self._set_offline()
        super().set_unavailable()

    # interface: self
    def update_sub_device_info(self, sub_device_info: "SubDeviceInfoType", /):
        name = sub_device_info.get(mc.KEY_SUBDEVICENAME) or get_productnameuuid(
            self.model, self.id
        )
        if name != self.device_entry.name:
            self.api.device_registry.async_update_device(
                self.device_entry.id, name=name
            )

    def _digest_parse(self, payload, /):
        """Internally invoked by parse_digest to parse subdevice-specific digest payloads.
        When a specialized SubDeviceEntity is installed this attribute will be redirected
        to the entity generic '_parse' method.
        The default implementation will just try the 'smart logic' parser by inspecting the
        class methods or building diagnostic entities in case."""
        self._hub_parse(self.key_digest, payload)

    def parse_digest(self, payload: "mt_h.Digest_SubDevice", /):
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
        if self.online:
            self._digest_parse(payload[self.key_digest])

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
        if self.online:
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
                device_value=payload[mc.KEY_ONOFF],
            ),
        )

    def _parse_version(self, payload: "mt_h.SubDevice_Version", /):
        device_entry = self.device_entry
        kwargs = {}
        hw_version = payload[mc.KEY_HARDWARE]
        if hw_version != device_entry.hw_version:
            kwargs["hw_version"] = hw_version
        sw_version = payload[mc.KEY_FIRMWARE]
        if sw_version != device_entry.sw_version:
            kwargs["sw_version"] = sw_version
        if kwargs:
            self.api.device_registry.async_update_device(device_entry.id, **kwargs)

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
                    entity_key = f"{parent_key}_{subkey}"
                    try:
                        self.entities[f"{self.id}_{entity_key}"].update_native_value(
                            subvalue
                        )
                    except KeyError:
                        MLDiagnosticSensor(
                            self,
                            self.id,
                            entity_key=entity_key,
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


class SubDeviceEntity(me.MLEntity):
    """Base class for entities acting as the 'main target' of a subdevice namespace parsing.
    The design allows to easily link both digest parsing and *.All parsing to the
    default entity parsing stub (MLEntity._parse). This is a rather common pattern even
    though some specializations could be needed for some subdevices."""

    if TYPE_CHECKING:
        # overrides
        manager: "SubDevice"

        DIGEST_MAP: Final[dict[str, type[Self]]]
        """Static registration map for SubDeviceEntity subclasses by their KEY_DIGEST."""

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
        NS_HUB: ClassVar[Iterable[Namespace]]

    DIGEST_MAP = {}
    MODEL = None
    NS_HUB = ()

    def __init_subclass__(cls):
        try:
            cls.DIGEST_MAP[cls.KEY_DIGEST] = cls
        except AttributeError:
            # KEY_DIGEST not defined...we need to allow for intermediate classes
            pass

    def _parse_all(self, payload: dict, /):
        self.manager._parse_online(payload[mc.KEY_ONLINE])
        if not self.available:
            return
        self._parse(payload[self.manager.key_digest])


# TODO: this lame import is to be later refactored to use lazy imports
# whenever subdevices appear in the code.
from .mts100 import Mts100Climate


class SmokeAlarmSensor(SubDeviceEntity, MLEnumSensor):
    if TYPE_CHECKING:
        STATUS_MAP: Final
        MUTE_MAP: Final
        STATUS_ALARM: Final[set[int]]
        STATUS_ERROR: Final[set[int]]
        STATUS_MUTED: Final[set[int]]

    MODEL = mc.TYPE_GS559
    KEY_DIGEST = mc.KEY_SMOKEALARM
    NS_HUB = (mn_h.Appliance_Hub_Sensor_All, *SubDeviceEntity.NS_HUB)

    ns = mn_h.Appliance_Hub_Sensor_Smoke
    ENTITY_KEY = mc.KEY_STATUS
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

    def __init__(self, subdevice: "SubDevice", subid: str):
        self.device_value = (
            None  # TODO: move to MLEnumSensor together with mapping capability
        )
        super().__init__(subdevice, subid, translation_key="smoke_alarm_status")
        self.binary_sensor_alarm = MLBinarySensor(
            subdevice,
            subid,
            entity_key=mc.KEY_ALARM,
            device_class=MLBinarySensor.DeviceClass.SAFETY,
        )
        self.binary_sensor_error = MLBinarySensor(
            subdevice,
            subid,
            entity_key=mc.KEY_ERROR,
            device_class=MLBinarySensor.DeviceClass.PROBLEM,
        )
        self.binary_sensor_muted = MLBinarySensor(subdevice, subid, entity_key="muted")
        self.sensor_interConn = MLEnumSensor(
            subdevice, subid, entity_key=mc.KEY_INTERCONN
        )
        MLButton(subdevice, subid, "button_mute", self.async_mute, name="Mute")
        MLButton(subdevice, subid, "button_test", self.async_test, name="Test")

    def _parse(self, payload: "mt_h._smokeAlarm", /):
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


class MS100Sensor(SubDeviceEntity, MLTemperatureSensor):

    class SensorAdjustNumber(MLConfigNumber):
        ns = mn_h.Appliance_Hub_Sensor_Adjust

        _attr_device_scale = 10

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

    class AdjustTemperatureNumber(SensorAdjustNumber):

        ENTITY_KEY = "config_adjust_temperature"
        key_value = mc.KEY_TEMPERATURE
        _attr_device_class = MLConfigNumber.DeviceClass.TEMPERATURE
        _attr_name = "Adjust temperature"

        native_min_value = -5
        native_max_value = 5
        native_step = 0.1

    class AdjustHumidityNumber(SensorAdjustNumber):

        ENTITY_KEY = "config_adjust_humidity"
        key_value = mc.KEY_HUMIDITY
        _attr_device_class = MLConfigNumber.DeviceClass.HUMIDITY
        _attr_name = "Adjust humidity"

        native_min_value = -20
        native_max_value = 20
        native_step = 1

    MODEL = mc.TYPE_MS100
    KEY_DIGEST = mc.TYPE_MS100
    NS_HUB = (
        mn_h.Appliance_Hub_Sensor_All,
        mn_h.Appliance_Hub_Sensor_Adjust,
        mn_h.Appliance_Hub_Sensor_Latest,
        *SubDeviceEntity.NS_HUB,
    )

    ns = mn_h.Appliance_Hub_Sensor_TempHum

    _attr_device_scale = 10

    __slots__ = ("sensor_humidity",)

    def __init__(self, subdevice: "SubDevice", subid: str):
        super().__init__(subdevice, subid)
        self.sensor_humidity = MLHumiditySensor(subdevice, subid)

    async def async_shutdown(self):
        await super().async_shutdown()
        del self.sensor_humidity

    @override
    def _parse(self, payload: "mt_h.Sensor_TempHum | mt_h._ms100", /):
        self._update_sensors(
            payload[mc.KEY_LATESTTEMPERATURE], payload[mc.KEY_LATESTHUMIDITY]
        )

    @override
    def _parse_all(self, payload: "mt_h.Sensor_All_ms100", /):
        self.manager._parse_online(payload[mc.KEY_ONLINE])
        if self.available:
            self._update_sensors(
                payload[mc.KEY_TEMPERATURE][mc.KEY_LATEST],
                payload[mc.KEY_HUMIDITY][mc.KEY_LATEST],
            )

    def _parse_adjust(self, payload: "mt_h.Sensor_Adjust"):
        subdevice = self.manager
        subdevice.manager.ns_handlers[mn_h.Appliance_Hub_Sensor_Adjust].swap_parsers(
            self,
            MS100Sensor.AdjustTemperatureNumber(
                subdevice,
                subdevice.id,
                device_value=payload[mc.KEY_TEMPERATURE],
            ),
            MS100Sensor.AdjustHumidityNumber(
                subdevice,
                subdevice.id,
                device_value=payload[mc.KEY_HUMIDITY],
            ),
        )
        # swap also the update_sensors method to a smarter one
        self._update_sensors = self._update_sensors_adjust

    def _parse_latest(self, payload: "mt_h.Sensor_Latest"):
        self._update_sensors(
            payload[mc.KEY_TEMPERATURE]["sample"],
            payload[mc.KEY_HUMIDITY]["sample"],
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


class MS100FSensor(MS100Sensor):
    """A variant of MS100SubDevice for the MS100FH device"""

    MODEL = mc.TYPE_MS100F
    KEY_DIGEST = mc.KEY_TEMPHUM


class MS130Sensor(MS100Sensor):
    MODEL = mc.TYPE_MS130
    KEY_DIGEST = mc.KEY_TEMPHUMI
    NS_HUB = (mn_h.Appliance_Config_DeviceCfg, *MS100Sensor.NS_HUB)
    _attr_device_scale = 100

    __slots__ = ("sensor_light",)

    def __init__(self, subdevice: "SubDevice", subid: str):
        super().__init__(subdevice, subid)
        self.sensor_light = MLLightSensor(subdevice, subid)
        subdevice.manager.get_handler(
            mn_h.Appliance_Control_Sensor_LatestX
        ).register_parser(
            self,
            {"channel": 0, "data": ["light", "temp", "humi"]},
        )

    async def async_shutdown(self):
        await super().async_shutdown()
        del self.sensor_light

    @override
    def _parse(self, payload: "mt_h._tempHumi", /):
        self._update_sensors(payload[mc.KEY_TEMP], payload[mc.KEY_HUMI])

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


class DoorWindowSensor(SubDeviceEntity, MLBinarySensor):
    MODEL = mc.TYPE_MS200
    KEY_DIGEST = mc.KEY_DOORWINDOW
    NS_HUB = (mn_h.Appliance_Hub_Sensor_All, *SubDeviceEntity.NS_HUB)

    ENTITY_KEY = MLBinarySensor.DeviceClass.WINDOW
    ns = mn_h.Appliance_Hub_Sensor_DoorWindow
    key_value = mc.KEY_STATUS

    _attr_device_class = MLBinarySensor.DeviceClass.WINDOW


class WaterLeakSensor(SubDeviceEntity, MLBinarySensor):
    MODEL = mc.TYPE_MS400
    KEY_DIGEST = mc.KEY_WATERLEAK
    NS_HUB = (mn_h.Appliance_Hub_Sensor_All, *SubDeviceEntity.NS_HUB)

    ENTITY_KEY = mc.KEY_WATERLEAK
    ns = mn_h.Appliance_Hub_Sensor_WaterLeak
    key_value = mc.KEY_LATESTWATERLEAK

    _attr_device_class = MLBinarySensor.DeviceClass.SAFETY


class MstSwitch(SubDeviceEntity, HubSubIdChannelMixin, MLSwitch):
    """Switch to turn on/off the MST valve."""

    # TODO: it looks like this device could support Hub.ToggleX

    if TYPE_CHECKING:
        # Appliance.Config.DeviceCfg payload structure
        class DeviceCfg_mstCfg_calibration(TypedDict):
            waCon: int  # water consumption
            onoff: int
            lmTime: int

        class DeviceCfg_mstCfg(TypedDict):
            dura: int  # duration of watering in seconds
            wfm: int  # water flow measurement
            calibration: "MstSwitch.DeviceCfg_mstCfg_calibration"

        class DeviceCfg(mt_h.SubIdPayload):
            mstCfg: "MstSwitch.DeviceCfg_mstCfg"

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
        _attr_name = "Watering duration"
        _attr_device_class = MLConfigNumber.DEVICE_CLASS_DURATION
        _attr_native_unit_of_measurement = MLConfigNumber.hac.UnitOfTime.SECONDS
        native_max_value = 86400  # 1 day max duration (no real info just guessing)
        native_min_value = 1

    MODEL = mc.TYPE_MST100
    KEY_DIGEST = mc.KEY_MST
    NS_HUB = (mn_h.Appliance_Config_DeviceCfg, *SubDeviceEntity.NS_HUB)

    ENTITY_KEY = mc.KEY_ONOFF
    ns = mn_h.Appliance_Control_Water
    native_on = 1
    native_off = 2

    _attr_name = "Watering"

    __slots__ = ("number_duration",)

    def __init__(self, subdevice: "SubDevice", subid: str):
        super().__init__(subdevice, subid)
        self.number_duration = MstSwitch.WateringDurationNumber(subdevice, subid)

    async def async_shutdown(self):
        await super().async_shutdown()
        del self.number_duration

    @override
    def _parse(self, payload: "mt_h._mst", /):
        # unknown payload semantic
        pass

    _parse_water = MLSwitch._parse

    def _parse_deviceCfg(self, payload: "DeviceCfg", /):
        self.number_duration._parse(payload)


def digest_init_hub(
    device: "HubMixin", digest: "mt_h.Digest_Hub", /
) -> "DigestInitReturnType":

    # This is a trick to dynamically mixin the HubMixin capabilities
    # into the device instance. Historically we were mixing HubMixin
    # as a subclass of the device class at ConfigEntry load time in ComponentApi
    # but this new approach requires less coding.
    # BEWARE: this works if we don't need special __init__ logic in HubMixin
    # because the instance is already initialized here and we're called in the
    # context of Device.async_init method. This happens rather soon but surely
    # after Device.__init__
    # Also, the base Device class mixed-in might be different at test time since it gets mocked
    # so we have to dynamically create a new class on the fly. and ensure HubMixin is not
    # overriding any mocked attribute (see test.helpers.ConfigEntryMocker.ManagerMock)
    device.__class__ = type(
        f"HubMixin{device.__class__.__name__}", (HubMixin, device.__class__), {}
    )
    # temporary patch entry platforms defaults
    device.platforms = HubMixin.DEFAULT_PLATFORMS.copy() | device.platforms

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
        if device_entry.via_device_id == device.device_entry.id:
            # checking 'via_device_id' should be enough to ensure
            # the device hasn't been re-binded
            for identifiers in device_entry.identifiers:
                if identifiers[0] == mlc.DOMAIN:
                    registry_subdevices[identifiers[1]] = device_entry

    for p_subdevice_digest in digest[mc.KEY_SUBDEVICE]:
        try:
            subdevice_id = p_subdevice_digest[mc.KEY_ID]
            if subdevice_id in device.entities:
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

    def digest_parse_hub(p_hub: dict, /):
        # Usually called by _handle_Appliance_System_All as part of the digest parsing
        # Here we'll check the fresh subdevice list against the actual one and
        # eventually manage newly added subdevices or removed ones #119
        subdevices = set(device.subdevices)

        for p_subdevice_digest in p_hub[mc.KEY_SUBDEVICE]:
            try:
                subdevice_id = p_subdevice_digest[mc.KEY_ID]
                try:
                    subdevice = device.entities[subdevice_id]
                    try:
                        subdevices.remove(subdevice)
                    except KeyError:
                        # this shouldnt but happened in a trace (#331)
                        device.log_duplicated_subdevice(subdevice_id)
                        continue
                except KeyError:
                    subdevice = device._subdevice_build(p_subdevice_digest)
                    device.schedule_entry_update(True)

                subdevice.parse_digest(p_subdevice_digest)
            except Exception as exception:
                device.log_exception(device.WARNING, exception, "digest_parse_hub")

        if subdevices:
            # now we're left with non-existent (removed) subdevices
            device.schedule_entry_update(False)
            for subdevice in subdevices:
                device.log(
                    device.WARNING,
                    "%s (id:%s) unregistered from hub",
                    subdevice.display_name,
                    subdevice.id,
                )
                device.create_issue(
                    mlc.ISSUE_HUB_SUBDEVICE_REMOVED,
                    subdevice.id,
                    severity=device.IssueSeverity.WARNING,
                    translation_placeholders={"device_name": subdevice.display_name},
                )
                device.async_create_task(
                    subdevice.async_shutdown(),
                    f"{subdevice.__class__.__name__}.async_shutdown()",
                )

    ability = device.descriptor.ability
    if mn_h.Appliance_Digest_Hub in ability:
        NamespaceHandler(
            device,
            mn_h.Appliance_Digest_Hub,
            handler=lambda message: digest_parse_hub(message.payload[mc.KEY_HUB]),
        )
    for ns in (
        mn_h.Appliance_Hub_ExtraInfo,
        mn_h.Appliance_Hub_SubdeviceList,
    ):
        if ns in ability:
            VoidNamespaceHandler(device, ns)

    return digest_parse_hub, ()


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
            0,  # ms600 devices lack of refresh #599
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
            NamespaceHandler.async_poll_smart,
        ),
        mn_h.Appliance_Hub_SubDevice_Version: (
            0,
            mlc.PARAM_CLOUDMQTT_UPDATE_PERIOD,
            mlc.PARAM_HEADER_SIZE,
            55,
            NamespaceHandler.async_poll_once,
        ),
    }
)
