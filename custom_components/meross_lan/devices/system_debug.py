"""
Diagnostic entities for Appliance.System.Debug:
{
  "debug": {
    "system": {"version": "9.3.12", "sysUpTime": "15h56m35s", "UTC": 1794077097,
               "localTimeOffset": 3600, "memTotal": 3958496, "memFree": 3562976, ...},
    "network": {"linkStatus": "connected", "snr": 36, "channel": 11, "signal": 96,
                "rssi": -52, "wifiDisconnectCount": 0, ...},
    "cloud": {"linkStatus": "connected", "sysConnectTime": "Sat Nov  7 02:48:41 2026",
              "sysOnlineTime": "15h56m16s", "sysDisconnectCount": 0, ...}
  }
}
All of the entities are disabled by default and the namespace is only polled
while at least one of them is enabled.
"""

import re
from typing import TYPE_CHECKING, override

from homeassistant import const as hac
from homeassistant.util import dt as dt_util

from ..binary_sensor import BinarySensorParser
from ..helpers import entity as mle
from ..merossclient.device.handler import NamespaceHandler
from ..merossclient.protocol import const as mc, namespaces as mn
from ..sensor import SensorEntity, SensorParser

if TYPE_CHECKING:
    from datetime import datetime
    from typing import Unpack

    from ..helpers.device import Device
    from ..merossclient.protocol.types import JsonMapping

KEY_SYSUPTIME = "sysUpTime"
KEY_UTC = "UTC"
KEY_RSSI = "rssi"
KEY_LINKSTATUS = "linkStatus"
LINKSTATUS_CONNECTED = "connected"


class SystemDebugEntity(mle.ParserEntity if TYPE_CHECKING else object):
    """Mixin enabling the namespace polling while the entity is enabled in HA."""

    # HA core entity attributes:
    _attr_entity_category = mle.Entity.EntityCategory.DIAGNOSTIC
    _attr_entity_registry_enabled_default = False

    async def async_added_to_hass(self):
        await super().async_added_to_hass()  # type: ignore
        handler: SystemDebugNamespaceHandler = self.handler_ns  # type: ignore
        handler.entity_enabled(self)

    async def async_will_remove_from_hass(self):
        handler: SystemDebugNamespaceHandler = self.handler_ns  # type: ignore
        handler.entity_disabled(self)
        await super().async_will_remove_from_hass()  # type: ignore

    @override
    def __call__(self, payload: "JsonMapping", /):
        # devices might not carry every key: just skip parsing in that case
        try:
            super().__call__(payload)  # type: ignore
        except (KeyError, TypeError):
            pass


class SystemDebugSensor(SystemDebugEntity, SensorParser):
    """Numeric diagnostic sensor reading a (nested) key in the debug payload."""


class SystemDebugCloudConnection(SystemDebugEntity, BinarySensorParser):
    """Cloud (MQTT) connection status as seen by the device ('cloud.linkStatus')."""

    _INIT_entity_key = "cloud_connection"
    _INIT_key_value = BinarySensorParser.KeyValue(mc.KEY_CLOUD, KEY_LINKSTATUS)
    _INIT_value_on = LINKSTATUS_CONNECTED  # type: ignore[assignment]

    # HA core entity attributes:
    _attr_device_class = BinarySensorParser.DeviceClass.CONNECTIVITY

    @override
    def update_device_value(self, device_value, /):
        # anything other than 'connected' means not connected
        if self.ns_value != device_value:
            self.ns_value = device_value
            self.is_on = device_value == LINKSTATUS_CONNECTED
            self.flush_state()
            return True


class UptimeSensor(SystemDebugEntity, mle.ParserEntity, SensorEntity):
    """Device uptime exposed as last boot time ('system.sysUpTime')."""

    if TYPE_CHECKING:
        native_value: datetime | None  # type: ignore[assignment]

    UPTIME_PATTERN = re.compile(r"(\d+)\s*([dhms])")
    UPTIME_UNITS = {"d": 86400, "h": 3600, "m": 60, "s": 1}
    BOOT_TIME_TOLERANCE = 60
    """Seconds: computed boot time changes smaller than this (polling jitter) are ignored."""
    DEVICE_CLOCK_TOLERANCE = 3600
    """Seconds: the device clock ('UTC' in the same payload) is used as the reference
    for 'sysUpTime' unless it differs more than this from local time (i.e. not synced)."""

    _INIT_entity_key = "uptime"

    # HA core entity attributes:
    _attr_device_class = SensorEntity.DeviceClass.TIMESTAMP
    _attr_icon = "mdi:timer-outline"
    _attr_state_class = None

    @classmethod
    def parse_uptime(cls, uptime: str, /) -> int | None:
        """Parses the device uptime string (i.e. '1d2h3m4s' or '15h56m35s') into seconds."""
        matches = cls.UPTIME_PATTERN.findall(uptime)
        if not matches:
            return None
        return sum(int(value) * cls.UPTIME_UNITS[unit] for value, unit in matches)

    @override
    def __call__(self, payload: "JsonMapping", /):
        self.ns_value = payload
        try:
            p_system = payload[mc.KEY_SYSTEM]
            uptime = self.parse_uptime(p_system[KEY_SYSUPTIME])
        except (KeyError, TypeError):
            return
        if uptime is None:
            return
        # 'sysUpTime' is relative to the device clock reported in the same payload
        # so we use that as reference (this is independent of transport delays)
        epoch = self.parent.time()
        device_epoch = p_system.get(KEY_UTC)
        if (type(device_epoch) is not int) or (
            abs(device_epoch - epoch) > self.DEVICE_CLOCK_TOLERANCE
        ):
            device_epoch = int(epoch)
        boot_time = dt_util.utc_from_timestamp(device_epoch - uptime)
        native_value = self.native_value
        if (native_value is None) or (
            abs((boot_time - native_value).total_seconds()) > self.BOOT_TIME_TOLERANCE
        ):
            self.update_native_value(boot_time)


