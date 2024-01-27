from __future__ import annotations

import typing

from .. import const as mlc
from ..merossclient import NAMESPACE_TO_KEY, const as mc, get_default_arguments

if typing.TYPE_CHECKING:
    from typing import Callable, Final

    from ..meross_device import MerossDevice
    from ..meross_entity import MerossEntity


class NamespaceHandler:
    """
    This is the root class for somewhat dynamic namespace handlers.
    Every device keeps its own list of method handlers indexed through
    the message namespace in order to speed up parsing/routing when receiving
    a message from the device see MerossDevice.namespace_handlers and
    MerossDevice._handle to get the basic behavior.
    Actually, every namespace handler is defined as a MerossDevice method with
    a well defined signature but this requires a bit of string manipulation on
    every incoming message. Also, the PollingStrategy class is itself related to
    a specific namespace polling/handling system and inherits from this basic class
    At runtime, the list of handlers is 'lazily' built when we receive the namespace
    for the first time
    """

    __slots__ = (
        "device",
        "namespace",
        "key_namespace",
        "lastrequest",
        "handler",
        "entities",
    )

    def __init__(
        self,
        device: MerossDevice,
        namespace: str,
        *,
        handler: Callable[[dict, dict], None] | None = None,
    ):
        self.device: typing.Final = device
        self.namespace: typing.Final = namespace
        self.key_namespace = NAMESPACE_TO_KEY[namespace]
        # this 'default' handler mapping might become obsolete
        # as soon as we move our handlers to the entity register/unregister
        # metaphore. As for now it is no harm
        self.handler = handler or getattr(
            device, f"_handle_{namespace.replace('.', '_')}", device._handle_undefined
        )
        self.lastrequest = 0
        self.entities: dict[object, Callable[[dict], None]] = {}
        device.namespace_handlers[namespace] = self

    def register(
        self, entity: MerossEntity, parse_func: Callable[[dict], None] | None = None
    ):
        # when setting up the entity-dispatching we'll substitute the legacy handler
        # (used to be a MerossDevice method with syntax like _handle_Appliance_xxx_xxx)
        # with our _handle_list, _handle_dict, _handle_generic. The 3 versions are meant
        # to be optimized against a well known type of payload. We're starting by guessing our
        # payload is a list but we'll dynamically adjust this whenever we find (in real world)
        # a different payload structure so we can adapt.
        # As an example of why this is needed, many modern payloads are just lists (
        # Thermostat payloads for instance) but many older ones are not, and still
        # either carry dict or, worse, could present themselves in both forms
        # (ToggleX is a well-known example)
        self.handler = self._handle_list
        assert entity.channel is not None
        self.entities[entity.channel] = parse_func or getattr(
            entity, f"_parse_{self.key_namespace}", entity._parse_undefined
        )

    def unregister(self, entity: MerossEntity):
        self.entities.pop(entity.channel, None)

    def _handle_list(self, header, payload):
        """
        splits and forwards the received NS payload to
        the registered entity(es).
        This handler si optimized for list payloads:
        "payload": { "key_namespace": [{"channel":...., ...}] }
        """
        try:
            for p_channel in payload[self.key_namespace]:
                self.entities[p_channel[mc.KEY_CHANNEL]](p_channel)
        except TypeError:
            # this might be expected: the payload is not a list
            self.handler = self._handle_dict
            self._handle_dict(header, payload)
        except Exception as exception:
            device = self.device
            device.log_exception(
                device.WARNING,
                exception,
                "NamespaceHandler(%s)._handle_list: payload=%s",
                self.namespace,
                device.loggable_dict(payload),
            )

    def _handle_dict(self, header, payload):
        """
        splits and forwards the received NS payload to
        the registered entity(es).
        This handler si optimized for dict payloads:
        "payload": { "key_namespace": {"channel":...., ...} }
        """
        try:
            p_channel = payload[self.key_namespace]
            self.entities[p_channel[mc.KEY_CHANNEL]](p_channel)
        except TypeError:
            # this might be expected: the payload is not a dict
            # final fallback to the safe _handle_generic
            self.handler = self._handle_generic
            self._handle_generic(header, payload)
        except Exception as exception:
            device = self.device
            device.log_exception(
                device.WARNING,
                exception,
                "NamespaceHandler(%s)._handle_dict: payload=%s",
                self.namespace,
                device.loggable_dict(payload),
            )

    def _handle_generic(self, header, payload):
        """
        splits and forwards the received NS payload to
        the registered entity(es)
        This handler can manage both lists or dicts or even
        payloads without the "channel" key (see namespace Toggle)
        which will default forwarding to channel == 0
        """
        try:
            p_channel = payload[self.key_namespace]
            if isinstance(p_channel, dict):
                self.entities[p_channel.get(mc.KEY_CHANNEL, 0)](p_channel)
            else:
                for p_channel in p_channel:
                    self.entities[p_channel[mc.KEY_CHANNEL]](p_channel)
        except Exception as exception:
            device = self.device
            device.log_exception(
                device.WARNING,
                exception,
                "NamespaceHandler(%s)._handle_generic: payload=%s",
                self.namespace,
                device.loggable_dict(payload),
            )

    def _parse_list(self, digest: list):
        """twin method for _handle (same job - different context).
        Used when parsing digest(s) in NS_ALL"""
        try:
            for channel_digest in digest:
                self.entities[channel_digest[mc.KEY_CHANNEL]](channel_digest)
        except Exception as exception:
            device = self.device
            device.log_exception(
                device.WARNING,
                exception,
                "NamespaceHandler(%s)._parse_list: digest=%s",
                self.namespace,
                device.loggable_any(digest),
            )

    def _parse_generic(self, digest):
        """twin method for _handle (same job - different context).
        Used when parsing digest(s) in NS_ALL"""
        try:
            if isinstance(digest, dict):
                self.entities[digest.get(mc.KEY_CHANNEL, 0)](digest)
            else:
                for channel_digest in digest:
                    self.entities[channel_digest[mc.KEY_CHANNEL]](channel_digest)
        except Exception as exception:
            device = self.device
            device.log_exception(
                device.WARNING,
                exception,
                "NamespaceHandler(%s)._parse_generic: digest=%s",
                self.namespace,
                device.loggable_any(digest),
            )


