import bisect
import typing

from .. import const as mlc
from ..merossclient import const as mc, namespaces as mn

if typing.TYPE_CHECKING:

    from ..meross_device import MerossDevice
    from ..meross_entity import MerossEntity

PollingStrategyFunc = typing.Callable[
    ["NamespaceHandler", "MerossDevice", float], typing.Coroutine
]


class EntityDisablerMixin:
    """
    Special 'disabler' mixin used when the device pushes a message for a 'not yet'
    known entity/channel. The namespace handler will then dynamically mixin this
    disabler into the entity instance class initialization
    """

    # HA core entity attributes:
    entity_registry_enabled_default = False


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
        "ns",
        "namespace",
        "key_namespace",
        "lastrequest",
        "lastresponse",
        "handler",
        "entities",
        "entity_class",
        "polling_strategy",
        "polling_period",
        "polling_period_cloud",
        "polling_response_base_size",
        "polling_response_item_size",
        "polling_response_size",
        "polling_request",
        "polling_request_payload",
    )

    def __init__(
        self,
        device: "MerossDevice",
        namespace: str,
        *,
        entity_class: type["MerossEntity"] | None = None,
        handler: typing.Callable[[dict, dict], None] | None = None,
    ):
        assert (
            namespace not in device.namespace_handlers
        ), "namespace already registered"
        self.device = device
        self.ns = ns = mn.NAMESPACES[namespace]
        self.namespace = namespace
        self.key_namespace = ns.key
        self.lastresponse = self.lastrequest = 0.0
        self.entities: dict[object, typing.Callable[[dict], None]] = {}
        if entity_class:
            assert not handler
            self.register_entity_class(entity_class)
        else:
            self.entity_class = None
            self.handler = handler or getattr(
                device, f"_handle_{namespace.replace('.', '_')}", self._handle_undefined
            )

        if _conf := POLLING_STRATEGY_CONF.get(namespace):
            self.polling_period = _conf[0]
            self.polling_period_cloud = _conf[1]
            self.polling_response_base_size = _conf[2]
            self.polling_response_item_size = _conf[3]
            self.polling_strategy = _conf[4]
        else:
            # these in turn are defaults for dynamically parsed
            # namespaces managed when using create_diagnostic_entities
            self.polling_period = mlc.PARAM_DIAGNOSTIC_UPDATE_PERIOD
            self.polling_period_cloud = mlc.PARAM_CLOUDMQTT_UPDATE_PERIOD
            self.polling_response_base_size = mlc.PARAM_HEADER_SIZE
            self.polling_response_item_size = 0
            self.polling_strategy = None

        if ns.need_channel:
            self.polling_request_payload = []
            self.polling_request = (
                namespace,
                mc.METHOD_GET,
                {ns.key: self.polling_request_payload},
            )
        else:
            self.polling_request_payload = None
            self.polling_request = ns.request_default

        # by default we calculate 1 item/channel per payload but we should
        # refine this whenever needed
        item_count = 1
        self.polling_response_size = (
            self.polling_response_base_size
            + item_count * self.polling_response_item_size
        )
        device.namespace_handlers[namespace] = self

    def polling_response_size_adj(self, item_count: int):
        self.polling_response_size = (
            self.polling_response_base_size
            + item_count * self.polling_response_item_size
        )

    def polling_response_size_inc(self):
        self.polling_response_size += self.polling_response_item_size

    def register_entity_class(self, entity_class: type["MerossEntity"]):
        self.entity_class = type(
            entity_class.__name__, (EntityDisablerMixin, entity_class), {}
        )
        self.handler = self._handle_list
        self.device.platforms.setdefault(entity_class.PLATFORM)

    def register_entity(self, entity: "MerossEntity"):
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
        channel = entity.channel
        assert channel not in self.entities, "entity already registered"
        self.entities[channel] = getattr(
            entity, f"_parse_{self.key_namespace}", entity._parse
        )
        entity.namespace_handlers.add(self)

        polling_request_payload = self.polling_request_payload
        if polling_request_payload is not None:
            for channel_payload in polling_request_payload:
                if channel_payload[mc.KEY_CHANNEL] == channel:
                    break
            else:
                polling_request_payload.append({mc.KEY_CHANNEL: channel})
                self.polling_response_size = (
                    self.polling_response_base_size
                    + len(polling_request_payload) * self.polling_response_item_size
                )

        self.handler = self._handle_list

    def unregister(self, entity: "MerossEntity"):
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
            str(device.loggable_any(payload)),
            timeout=604800,
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
                    _parse = self.entities[p_channel[mc.KEY_CHANNEL]]
                except KeyError as key_error:
                    _parse = self._try_create_entity(key_error)
                _parse(p_channel)
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
        p_channel = payload[self.key_namespace]
        try:
            _parse = self.entities[p_channel.get(mc.KEY_CHANNEL)]
        except KeyError as key_error:
            _parse = self._try_create_entity(key_error)
        except AttributeError:
            # this might be expected: the payload is not a dict
            # final fallback to the safe _handle_generic
            self.handler = self._handle_generic
            self._handle_generic(header, payload)
            return
        _parse(p_channel)

    def _handle_generic(self, header, payload):
        """
        splits and forwards the received NS payload to
        the registered entity(es)
        This handler can manage both lists or dicts or even
        payloads without the "channel" key (see namespace Toggle)
        which will default forwarding to channel == None
        """
        p_channel = payload[self.key_namespace]
        if type(p_channel) is dict:
            try:
                _parse = self.entities[p_channel.get(mc.KEY_CHANNEL)]
            except KeyError as key_error:
                _parse = self._try_create_entity(key_error)
            _parse(p_channel)
        else:
            for p_channel in p_channel:
                try:
                    _parse = self.entities[p_channel[mc.KEY_CHANNEL]]
                except KeyError as key_error:
                    _parse = self._try_create_entity(key_error)
                _parse(p_channel)

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

    def parse_list(self, digest: list):
        """twin method for _handle (same job - different context).
        Used when parsing digest(s) in NS_ALL"""
        try:
            for p_channel in digest:
                try:
                    _parse = self.entities[p_channel[mc.KEY_CHANNEL]]
                except KeyError as key_error:
                    _parse = self._try_create_entity(key_error)
                _parse(p_channel)
        except Exception as exception:
            self.handle_exception(exception, "_parse_list", digest)

    def parse_generic(self, digest: list | dict):
        """twin method for _handle (same job - different context).
        Used when parsing digest(s) in NS_ALL"""
        try:
            if type(digest) is dict:
                self.entities[digest.get(mc.KEY_CHANNEL)](digest)
            else:
                for p_channel in digest:
                    try:
                        _parse = self.entities[p_channel[mc.KEY_CHANNEL]]
                    except KeyError as key_error:
                        _parse = self._try_create_entity(key_error)
                    _parse(p_channel)
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
                if not self.polling_strategy:
                    self.polling_strategy = NamespaceHandler.async_poll_diagnostic

    def _parse_undefined_list(self, key: str, payload: list, channel):
        pass

    def _try_create_entity(self, key_error: KeyError):
        if not self.entity_class:
            raise key_error
        channel = key_error.args[0]
        if channel == mc.KEY_CHANNEL:
            # ensure key represents a channel and not the "channel" key
            # in the p_channel dict
            raise key_error
        self.entity_class(self.device, channel)
        return self.entities[channel]

    async def async_poll_all(self, device: "MerossDevice", epoch: float):
        """
        This is a special policy for NS_ALL.
        It is basically an 'async_poll_default' policy so it kicks-in whenever we poll
        the state in 'device._async_request_updates' but contrary to 'legacy' behavior
        where NS_ALL was always polled (unless mqtt active).
        This will alternate polling NS_ALL to the group of namespaces responsible for
        the state carried in 'digest'. This is an improvement since NS_ALL, even if carrying
        the whole state in one query, might be huge (because of the 'time' key) but also because
        most of its data are pretty static (never or seldom changing) info of the device.
        This new policy will interleave querying NS_ALL once in a while with smaller direct
        equivalent queries for the state carried in digest. (If the device doesn't support
        NS_MULTIPLE, it will likely do more queries though but this is unlikely)
        """
        if device._mqtt_active:
            # on MQTT no need for updates since they're being PUSHed
            if not self.lastrequest:
                # just when onlining...
                self.lastrequest = epoch
                await device.async_request_poll(self)
            return

        # here we're missing PUSHed updates so we have to poll...
        if (epoch - self.lastrequest) > mlc.PARAM_HEARTBEAT_PERIOD:
            # at start or periodically ask for NS_ALL..plain
            self.lastrequest = epoch
            await device.async_request_poll(self)
            return

        # query specific namespaces instead of NS_ALL since we hope this is
        # better (less overhead/http sessions) together with ns_multiple packing
        for digest_poller in device.digest_pollers:
            if digest_poller.entities:
                # don't query if digest key/namespace hasn't any entity registered
                # this also prevents querying a somewhat 'malformed' ToggleX reply
                # appearing in an mrs100 (#447)
                digest_poller.lastrequest = epoch
                await device.async_request_poll(digest_poller)

    async def async_poll_default(self, device: "MerossDevice", epoch: float):
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

    async def async_poll_lazy(self, device: "MerossDevice", epoch: float):
        """
        This strategy is for those namespaces which might be skipped now and then
        if they don't fit in the current ns_multiple request. Their delaying
        would be no harm since they typically carry rather unchanging values
        or data which are not 'critical'. For those namespaces, polling_period
        is considered the maximum amount of time after which the poll 'has' to
        be done. If it hasn't elpased then they're eventually packed
        with the outgoing ns_multiple
        """
        if (epoch - self.lastrequest) >= self.polling_period:
            await device.async_request_smartpoll(self, epoch)
        else:
            # insert into the lazypoll_requests ordering by least recently polled
            def _lazypoll_key(_handler: NamespaceHandler):
                return _handler.lastrequest - epoch

            bisect.insort(device.lazypoll_requests, self, key=_lazypoll_key)

    async def async_poll_smart(self, device: "MerossDevice", epoch: float):
        if (epoch - self.lastrequest) >= self.polling_period:
            await device.async_request_smartpoll(self, epoch)

    async def async_poll_once(self, device: "MerossDevice", epoch: float):
        """
        This strategy is for 'constant' namespace data which do not change and only
        need to be requested once (after onlining that is). When polling use
        same queueing policy as async_poll_smart (don't overwhelm the cloud mqtt),
        """
        if not self.lastrequest:
            await device.async_request_smartpoll(self, epoch)

    async def async_poll_diagnostic(self, device: "MerossDevice", epoch: float):
        """
        This strategy is for namespace polling when diagnostics sensors are detected and
        installed due to any unknown namespace parsing (see self._parse_undefined_dict).
        This in turn needs to be removed from polling when diagnostic sensors are disabled.
        The strategy itself is the same as async_poll_smart; the polling settings
        (period, payload size, etc) has been defaulted in self.__init__ when the definition
        for the namespace polling has not been found in POLLING_STRATEGY_CONF
        """
        if (epoch - self.lastrequest) >= self.polling_period:
            await device.async_request_smartpoll(self, epoch)

    async def async_trace(self, device: "MerossDevice", protocol: str | None):
        """
        Used while tracing abilities. In general, we use an euristic 'default'
        query but for some 'well known namespaces' we might be better off querying with
        a better structured payload.
        """
        if protocol is mlc.CONF_PROTOCOL_HTTP:
            await device.async_http_request(*self.polling_request)
        elif protocol is mlc.CONF_PROTOCOL_MQTT:
            await device.async_mqtt_request(*self.polling_request)
        else:
            await device.async_request(*self.polling_request)