class SystemDebugNamespaceHandler(NamespaceHandler):
    """Appliance.System.Debug handler: polling is only enabled while at least one
    of its (disabled by default) diagnostic entities is enabled."""

    if TYPE_CHECKING:
        enabled_entities: set[SystemDebugEntity]

    POLLING_CONFIG = NamespaceHandler.NS_CONTENT_POLLING_CONFIG_MAP[mn.Content.INFO]

    ENTITY_DEFS = (
        UptimeSensor,
        SystemDebugSensor.DEF(
            entity_key="wifi_rssi",
            key_value=SystemDebugSensor.KeyValue(mc.KEY_NETWORK, KEY_RSSI),
            device_class=SensorEntity.DeviceClass.SIGNAL_STRENGTH,
            native_unit_of_measurement=hac.SIGNAL_STRENGTH_DECIBELS_MILLIWATT,
            state_class=SensorEntity.StateClass.MEASUREMENT,
            suggested_display_precision=0,
        ),
        SystemDebugSensor.DEF(
            entity_key="wifi_channel",
            key_value=SystemDebugSensor.KeyValue(mc.KEY_NETWORK, "channel"),
            state_class=None,
            suggested_display_precision=0,
            icon="mdi:wifi-cog",
        ),
        SystemDebugSensor.DEF(
            entity_key="wifi_disconnects",
            key_value=SystemDebugSensor.KeyValue(mc.KEY_NETWORK, "wifiDisconnectCount"),
            state_class=SensorEntity.StateClass.TOTAL_INCREASING,
            suggested_display_precision=0,
            icon="mdi:wifi-off",
        ),
        SystemDebugCloudConnection,
        SystemDebugSensor.DEF(
            entity_key="cloud_disconnects",
            key_value=SystemDebugSensor.KeyValue(mc.KEY_CLOUD, "sysDisconnectCount"),
            state_class=SensorEntity.StateClass.TOTAL_INCREASING,
            suggested_display_precision=0,
            icon="mdi:cloud-off-outline",
        ),
        SystemDebugSensor.DEF(
            entity_key="memory_free",
            key_value=SystemDebugSensor.KeyValue(mc.KEY_SYSTEM, "memFree"),
            device_class=SensorEntity.DeviceClass.DATA_SIZE,
            native_unit_of_measurement=hac.UnitOfInformation.BYTES,
            state_class=SensorEntity.StateClass.MEASUREMENT,
            suggested_display_precision=0,
            icon="mdi:memory",
        ),
    )

    def __init__(
        self,
        ns: "mn.Namespace",
        device: "Device",
        /,
        **kwargs: "Unpack[NamespaceHandler.Args]",
    ):
        kwargs["config"] = NamespaceHandler.POLLING_CONFIG_NONE
        # not indexed namespace: forward the payload to the registered (dispatcher) parser
        kwargs["handler"] = self._handle_index_none
        NamespaceHandler.__init__(self, ns, device, **kwargs)
        self.enabled_entities = set()
        index = mn.IndexType.none.get()
        self.register_parsers(
            *(
                entity_def(None, device, ns=ns, index=index)
                for entity_def in self.ENTITY_DEFS
            )
        )

    @override
    async def async_poll_queue(self, /):
        # The debug payload is rather big (~1KB) and polled seldom: never pack it
        # into a NS_MULTIPLE request since that could overflow the device response
        # buffer (and the response would be truncated).
        device = self.parent
        self.next_poll_epoch = device.polling_epoch + self.polling_period
        self.handle(await device.async_request(*self.polling_request))

    def entity_enabled(self, entity: SystemDebugEntity, /):
        self.enabled_entities.add(entity)
        (
            self.polling_period,
            self.polling_period_cloud,
            self.polling_strategy,
        ) = self.POLLING_CONFIG

    def entity_disabled(self, entity: SystemDebugEntity, /):
        self.enabled_entities.discard(entity)
        if not self.enabled_entities:
            self.polling_strategy = None
