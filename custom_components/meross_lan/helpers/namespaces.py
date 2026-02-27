from typing import TYPE_CHECKING, override

from .. import const as mlc
from ..merossclient.device import handler
from ..merossclient.protocol import const as mc, namespaces as mn

if TYPE_CHECKING:
    from typing import Any, Callable, Coroutine, Final, Iterable, Self

    from ..merossclient.protocol import types as mt
    from ..merossclient.protocol.message import MerossMessage, MerossResponse
    from .device import Device
    from .entity import MLEntity

    POLLING_STRATEGY_CONF: Final[dict[mn.Namespace, "NamespaceHandler.ConfigType"]]


class NamespaceHandler(handler.NamespaceHandler):
    """
    This is the root class for somewhat dynamic namespace handlers.
    Every device keeps its own list of method handlers indexed through
    the message namespace in order to speed up parsing/routing when receiving
    a message from the device see Device.ns_handlers and
    Device._handle to get the basic behavior.

    - handler: specify a custom handler method for this namespace. By default
    it will be looked-up in the device definition (looking for _handle_xxxxxx)

    - entity_class: specify a MLEntity type (actually an implementation
    of Merossentity) to be instanced whenever a message for a particular channel
    is received and the channel has no parser associated (see _handle_list)

    """

    if TYPE_CHECKING:

        device: Device
        entity_class: type["MLEntity"] | None

    DEFAULT_CONFIG = (
        mlc.PARAM_DIAGNOSTIC_UPDATE_PERIOD,
        mlc.PARAM_CLOUD_UPDATE_PERIOD,
        None,
    )

    __slots__ = ("entity_class",)

    def __init__(
        self,
        device: "Device",
        ns: "mn.Namespace",
        /,
        *,
        handler: "NamespaceHandler.HandlerFunc | None" = None,
        config: "NamespaceHandler.ConfigType | None" = None,
    ):
        super().__init__(
            device,
            ns,
            handler=handler,
            config=config or POLLING_STRATEGY_CONF.get(ns, self.DEFAULT_CONFIG),
        )
        self.entity_class = None

    def register_entity_class(
        self, entity_class: type["MLEntity"], channels: "Iterable[int] | None", /
    ):
        # TODO: rename to parser_class and move to base
        self.entity_class = entity_class
        self.handler = self._handle_list
        self.device.platforms.setdefault(entity_class.PLATFORM)
        for channel in (
            self.device.descriptor.channels if channels is None else channels
        ):
            entity_class(channel, self.device)

    @override
    def _handle_undefined(self, message: "MerossMessage", /):
        device = self.device
        if device.create_diagnostic_entities:
            # since we're parsing an unknown namespace, our euristic about
            # the key_namespace might be wrong so we use another euristic
            ns = self.ns
            if not self.polling_strategy:
                self.polling_strategy = NamespaceHandler.async_poll_diagnostic
            for _key, _payload in message.payload.items():
                # since the ns_key might be often the same across different namespaces
                # we add the last split of the namespace to the extracted payload key
                if type(_payload) is dict:
                    device.parse_undefined_dict(
                        f"{ns.slug_end}_{_key}", _payload, _payload.get(ns.key_idx)
                    )
                elif type(_payload) is list:
                    _key = f"{ns.slug_end}_{_key}"
                    for __payload in _payload:
                        # not having a "channel" in the list payloads is unexpected so far
                        device.parse_undefined_dict(
                            _key, __payload, __payload.get(ns.key_idx)
                        )
                else:
                    # should we diagnostic scalar values in root payload ?
                    pass

        else:
            super()._handle_undefined(message)

    @override
    def _handle_missing_parser(self, p_channel: dict, ke: KeyError, /):
        channel = p_channel[self.ns.key_idx]
        if channel in self.parsers:
            self.log_parser_exception(ke, p_channel)
            return

        if self.entity_class:
            self.entity_class(
                channel, self.device, entity_registry_enabled_default=True
            )
        elif self.device.create_diagnostic_entities:
            from ..sensor import MLDiagnosticSensor

            self.register_parser(
                MLDiagnosticSensor(channel, self.device, entity_key=self.ns.key)
            )
        else:
            self.parsers[channel] = self._parse_stub

        self.parsers[channel](p_channel)


