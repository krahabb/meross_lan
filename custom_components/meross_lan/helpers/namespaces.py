from typing import TYPE_CHECKING, override

from .. import const as mlc
from ..merossclient.device.handler import NamespaceHandler as _NH
from ..merossclient.device.parser import NamespaceParser
from ..merossclient.protocol import const as mc, namespaces as mn

if TYPE_CHECKING:
    from typing import ClassVar, Final, Mapping, NotRequired

    from ..merossclient.protocol.message import MerossMessage
    from .device import Device
    from .entity import ParserEntity, ValueParser


class NamespaceHandler(_NH):

    if TYPE_CHECKING:
        type HandlerFunc = _NH.HandlerFunc
        type ParserFunc = _NH.ParserFunc
        type PollingStrategyFunc = _NH.PollingStrategyFunc
        type PollingConfigType = _NH.PollingConfigType
        POLLING_CONFIG_FASTSENSOR: Final[PollingConfigType]
        POLLING_CONFIG_SLOWSENSOR: Final[PollingConfigType]
        POLLING_CONFIG_CONFIGURATION: Final[PollingConfigType]
        """Common polling configuration for namespaces carrying configuration parameters.
        These are polled on a longer period since we don't expect them to change very often."""
        POLLING_CONFIG_ONCE: Final[PollingConfigType]
        """Common polling configuration for namespaces carrying fixed info."""
        POLLING_CONFIG_DIAGNOSTIC: Final[PollingConfigType]
        """Configuration to be used for unknown/unmanaged namespaces."""
        parent: Final[Device]  # type: ignore[override]
        parser_class: type[ParserEntity] | None  # type: ignore[override]

    POLLING_CONFIG_FASTSENSOR = (0, 180, _NH.async_poll_smart)
    POLLING_CONFIG_SLOWSENSOR = (300, 600, _NH.async_poll_smart)
    POLLING_CONFIG_CONFIGURATION = (
        mlc.PARAM_CONFIG_UPDATE_PERIOD,
        mlc.PARAM_CLOUD_UPDATE_PERIOD,
        _NH.async_poll_smart,
    )
    POLLING_CONFIG_ONCE = (0, 0, _NH.async_poll_once)
    POLLING_CONFIG_DIAGNOSTIC = (300, mlc.PARAM_CLOUD_UPDATE_PERIOD, None)
    _NH.POLLING_CONFIG_MAP.update(
        {
            mn.Appliance_Config_Alarm: POLLING_CONFIG_CONFIGURATION,
            mn.Appliance_Config_Sensor_Association: POLLING_CONFIG_CONFIGURATION,
            mn.Appliance_Control_Alarm: POLLING_CONFIG_CONFIGURATION,
            mn.Appliance_Control_FilterMaintenance: POLLING_CONFIG_SLOWSENSOR,
            mn.Appliance_Control_Light_Effect: POLLING_CONFIG_CONFIGURATION,
            mn.Appliance_Control_PhysicalLock: POLLING_CONFIG_CONFIGURATION,
            mn.Appliance_Control_Presence_Config: POLLING_CONFIG_CONFIGURATION,
            mn.Appliance_Control_Sensor_Latest: POLLING_CONFIG_FASTSENSOR,
            mn.Appliance_Control_Sensor_LatestX: POLLING_CONFIG_FASTSENSOR,
            mn.Appliance_Mcu_Firmware: POLLING_CONFIG_ONCE,
            mn.Appliance_Mcu_Hp110_Firmware: POLLING_CONFIG_ONCE,
        }
    )

    def __init_subclass__(cls):
        super().__init_subclass__()
        # Since NamespaceHandler cannot be slotted itself because of mixin-ing with ParserEntity
        # in EntityNamespaceMixin we try this trick to provide automatic slotting for all the subclasses
        # which are not mixed with parsers and which don't define their own __slots__.
        if not issubclass(cls, NamespaceParser):
            cls.__slots__ = cls._calc_slots()

    @override
    def _handle(self, message: "MerossMessage", /):
        device = self.parent
        if device.create_diagnostic_entities:
            # since we're parsing an unknown namespace, our euristic about
            # the key_namespace might be wrong so we use another euristic
            ns = self.id
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
            _NH._handle(self, message)

    @override
    def _handle_missing_parser(self, p_channel: dict, ke: KeyError, /):
        channel = p_channel[self.id.key_idx]
        if channel in self.parsers:
            # KeyError raised inside parser function, not on missing parser
            self.log_parser_exception(ke, p_channel)
            return

        # TODO: move to base. We must decide on diagnostic parser installations
        if self.parser_class:
            self.register_parser(
                self.parent.add_entity(
                    self.parser_class(channel, self.parent, ns=self.id)
                )
            )
        elif self.parent.create_diagnostic_entities:
            from ..sensor import DiagnosticParser

            self.register_parser(
                DiagnosticParser(channel, self.parent, entity_key=self.id.key)
            )
        else:
            self.parsers[channel] = self._parse_stub

        self.parsers[channel](p_channel)


class EntityDefNamespaceHandler(NamespaceHandler):
    """
    Special 'entity definer' namespace handler used to define entities based on the presence of keys in the payload.
    This is intended to be used with namespaces which have a 'flat' payload structure with multiple keys representing
    different entities (like Appliance.Control.Diffuser.Sensor).
    The parsers member is hacked a bit so this could be dangerous with lifecycle management.
    Entities are typically created on the fly and stored in parsers (instead of registering the ParserFunc).
    The entities cleanup/shutdown will be managed by the device as usual but we have to cleanup the parsers map.
    """

    if TYPE_CHECKING:

        parsers: Final[dict[str, ValueParser]]  # type: ignore[override]

        init_entity_defs: ClassVar[Mapping[str, type[ValueParser]]]
        entity_defs: Mapping[str, type[ValueParser]]

        class Args(NamespaceHandler.Args):
            entity_defs: NotRequired[Mapping[str, type[ValueParser]]]

    SLOTS_AUTO_INIT = ("entity_defs",)

    def shutdown(self):
        self.parsers.clear()
        super().shutdown()

    def _handle(self, message: "MerossMessage", /):

        parsers = self.parsers
        for key, value in message.payload[self.id.key].items():
            try:
                parsers[key].update_device_value(value)
            except KeyError:
                # assert KeyError is due to missing parser ?
                try:
                    parsers[key] = self.parent.add_entity(
                        self.entity_defs[key](
                            None,
                            self.parent,
                            ns=self.id,
                            key_value=key,
                            device_value=value,
                        )
                    )
                except Exception as e:
                    self.log_exception(
                        self.DEBUG,
                        e,
                        "creating entity for '%s' key in '%s' namespace",
                        key,
                        self.id,
                    )