class EntityNamespaceMixin(MerossEntity if typing.TYPE_CHECKING else object):
    """
    Special 'polling enabler/disabler' mixin used with entities which are
    'single instance' for a namespace handler and so they'll disable polling
    should they're disabled in HA.
    """

    manager: "MerossDevice"

    async def async_added_to_hass(self):
        self.manager.get_handler(self.namespace).polling_strategy = (
            POLLING_STRATEGY_CONF[self.namespace][4]
        )
        return await super().async_added_to_hass()

    async def async_will_remove_from_hass(self):
        self.manager.get_handler(self.namespace).polling_strategy = None
        return await super().async_will_remove_from_hass()


class EntityNamespaceHandler(NamespaceHandler):
    """
    Utility class to manage namespaces which are mapped to a single entity.
    This will acts as an helper in initialization
    """

    def __init__(self, entity: "EntityNamespaceMixin"):
        NamespaceHandler.__init__(
            self,
            entity.manager,
            entity.namespace,
            handler=getattr(
                entity, f"_handle_{entity.namespace.replace('.', '_')}", entity._handle
            ),
        )
        if not entity._hass_connected:
            # if initially disabled then uninstall default strategy
            # EntityNamespaceMixin will manage enabling/disabling
            self.polling_strategy = None


class VoidNamespaceHandler(NamespaceHandler):
    """Utility class to manage namespaces which should be 'ignored' i.e. we're aware
    of their existence but we don't process them at the device level. This class in turn
    just provides an empty handler and so suppresses any log too (for unknown namespaces)
    done by the base default handling."""

    def __init__(self, device: "MerossDevice", namespace: str):
        NamespaceHandler.__init__(self, device, namespace, handler=self._handle_void)

    def _handle_void(self, header: dict, payload: dict):
        pass