class EntityNamespaceMixin(MLEntity if TYPE_CHECKING else object):
    """
    Special 'polling enabler/disabler' mixin used with entities which are
    'single instance' for a namespace handler and so they'll disable polling
    should they're disabled in HA.
    """

    if TYPE_CHECKING:
        manager: Device

    @classmethod
    def namespace_init(cls, device: "Device", ns: mn.Namespace, /):
        assert ns is cls.ns
        entity = cls(None, device)
        entity.handler_ns = NamespaceHandler(device, ns, handler=entity._handle)
        entity.handler_ns.polling_strategy = None
        return entity

    async def async_added_to_hass(self):
        self.handler_ns.polling_strategy = POLLING_STRATEGY_CONF[self.ns][-1]
        return await super().async_added_to_hass()

    async def async_will_remove_from_hass(self):
        self.handler_ns.polling_strategy = None
        return await super().async_will_remove_from_hass()

    def _handle(self, message: "MerossMessage", /):
        self._parse(message.payload[self.ns.key])


"""
Default timeouts and config parameters for polled namespaces.
The configuration is set in the tuple as:
(
    polling_period,
    polling_period_cloud,
    strategy
)
see the NamespaceHandler class for the meaning of these values
"""
POLLING_STRATEGY_CONF = {
    mn.Appliance_System_Debug: (
        0,
        0,
        None,
    ),  # TODO: add expected size definition to mn.Namespace class grammar
    mn.Appliance_System_DNDMode: (
        mlc.PARAM_CONFIG_UPDATE_PERIOD,
        mlc.PARAM_CLOUD_UPDATE_PERIOD,
        NamespaceHandler.async_poll_smart,
    ),
    mn.Appliance_System_Runtime: (
        mlc.PARAM_SENSOR_SLOW_UPDATE_PERIOD,
        mlc.PARAM_CLOUD_UPDATE_PERIOD,
        NamespaceHandler.async_poll_smart,
    ),
    mn.Appliance_Config_Alarm: (
        mlc.PARAM_CONFIG_UPDATE_PERIOD,
        mlc.PARAM_CLOUD_UPDATE_PERIOD,
        NamespaceHandler.async_poll_smart,
    ),
    mn.Appliance_Config_Sensor_Association: (
        mlc.PARAM_CONFIG_UPDATE_PERIOD,
        mlc.PARAM_CLOUD_UPDATE_PERIOD,
        NamespaceHandler.async_poll_smart,
    ),
    mn.Appliance_Control_Alarm: (
        mlc.PARAM_CONFIG_UPDATE_PERIOD,
        mlc.PARAM_CLOUD_UPDATE_PERIOD,
        NamespaceHandler.async_poll_smart,
    ),
    mn.Appliance_Control_Fan: (
        0,
        mlc.PARAM_CLOUD_UPDATE_PERIOD,
        None,
    ),
    mn.Appliance_Control_FilterMaintenance: (
        mlc.PARAM_CLOUD_UPDATE_PERIOD,
        mlc.PARAM_CLOUD_UPDATE_PERIOD,
        NamespaceHandler.async_poll_smart,
    ),
    mn.Appliance_Control_Light_Effect: (
        mlc.PARAM_CONFIG_UPDATE_PERIOD,
        mlc.PARAM_CLOUD_UPDATE_PERIOD,
        NamespaceHandler.async_poll_smart,
    ),
    mn.Appliance_Control_Mp3: (0, 0, NamespaceHandler.async_poll_default),
    mn.Appliance_Control_PhysicalLock: (
        mlc.PARAM_CONFIG_UPDATE_PERIOD,
        mlc.PARAM_CLOUD_UPDATE_PERIOD,
        NamespaceHandler.async_poll_smart,
    ),
    mn.Appliance_Control_Presence_Config: (
        mlc.PARAM_CONFIG_UPDATE_PERIOD,
        mlc.PARAM_CLOUD_UPDATE_PERIOD,
        NamespaceHandler.async_poll_smart,
    ),
    mn.Appliance_Control_Sensor_Latest: (
        mlc.PARAM_SENSOR_FAST_UPDATE_PERIOD,
        mlc.PARAM_SENSOR_SLOW_CLOUD_UPDATE_PERIOD,
        NamespaceHandler.async_poll_smart,
    ),
    mn.Appliance_Control_Sensor_LatestX: (
        mlc.PARAM_SENSOR_FAST_UPDATE_PERIOD,
        mlc.PARAM_CLOUD_UPDATE_PERIOD,
        NamespaceHandler.async_poll_smart,
    ),
    mn.Appliance_Control_Toggle: (0, 0, NamespaceHandler.async_poll_default),
    mn.Appliance_Mcu_Firmware: (0, 0, NamespaceHandler.async_poll_once),
    mn.Appliance_Mcu_Hp110_Firmware: (0, 0, NamespaceHandler.async_poll_once),
}