class PollingStrategy:
    """
    These helper class(es) is used to implement 'smart' polling
    based on current state of device, especially regarding MQTT availability.
    In fact, on MQTT we can receive almost all of the state through async PUSHES
    and we so avoid any polling. This is not true for everything (for example it looks
    in general that configurations are not pushed though). We use the namespace
    to decide which policy is best for.
    See 'poll' implementation(s) for the different behaviors
    """

    __slots__ = (
        "namespace",
        "key_namespace",
        "lastrequest",
        "polling_period",
        "polling_period_cloud",
        "response_size",
        "request",
    )

    def __init__(
        self,
        device: MerossDevice,
        namespace: str,
        *,
        payload=None,
        item_count: int = 0,
    ):
        assert namespace not in device.polling_strategies
        self.namespace: Final = namespace
        self.key_namespace = NAMESPACE_TO_KEY[namespace]
        self.lastrequest = 0
        _conf = mlc.POLLING_STRATEGY_CONF[namespace]
        self.polling_period = _conf[0]
        self.polling_period_cloud = _conf[1]
        self.response_size = _conf[2] + item_count * _conf[3]
        self.request = (
            get_default_arguments(namespace)
            if payload is None
            else (
                namespace,
                mc.METHOD_GET,
                {self.key_namespace: payload},
            )
        )
        device.polling_strategies[namespace] = self

    def adjust_size(self, item_count: int):
        _conf = mlc.POLLING_STRATEGY_CONF[self.namespace]
        self.response_size = _conf[2] + item_count * _conf[3]

    def increment_size(self):
        self.response_size += mlc.POLLING_STRATEGY_CONF[self.namespace][3]

    async def async_poll(self, device: MerossDevice, epoch: float):
        """
        This is a basic 'default' policy:
        - avoid the request when MQTT available (this is for general 'state' namespaces like NS_ALL) and
        we expect this namespace to be updated by PUSH(es)
        - unless the 'lastrequest' is 0 which means we're re-onlining the device and so
        we like to re-query the full state (even on MQTT)
        - as an optimization, when onlining we'll skip the request if it's for
        the same namespace by not calling this strategy (see MerossDevice.async_request_updates)
        """
        if not (device._mqtt_active and self.lastrequest):
            self.lastrequest = epoch
            await device.async_request_poll(self)

    async def async_trace(self, device: MerossDevice):
        """
        Used while tracing abilities. In general, we use an euristic 'default'
        query but for some 'well known namespaces' we might be better off querying with
        a better structured payload.
        """
        await device.async_request_poll(self)
        # this is to not 'pack' abilities tracing into ns_multiple
        await device.async_request_flush()


class SmartPollingStrategy(PollingStrategy):
    """
    This is a strategy for polling states which are not actively pushed so we should
    always query them (eventually with a variable timeout depending on the relevant
    time dynamics of the sensor/state). When using cloud MQTT though we have to be very
    conservative on traffic so we eventually delay the request onto the next cycle
    if this 'pass' is already crowded (see device.async_request_smartpoll)
    """

    async def async_poll(self, device: MerossDevice, epoch: float):
        if (epoch - self.lastrequest) >= self.polling_period:
            await device.async_request_smartpoll(self, epoch)


class EntityPollingStrategy(SmartPollingStrategy):
    __slots__ = ("entity",)

    def __init__(
        self,
        device: MerossDevice,
        namespace: str,
        entity: MerossEntity,
        *,
        item_count: int = 0,
    ):
        self.entity = entity
        super().__init__(device, namespace, item_count=item_count)

    async def async_poll(self, device: MerossDevice, epoch: float):
        """
        Same as SmartPollingStrategy but we have a 'relevant' entity associated with
        the state of this paylod so we'll skip the smartpoll should the entity be disabled
        """
        if self.entity.enabled:
            await super().async_poll(device, epoch)


class OncePollingStrategy(SmartPollingStrategy):
    """
    This strategy is for 'constant' namespace data which do not change and only
    need to be requested once (after onlining that is).
    """

    async def async_poll(self, device: MerossDevice, epoch: float):
        """
        Same as SmartPollingStrategy (don't overwhelm the cloud mqtt)
        """
        if not self.lastrequest:
            await device.async_request_smartpoll(self, epoch)