"""
Default timeouts and config parameters for polled namespaces.
The configuration is set in the tuple as:
(
    polling_timeout,
    polling_timeout_cloud,
    response_base_size,
    response_item_size,
    strategy
)
see the NamespaceHandler class for the meaning of these values
The 'response_size' is a conservative (in excess) estimate of the
expected response size for the whole message (header itself weights around 300 bytes).
Some payloads would depend on the number of channels/subdevices available
and the configured number would just be a base size (minimum) while
the 'response_item_size' value must be multiplied for the number of channels/subdevices
and will be used to adjust the actual 'response_size' at runtime in the relative strategy.
This parameter in turn will be used to split expected huge payload requests/responses
in Appliance.Control.Multiple since it appears the HTTP interface has an outbound
message size limit around 3000 chars/bytes (on a legacy mss310) and this would lead to a malformed (truncated)
response. This issue also appeared on hubs when querying for a big number of subdevices
as reported in #244 (here the buffer limit was around 4000 chars). From limited testing this 'kind of overflow' is not happening on MQTT
responses though
"""
POLLING_STRATEGY_CONF: dict[
    str, tuple[int, int, int, int, PollingStrategyFunc | None]
] = {
    mc.NS_APPLIANCE_SYSTEM_ALL: (0, 0, 1000, 0, NamespaceHandler.async_poll_all),
    mc.NS_APPLIANCE_SYSTEM_DEBUG: (0, 0, 1900, 0, None),
    mc.NS_APPLIANCE_SYSTEM_DNDMODE: (
        300,
        mlc.PARAM_CLOUDMQTT_UPDATE_PERIOD,
        320,
        0,
        NamespaceHandler.async_poll_lazy,
    ),
    mc.NS_APPLIANCE_SYSTEM_RUNTIME: (
        300,
        mlc.PARAM_CLOUDMQTT_UPDATE_PERIOD,
        330,
        0,
        NamespaceHandler.async_poll_lazy,
    ),
    mc.NS_APPLIANCE_CONFIG_OVERTEMP: (
        300,
        mlc.PARAM_CLOUDMQTT_UPDATE_PERIOD,
        340,
        0,
        NamespaceHandler.async_poll_lazy,
    ),
    mc.NS_APPLIANCE_CONTROL_CONSUMPTIONX: (
        mlc.PARAM_ENERGY_UPDATE_PERIOD,
        mlc.PARAM_CLOUDMQTT_UPDATE_PERIOD,
        320,
        53,
        NamespaceHandler.async_poll_smart,
    ),
    mc.NS_APPLIANCE_CONTROL_DIFFUSER_SENSOR: (
        300,
        mlc.PARAM_CLOUDMQTT_UPDATE_PERIOD,
        mlc.PARAM_HEADER_SIZE,
        100,
        NamespaceHandler.async_poll_lazy,
    ),
    mc.NS_APPLIANCE_CONTROL_ELECTRICITY: (
        0,
        mlc.PARAM_CLOUDMQTT_UPDATE_PERIOD,
        430,
        0,
        NamespaceHandler.async_poll_smart,
    ),
    mc.NS_APPLIANCE_CONTROL_FAN: (
        0,
        mlc.PARAM_CLOUDMQTT_UPDATE_PERIOD,
        mlc.PARAM_HEADER_SIZE,
        20,
        None,
    ),
    mc.NS_APPLIANCE_CONTROL_FILTERMAINTENANCE: (
        mlc.PARAM_CLOUDMQTT_UPDATE_PERIOD,
        mlc.PARAM_CLOUDMQTT_UPDATE_PERIOD,
        mlc.PARAM_HEADER_SIZE,
        35,
        NamespaceHandler.async_poll_smart,
    ),
    mc.NS_APPLIANCE_CONTROL_LIGHT_EFFECT: (
        0,
        mlc.PARAM_CLOUDMQTT_UPDATE_PERIOD,
        1850,
        0,
        NamespaceHandler.async_poll_smart,
    ),
    mc.NS_APPLIANCE_CONTROL_MP3: (0, 0, 380, 0, NamespaceHandler.async_poll_default),
    mc.NS_APPLIANCE_CONTROL_PHYSICALLOCK: (
        300,
        mlc.PARAM_CLOUDMQTT_UPDATE_PERIOD,
        mlc.PARAM_HEADER_SIZE,
        35,
        NamespaceHandler.async_poll_lazy,
    ),
    mc.NS_APPLIANCE_CONTROL_THERMOSTAT_CALIBRATION: (
        0,
        mlc.PARAM_CLOUDMQTT_UPDATE_PERIOD,
        mlc.PARAM_HEADER_SIZE,
        80,
        NamespaceHandler.async_poll_smart,
    ),
    mc.NS_APPLIANCE_CONTROL_THERMOSTAT_DEADZONE: (
        0,
        mlc.PARAM_CLOUDMQTT_UPDATE_PERIOD,
        mlc.PARAM_HEADER_SIZE,
        80,
        NamespaceHandler.async_poll_smart,
    ),
    mc.NS_APPLIANCE_CONTROL_THERMOSTAT_FROST: (
        0,
        mlc.PARAM_CLOUDMQTT_UPDATE_PERIOD,
        mlc.PARAM_HEADER_SIZE,
        80,
        NamespaceHandler.async_poll_smart,
    ),
    mc.NS_APPLIANCE_CONTROL_THERMOSTAT_OVERHEAT: (
        0,
        0,
        mlc.PARAM_HEADER_SIZE,
        140,
        NamespaceHandler.async_poll_default,
    ),
    mc.NS_APPLIANCE_CONTROL_THERMOSTAT_SCHEDULE: (
        0,
        0,
        mlc.PARAM_HEADER_SIZE,
        550,
        NamespaceHandler.async_poll_default,
    ),
    mc.NS_APPLIANCE_CONTROL_THERMOSTAT_SCHEDULEB: (
        0,
        0,
        mlc.PARAM_HEADER_SIZE,
        550,
        NamespaceHandler.async_poll_default,
    ),
    mc.NS_APPLIANCE_CONTROL_THERMOSTAT_SENSOR: (
        0,
        0,
        mlc.PARAM_HEADER_SIZE,
        40,
        NamespaceHandler.async_poll_default,
    ),
    mc.NS_APPLIANCE_CONTROL_SCREEN_BRIGHTNESS: (
        0,
        mlc.PARAM_CLOUDMQTT_UPDATE_PERIOD,
        mlc.PARAM_HEADER_SIZE,
        70,
        NamespaceHandler.async_poll_smart,
    ),
    mc.NS_APPLIANCE_GARAGEDOOR_CONFIG: (
        0,
        mlc.PARAM_CLOUDMQTT_UPDATE_PERIOD,
        410,
        0,
        NamespaceHandler.async_poll_smart,
    ),
    mc.NS_APPLIANCE_GARAGEDOOR_MULTIPLECONFIG: (
        0,
        mlc.PARAM_CLOUDMQTT_UPDATE_PERIOD,
        mlc.PARAM_HEADER_SIZE,
        140,
        NamespaceHandler.async_poll_smart,
    ),
    mc.NS_APPLIANCE_HUB_BATTERY: (
        3600,
        mlc.PARAM_CLOUDMQTT_UPDATE_PERIOD,
        mlc.PARAM_HEADER_SIZE,
        40,
        NamespaceHandler.async_poll_smart,
    ),
    mc.NS_APPLIANCE_HUB_MTS100_ADJUST: (
        mlc.PARAM_CLOUDMQTT_UPDATE_PERIOD,
        mlc.PARAM_CLOUDMQTT_UPDATE_PERIOD,
        mlc.PARAM_HEADER_SIZE,
        40,
        NamespaceHandler.async_poll_smart,
    ),
    mc.NS_APPLIANCE_HUB_MTS100_ALL: (
        0,
        mlc.PARAM_CLOUDMQTT_UPDATE_PERIOD,
        mlc.PARAM_HEADER_SIZE,
        350,
        None,
    ),
    mc.NS_APPLIANCE_HUB_MTS100_SCHEDULEB: (
        0,
        mlc.PARAM_CLOUDMQTT_UPDATE_PERIOD,
        mlc.PARAM_HEADER_SIZE,
        500,
        None,
    ),
    mc.NS_APPLIANCE_HUB_SENSOR_ADJUST: (
        mlc.PARAM_CLOUDMQTT_UPDATE_PERIOD,
        mlc.PARAM_CLOUDMQTT_UPDATE_PERIOD,
        mlc.PARAM_HEADER_SIZE,
        60,
        NamespaceHandler.async_poll_smart,
    ),
    mc.NS_APPLIANCE_HUB_SENSOR_ALL: (
        0,
        mlc.PARAM_CLOUDMQTT_UPDATE_PERIOD,
        mlc.PARAM_HEADER_SIZE,
        250,
        None,
    ),
    mc.NS_APPLIANCE_HUB_SUBDEVICE_VERSION: (
        0,
        mlc.PARAM_CLOUDMQTT_UPDATE_PERIOD,
        mlc.PARAM_HEADER_SIZE,
        55,
        NamespaceHandler.async_poll_once,
    ),
    mc.NS_APPLIANCE_HUB_TOGGLEX: (
        0,
        0,
        mlc.PARAM_HEADER_SIZE,
        35,
        NamespaceHandler.async_poll_default,
    ),
    mc.NS_APPLIANCE_ROLLERSHUTTER_ADJUST: (
        mlc.PARAM_CLOUDMQTT_UPDATE_PERIOD,
        mlc.PARAM_CLOUDMQTT_UPDATE_PERIOD,
        mlc.PARAM_HEADER_SIZE,
        35,
        NamespaceHandler.async_poll_smart,
    ),
    mc.NS_APPLIANCE_ROLLERSHUTTER_CONFIG: (
        0,
        mlc.PARAM_CLOUDMQTT_UPDATE_PERIOD,
        mlc.PARAM_HEADER_SIZE,
        70,
        NamespaceHandler.async_poll_smart,
    ),
    mc.NS_APPLIANCE_ROLLERSHUTTER_POSITION: (
        0,
        0,
        mlc.PARAM_HEADER_SIZE,
        50,
        NamespaceHandler.async_poll_default,
    ),
    mc.NS_APPLIANCE_ROLLERSHUTTER_STATE: (
        0,
        0,
        mlc.PARAM_HEADER_SIZE,
        40,
        NamespaceHandler.async_poll_default,
    ),
}
