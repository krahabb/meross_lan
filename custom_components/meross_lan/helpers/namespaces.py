from __future__ import annotations

import typing

from .. import const as mlc
from ..merossclient import NAMESPACE_TO_KEY, const as mc, request_get, request_push

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

    - handler: specify a custom handler method for this namespace. By default
    it will be looked-up in the device definition (looking for _handle_xxxxxx)

    - entity_class: specify a MerossEntity type (actually an implementation
    of Merossentity) to be instanced whenever a message for a particular channel
    is received and the channel has no parser associated (see _handle_list)

    """

    __slots__ = (
        "device",
        "namespace",
        "key_namespace",
        "lastrequest",
        "handler",
        "entity_class",
        "entities",
    )

    def __init__(
        self,
        device: MerossDevice,
        namespace: str,
        *,
        handler: Callable[[dict, dict], None] | None = None,
        entity_class: type[MerossEntity] | None = None,
    ):
        assert (
            namespace not in device.namespace_handlers
        ), "namespace already registered"
        self.device: typing.Final = device
        self.namespace: typing.Final = namespace
        self.key_namespace = NAMESPACE_TO_KEY[namespace]
        if entity_class:
            self.entity_class = entity_class
            self.handler = self._handle_list
            self.device.platforms.setdefault(entity_class.PLATFORM)
        else:
            self.entity_class = None
            self.handler = handler or getattr(
                device, f"_handle_{namespace.replace('.', '_')}", self._handle_undefined
            )
        self.lastrequest = 0
        self.entities: dict[object, Callable[[dict], None]] = {}
        device.namespace_handlers[namespace] = self

    def register_entity(self, entity: MerossEntity):
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
        assert entity.channel not in self.entities, "entity already registered"
        self.entities[entity.channel] = getattr(
            entity, f"_parse_{self.key_namespace}", entity._parse
        )
        entity.namespace_handlers.add(self)
        self.handler = self._handle_list

    def unregister(self, entity: MerossEntity):
        if self.entities.pop(entity.channel, None):
            entity.namespace_handlers.remove(self)

    def handle_exception(self, exception: Exception, function_name: str, payload):
        device = self.device
        device.log_exception(
            device.WARNING,
            exception,
            "%s(%s).%s: payload=%s",
            self.__class__.__name__,
            self.namespace,
            function_name,
            device.loggable_any(payload),
        )

    def _handle_list(self, header, payload):
        """
        splits and forwards the received NS payload to
        the registered entity(es).
        This handler si optimized for list payloads:
        "payload": { "key_namespace": [{"channel":...., ...}] }
        """
        try:
            for p_channel in payload[self.key_namespace]:
                try:
                    self.entities[p_channel[mc.KEY_CHANNEL]](p_channel)
                except KeyError as key_error:
                    channel = key_error.args[0]
                    if channel != mc.KEY_CHANNEL and self.entity_class:
                        # ensure key represents a channel and not the "channel" key
                        # in the p_channel dict
                        self.entity_class(self.device, channel)
                        self.entities[channel](p_channel)
                    else:
                        raise key_error

        except TypeError:
            # this might be expected: the payload is not a list
            self.handler = self._handle_dict
            self._handle_dict(header, payload)

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

    def _handle_generic(self, header, payload):
        """
        splits and forwards the received NS payload to
        the registered entity(es)
        This handler can manage both lists or dicts or even
        payloads without the "channel" key (see namespace Toggle)
        which will default forwarding to channel == 0
        """
        p_channel = payload[self.key_namespace]
        if isinstance(p_channel, dict):
            self.entities[p_channel.get(mc.KEY_CHANNEL, 0)](p_channel)
        else:
            for p_channel in p_channel:
                self.entities[p_channel[mc.KEY_CHANNEL]](p_channel)

    def _handle_undefined(self, header: dict, payload: dict):
        device = self.device
        device.log(
            device.DEBUG,
            "Handler undefined for method:%s namespace:%s payload:%s",
            header[mc.KEY_METHOD],
            header[mc.KEY_NAMESPACE],
            str(device.loggable_dict(payload)),
            timeout=14400,
        )
        if device.create_diagnostic_entities:
            # since we're parsing an unknown namespace, our euristic about
            # the key_namespace might be wrong so we use another euristic
            for key, payload in payload.items():
                # payload = payload[self.key_namespace]
                if isinstance(payload, dict):
                    self._parse_undefined_dict(
                        key, payload, payload.get(mc.KEY_CHANNEL)
                    )
                else:
                    for payload in payload:
                        # not having a "channel" in the list payloads is unexpected so far
                        self._parse_undefined_dict(
                            key, payload, payload[mc.KEY_CHANNEL]
                        )

    def _parse_list(self, digest: list):
        """twin method for _handle (same job - different context).
        Used when parsing digest(s) in NS_ALL"""
        try:
            for channel_digest in digest:
                self.entities[channel_digest[mc.KEY_CHANNEL]](channel_digest)
        except Exception as exception:
            self.handle_exception(exception, "_parse_list", digest)

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
            self.handle_exception(exception, "_parse_generic", digest)

    def _parse_undefined_dict(self, key: str, payload: dict, channel: object | None):
        device_entities = self.device.entities
        for subkey, subvalue in payload.items():
            if isinstance(subvalue, dict):
                self._parse_undefined_dict(f"{key}_{subkey}", subvalue, channel)
                continue
            if isinstance(subvalue, list):
                self._parse_undefined_list(f"{key}_{subkey}", subvalue, channel)
                continue
            if subkey in {
                mc.KEY_ID,
                mc.KEY_CHANNEL,
                mc.KEY_LMTIME,
                mc.KEY_LMTIME_,
                mc.KEY_SYNCEDTIME,
                mc.KEY_LATESTSAMPLETIME,
            }:
                continue
            entitykey = f"{key}_{subkey}"
            try:
                device_entities[
                    f"{channel}_{entitykey}" if channel is not None else entitykey
                ].update_native_value(subvalue)
            except KeyError:
                from ..sensor import MLDiagnosticSensor

                device = self.device
                MLDiagnosticSensor(
                    device,
                    channel,
                    entitykey,
                    native_value=subvalue,
                )
                # we'll also create a polling strategy on the fly so that
                # the diagnostic sensors get updated
                if self.namespace not in device.polling_strategies:
                    DiagnosticPollingStrategy(device, self.namespace)

    def _parse_undefined_list(self, key: str, payload: list, channel):
        pass


class VoidNamespaceHandler(NamespaceHandler):
    """Utility class to manage namespaces which should be 'ignored' i.e. we're aware
    of their existence but we don't process them at the device level. This class in turn
    just provides an empty handler and so suppresses any log too (for unknown namespaces)
    done by the base default handling."""

    def __init__(self, device: MerossDevice, namespace: str):
        super().__init__(device, namespace, handler=self._handle_void)

    def _handle_void(self, header: dict, payload: dict):
        pass


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
        "response_base_size",
        "response_item_size",
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
        handler: Callable[[dict, dict], None] | None = None,
    ):
        assert namespace not in device.polling_strategies
        self.namespace: Final = namespace
        self.key_namespace = NAMESPACE_TO_KEY[namespace]
        self.lastrequest = 0
        if _conf := mlc.POLLING_STRATEGY_CONF.get(namespace):
            self.polling_period = _conf[0]
            self.polling_period_cloud = _conf[1]
            self.response_base_size = _conf[2]
            self.response_item_size = _conf[3]
        else:
            # these in turn are defaults for dynamically parsed
            # namespaces managed when using create_diagnostic_entities
            self.polling_period = mlc.PARAM_SIGNAL_UPDATE_PERIOD
            self.polling_period_cloud = mlc.PARAM_CLOUDMQTT_UPDATE_PERIOD
            self.response_base_size = mlc.PARAM_HEADER_SIZE
            self.response_item_size = 0
        self.response_size = (
            self.response_base_size + item_count * self.response_item_size
        )

        if payload is None:
            self.request = (
                request_push(namespace)
                if namespace in mc.PUSH_ONLY_NAMESPACES
                else request_get(namespace)
            )
        else:
            self.request = (
                namespace,
                mc.METHOD_GET,
                {self.key_namespace: payload},
            )
        device.polling_strategies[namespace] = self
        if handler:
            assert namespace not in device.namespace_handlers
            NamespaceHandler(device, namespace, handler=handler)

    def adjust_size(self, item_count: int):
        self.response_size = (
            self.response_base_size + item_count * self.response_item_size
        )

    def increment_size(self):
        self.response_size += self.response_item_size

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

    async def async_trace(self, device: MerossDevice, protocol: str | None):
        """
        Used while tracing abilities. In general, we use an euristic 'default'
        query but for some 'well known namespaces' we might be better off querying with
        a better structured payload.
        """
        if protocol is mlc.CONF_PROTOCOL_HTTP:
            await device.async_http_request(*self.request)
        elif protocol is mlc.CONF_PROTOCOL_MQTT:
            await device.async_mqtt_request(*self.request)
        else:
            await device.async_request(*self.request)


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
        handler: Callable[[dict, dict], None] | None = None,
    ):
        self.entity = entity
        super().__init__(device, namespace, item_count=item_count, handler=handler)

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


class DiagnosticPollingStrategy(SmartPollingStrategy):
    """
    This strategy is for namespace polling when diagnostics sensors are
    detected and installed due to any unknown namespace parsing.
    This in turn needs to be removed from polling when diagnostic sensors is disabled
    """
