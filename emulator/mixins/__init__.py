import asyncio
from base64 import b64decode, b64encode
from enum import Enum
from json import JSONDecodeError
import threading
from time import time
import typing
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from custom_components.meross_lan import const as mlc
from custom_components.meross_lan.helpers.manager import ConfigEntryManager
from custom_components.meross_lan.merossclient import (
    HostAddress,
    MerossDeviceDescriptor,
    extract_dict_payloads,
    get_element_by_key,
    get_macaddress_from_uuid,
    json_dumps,
    json_loads,
    update_dict_strict,
    update_dict_strict_by_key,
)
from custom_components.meross_lan.merossclient.mqttclient import MerossMQTTDeviceClient
from custom_components.meross_lan.merossclient.protocol import (
    const as mc,
    namespaces as mn,
)
from custom_components.meross_lan.merossclient.protocol.message import (
    MerossMessage,
    MerossRequest,
    build_message,
    compute_message_encryption_key,
    get_replykey,
)

if TYPE_CHECKING:
    from io import TextIOWrapper
    from typing import Any, ClassVar, Mapping

    import paho.mqtt.client as mqtt

    from custom_components.meross_lan.merossclient.protocol.namespaces import Namespace
    from custom_components.meross_lan.merossclient.protocol.types import (
        MerossHeaderType,
        MerossNamespaceType,
        MerossPayloadType,
    )


class MerossEmulatorDescriptor(MerossDeviceDescriptor):
    namespaces: "dict[MerossNamespaceType, MerossPayloadType]"

    __slots__ = ("namespaces",)

    def __init__(
        self,
        tracefile: str,
        *,
        uuid: str | None = None,
        broker: str | None = None,
        userId: int | None = None,
    ):
        self.namespaces = {}
        with open(tracefile, "r", encoding="utf8") as f:
            if tracefile.endswith(".json.txt") or tracefile.endswith(".json"):
                # HA diagnostics trace
                self._import_json(f)
            else:
                self._import_tsv(f)

        super().__init__(
            self.namespaces[mn.Appliance_System_All.name]
            | self.namespaces[mn.Appliance_System_Ability.name]
        )
        # patch system payload with fake ids
        if uuid:
            hardware = self.hardware
            hardware[mc.KEY_UUID] = uuid
            hardware[mc.KEY_MACADDRESS] = get_macaddress_from_uuid(uuid)
        if broker:
            broker_address = HostAddress.build(broker)
            firmware = self.firmware
            firmware[mc.KEY_SERVER] = broker_address.host
            firmware[mc.KEY_PORT] = broker_address.port
            firmware.pop(mc.KEY_SECONDSERVER, None)
            firmware.pop(mc.KEY_SECONDPORT, None)
        if userId:
            self.firmware[mc.KEY_USERID] = userId

    def _import_tsv(self, f: "TextIOWrapper"):
        """
        parse a legacy tab separated values meross_lan trace
        """
        row = next(f).split("\t")

        def _import_legacy_config(_row):
            ns = mn.Appliance_System_All
            self.namespaces[ns.name] = {ns.key: json_loads(_row[-1])}
            _row = next(f).split("\t")
            ns = mn.Appliance_System_Ability
            self.namespaces[ns.name] = {ns.key: json_loads(_row[-1])}

        # detect version: lot of heuristic since the structure was not so smart
        if len(row) == 5:
            # earlier versions missing 'txrx' - no column headers though
            # 2 'auto' rows carrying ns.All and ns.Ability from config
            version = 0
            _import_legacy_config(row)
        else:
            match row[2]:  # 'protocol' column
                case "auto":
                    # Version 1: no column headers - trace starting with
                    # 2 'auto' rows carrying ns.All and ns.Ability from config
                    version = 1
                    _import_legacy_config(row)
                case "protocol":
                    # Version 2 (and on): we have column headers
                    columns = row
                    row = next(f).split("\t")
                    # first data row contains an 'HEADER' i.e. 'diagnostic like' dict
                    _header: "mlc.TracingHeaderType" = json_loads(row[-1])
                    version = _header["version"]
                    config_payload = _header["config"]["payload"]
                    ns = mn.Appliance_System_All
                    self.namespaces[ns.name] = {ns.key: config_payload[ns.key]}
                    ns = mn.Appliance_System_Ability
                    self.namespaces[ns.name] = {ns.key: config_payload[ns.key]}
                    for namespace, payload in _header["state"][
                        "namespace_pushes"
                    ].items():
                        self.namespaces[namespace] = payload

        for line in f:
            row = line.split("\t")
            if not version:
                row.insert(1, "")  # patch earlier versions missing 'txrx'
            if row[2] == "auto":
                continue
            row[-1] = json_loads(row[-1])
            self._import_tracerow(*row)  # type:ignore

    def _import_json(self, f: "TextIOWrapper"):
        """
        parse a 'diagnostics' HA trace
        """
        try:
            _data: dict = json_loads(f.read())["data"]

            try:
                version = _data["version"]
            except KeyError:
                version = 1

            match version:
                case 2:
                    config_payload = _data["config"]["payload"]
                    pushes = _data["state"]["namespace_pushes"]
                    rows = iter(_data["trace"])
                    columns = next(rows)
                case 1:
                    config_payload = _data["payload"]
                    try:
                        pushes = _data["device"]["namespace_pushes"]
                    except KeyError:
                        # earlier versions missing pushes
                        pushes = {}
                    rows = iter(_data["trace"])
                    columns = next(rows)
                    # mandatory All and Ability stored in the first 2 rows
                    # now loaded from _config before parsing trace rows
                    next(rows)
                    next(rows)

            ns = mn.Appliance_System_All
            self.namespaces[ns.name] = {ns.key: config_payload[ns.key]}
            ns = mn.Appliance_System_Ability
            self.namespaces[ns.name] = {ns.key: config_payload[ns.key]}
            for namespace, payload in pushes.items():
                self.namespaces[namespace] = payload

            for row in rows:
                if row[2] == "auto":
                    continue
                self._import_tracerow(*row)

        except:
            raise

        return

    def _import_tracerow(
        self,
        epoch: str,
        rxtx: str,
        protocol: str,
        method: str,
        namespace: str,
        data: dict,
    ):

        match method:
            case mc.METHOD_PUSH:
                if rxtx == "RX" and (namespace not in self.namespaces):
                    self.namespaces[namespace] = data
            case mc.METHOD_GETACK:
                self.namespaces[namespace] = data
            case mc.METHOD_SETACK:
                if namespace == mn.Appliance_Control_Multiple.name:
                    for message in data[mc.KEY_MULTIPLE]:
                        header = message[mc.KEY_HEADER]
                        if header[mc.KEY_METHOD] == mc.METHOD_GETACK:
                            self.namespaces[header[mc.KEY_NAMESPACE]] = message[
                                mc.KEY_PAYLOAD
                            ]


class MerossEmulator:
    """
    Based off the knowledge inside the MerossEmulatorDescriptor
    this class tries to reply to an incoming request by looking
    at the vocabulary of known namespaces listed in the descriptor.
    It is also able to manage a sort of state for commands accessing
    data in the Apllication.System.All namespace at the 'digest' key
    which are the majority.
    If state is not available there it could be looked up in the specific
    command carrying the message and so automatically managed too
    """

    class NSDefaultMode(Enum):
        """Determines the type of ns state update."""

        MixIn = 0
        """Keys in provided payload overwrite existing state."""
        MixOut = 1
        """Keys in existing state are preserved (only non-existing keys are added)."""

    if typing.TYPE_CHECKING:
        NAMESPACES: ClassVar
        MAXIMUM_RESPONSE_SIZE: ClassVar

        type NSDefaultArgs = tuple[NSDefaultMode, dict]
        type NSDefault = dict[Namespace, NSDefaultArgs]
        NAMESPACES_DEFAULT: ClassVar[NSDefault]
        """Contains default data for namespaces initialization.
        Some traces could miss some important namespaces because of the way they're collected
        so we'll add the 'well-known minimum working configuration' for those namespaces which
        should (always) work in emulator (to the best of our knowledge).
        This container must be defined in every custom mixin with defaults
        relevant for the features they're implementing. The complete class defaults
        will be 'explored' in MerossEmulator.__init__."""
        NAMESPACES_DEFAULT_IGNORE: ClassVar[tuple[Namespace, ...]]

    NAMESPACES = mn.NAMESPACES

    MAXIMUM_RESPONSE_SIZE = 3000

    NAMESPACES_DEFAULT = {
        mn.Appliance_System_DNDMode: (NSDefaultMode.MixOut, {mc.KEY_MODE: 0}),
    }

    NAMESPACES_DEFAULT_IGNORE = (
        mn.Appliance_Control_Diffuser_Light,
        mn.Appliance_Control_Diffuser_Sensor,
        mn.Appliance_Control_Diffuser_Spray,
        mn.Appliance_Control_Multiple,
        mn.Appliance_System_Clock,
        mn.Appliance_System_Debug,
        mn.Appliance_System_Firmware,
        mn.Appliance_System_Hardware,
        mn.Appliance_System_Time,
        mn.Appliance_System_Online,
    )

    __slots__ = (
        "epoch",
        "lock",
        "loop",
        "key",
        "descriptor",
        "namespaces",
        "topic_response",
        "mqtt_client",
        "mqtt_connected",
        "_scheduler_unsub",
        "_tzinfo",
        "_cipher",
        "__dict__",
    )

    def __init__(self, descriptor: MerossEmulatorDescriptor, key: str, /):
        self.lock = threading.Lock()
        self.loop: asyncio.AbstractEventLoop = None  # type: ignore
        self.key = key
        self.descriptor = descriptor
        self.namespaces = namespaces = descriptor.namespaces
        namespaces_default: "MerossEmulator.NSDefault" = {}
        namespaces_default_ignore = []
        for cls in self.__class__.mro():
            if cls is object:
                continue
            try:
                namespaces_default |= cls.NAMESPACES_DEFAULT
            except AttributeError:
                pass

            try:
                namespaces_default_ignore.extend(cls.NAMESPACES_DEFAULT_IGNORE)
            except AttributeError:
                pass

        for ability in descriptor.ability:
            ns = self.NAMESPACES.get(ability)
            if ns and ns.grammar is not mn.Grammar.UNKNOWN:
                if (not (ns.has_get or ns.has_push_query)) or (
                    ns in namespaces_default_ignore
                ):
                    # not querable
                    continue

                if ns in namespaces_default:
                    _nsdefaultmode, _payload = namespaces_default[ns]
                    self.update_namespace_state(ns, _nsdefaultmode, _payload)
                    continue

                # no default state set in NAMESPACES_DEFAULT
                try:
                    p_namespace = namespaces[ability]
                    if ns.key in p_namespace:
                        continue
                except KeyError:
                    namespaces[ability] = p_namespace = {}
                # Either namespace missing or malformed according to our grammar.
                # Setup a 'default' (which will not work for hubs though...)
                # but we cannot use copy because request_payload_type.value is immutable
                # p_namespace[ns.key] = ns.request_payload_type.value.clone()

        self.topic_response = mc.TOPIC_RESPONSE.format(descriptor.uuid)
        self.mqtt_client: MerossMQTTDeviceClient = None  # type: ignore
        self.mqtt_connected = None
        self._scheduler_unsub = None
        self._tzinfo: ZoneInfo | None = None
        self._cipher = (
            Cipher(
                algorithms.AES(
                    compute_message_encryption_key(
                        descriptor.uuid, key, descriptor.macAddress
                    ).encode("utf-8")
                ),
                modes.CBC("0000000000000000".encode("utf8")),
            )
            if mn.Appliance_Encrypt_ECDHE.name in descriptor.ability
            else None
        )
        self.update_epoch()

    async def async_startup(self, *, enable_scheduler: bool, enable_mqtt: bool):
        """Delayed initialization for async stuff."""
        self.loop = asyncio.get_event_loop()
        if enable_scheduler:
            self._scheduler_unsub = self.loop.call_later(
                30,
                self._scheduler,
            )
        if enable_mqtt:
            self._mqtt_setup()

    def shutdown(self):
        """cleanup when the emulator is stopped/destroyed"""
        if self._scheduler_unsub:
            self._scheduler_unsub.cancel()
            self._scheduler_unsub = None
        if self.mqtt_client:
            self._mqtt_shutdown()

    def set_timezone(self, timezone: str, /):
        # beware when using TZ names: here we expect a IANA zoneinfo key
        # as "US/Pacific" or so. Using tzname(s) like "PDT" or "PST"
        # such as those recovered from tzinfo.tzname() might be wrong
        self.descriptor.timezone = self.descriptor.time[mc.KEY_TIMEZONE] = timezone

    @property
    def tzinfo(self):
        tz_name = self.descriptor.timezone
        if not tz_name:
            return None
        if self._tzinfo and (self._tzinfo.key == tz_name):
            return self._tzinfo
        try:
            self._tzinfo = ZoneInfo(tz_name)
        except Exception:
            self._tzinfo = None
        return self._tzinfo

    @property
    def uuid(self):
        return self.descriptor.uuid

    def update_epoch(self):
        """
        Called (by default) on every command processing.
        Could be used to (rather asynchronously) trigger internal state changes
        """
        self.descriptor.time[mc.KEY_TIMESTAMP] = self.epoch = int(time())

    def handle(self, request: MerossMessage | str, /) -> str | None:
        """
        main message handler entry point: this is called either from web.Request
        for request routed from the web.Application or from the mqtt.Client.
        It could also be used alone if we want to 'query' the emulator in any other
        scenario like for testing (where the web/mqtt environments are likely mocked)
        This method is thread-safe
        """
        cipher = None
        if isinstance(request, str):
            # this is typically the path when processing HTTP requests.
            # we're now 'enforcing' encrypted local traffic if device abilities
            # request so
            try:
                request = MerossMessage.decode(request)
            except JSONDecodeError:
                if cipher := self._cipher:
                    decryptor = cipher.decryptor()
                    request = (
                        (decryptor.update(b64decode(request)) + decryptor.finalize())
                        .decode("utf8")
                        .rstrip("\0")
                    )
                    request = MerossMessage.decode(request)
                else:
                    raise
            else:
                # when a non encrypted requested is received the device
                # actually resets the TCP connection..here we're just raising an
                # exception in the hope we can emulate a broken connection
                if self._cipher and (
                    request[mc.KEY_HEADER][mc.KEY_NAMESPACE]
                    != mn.Appliance_System_Ability.name
                ):
                    raise Exception("Encryption required")

        request_header = request[mc.KEY_HEADER]
        request_payload = request[mc.KEY_PAYLOAD]
        self._log_message("RX", request.json())
        with self.lock:
            # guarantee thread safety by locking the whole message handling
            self.update_epoch()

            if get_replykey(request_header, self.key) is not self.key:
                response = build_message(
                    request_header[mc.KEY_NAMESPACE],
                    mc.METHOD_ERROR,
                    {mc.KEY_ERROR: {mc.KEY_CODE: mc.ERROR_INVALIDKEY}},
                    request_header[mc.KEY_MESSAGEID],
                    self.key,
                    self.topic_response,
                )
            else:
                response = self._handle_message(request_header, request_payload)

        if response:
            response = json_dumps(response)
            if len(response) > self.MAXIMUM_RESPONSE_SIZE:
                # Applying 'overflow' if the response text is too big,
                # thus emulating the same behavior of hw devices.
                # These have a ranging 'maximum response size' based on my experience:
                # - msl120:  2k
                # - msh300:  4k
                # - mss310:  2.9k
                response = response[: self.MAXIMUM_RESPONSE_SIZE]
            self._log_message("TX", response)
            if cipher:
                response_bytes = response.encode("utf-8")
                response_bytes += bytes(16 - (len(response_bytes) % 16))
                encryptor = cipher.encryptor()
                response = b64encode(
                    encryptor.update(response_bytes) + encryptor.finalize()
                ).decode("utf-8")
            return response

        return None

    def _handle_message(
        self, header: "MerossHeaderType", payload: "MerossPayloadType", /
    ):
        namespace = header[mc.KEY_NAMESPACE]
        method = header[mc.KEY_METHOD]
        try:
            if namespace not in self.descriptor.ability:
                raise Exception(f"{namespace} not supported in ability")

            if namespace == mn.Appliance_Control_Multiple.name:
                if method != mc.METHOD_SET:
                    raise Exception(f"{method} not supported for {namespace}")
                multiple = []
                for message in payload[mc.KEY_MULTIPLE]:
                    multiple.append(
                        self._handle_message(
                            message[mc.KEY_HEADER], message[mc.KEY_PAYLOAD]
                        )
                    )
                response_method = mc.METHOD_SETACK
                response_payload = {mc.KEY_MULTIPLE: multiple}
            elif handler := getattr(
                self, f"_{method}_{namespace.replace('.', '_')}", None
            ):
                response_method, response_payload = handler(header, payload)
            else:
                response_method, response_payload = self._handler_default(
                    method, namespace, payload
                )

        except Exception as e:
            self._log_message(e.__class__.__name__, str(e))
            response_method = mc.METHOD_ERROR
            response_payload = {
                mc.KEY_ERROR: {
                    mc.KEY_CODE: -1,
                    "message": f"{e.__class__.__name__}({e})",
                }
            }

        if response_method:
            response = build_message(
                header[mc.KEY_NAMESPACE],
                response_method,
                response_payload,
                header[mc.KEY_MESSAGEID],
                self.key,
                self.topic_response,
            )
            return response

        return None

    def _handler_default(self, method: str, namespace: str, payload: "Mapping", /):
        """
        This is an euristhic to try parse a namespace carrying state stored in all->digest
        If the state is not stored in all->digest we'll search our namespace(s) list for
        state carried through our GETACK messages in the trace
        """
        try:
            key_namespace, p_state = self._get_key_state(namespace)
        except Exception as exception:
            # when the 'looking for state' euristic fails
            # we might fallback to a static reply should it fit...
            if (method == mc.METHOD_GET) and (namespace in self.namespaces):
                return mc.METHOD_GETACK, self.namespaces[namespace]
            raise Exception(
                f"{namespace} not supported in emulator ({exception})"
            ) from exception

        ns = self.NAMESPACES[namespace]

        match method:
            case mc.METHOD_GET:
                if ns.has_get is False:
                    raise Exception(
                        f"{method} not supported in emulator for {namespace}"
                    )
                return mc.METHOD_GETACK, {key_namespace: p_state}

            case mc.METHOD_SET:
                p_payload = payload[key_namespace]
                if isinstance(p_state, list):
                    for p_payload_channel in extract_dict_payloads(p_payload):
                        update_dict_strict_by_key(p_state, p_payload_channel)
                elif mc.KEY_CHANNEL in p_state:
                    if p_state[mc.KEY_CHANNEL] == p_payload[mc.KEY_CHANNEL]:
                        update_dict_strict(p_state, p_payload)
                    else:
                        raise Exception(
                            f"{p_payload[mc.KEY_CHANNEL]} not present in digest.{key_namespace}"
                        )
                else:
                    update_dict_strict(p_state, p_payload)

                if self.mqtt_connected and ns.has_push:
                    self.mqtt_publish_push(namespace, {key_namespace: p_state})

                return mc.METHOD_SETACK, {}

            case mc.METHOD_PUSH:
                if ns.has_push_query:
                    return mc.METHOD_PUSH, {key_namespace: p_state}

        raise Exception(f"{method} not supported in emulator for {namespace}")

    def _SET_Appliance_Config_Key(self, header, payload, /):
        """
        When connecting to a Meross cloud broker we're receiving this 'on the fly'
        so we should try to accomplish the new config
        {
            "key":{
                "key":"meross_account_key",
                "userId":"meross_account_id",
                "gateway":{
                    "host":"some-mqtt.meross.com",
                    "secondHost":"some-mqtt.meross.com",
                    "redirect":2
                }
            }
        }
        """
        p_key = payload[mc.KEY_KEY]
        p_gateway = p_key[mc.KEY_GATEWAY]
        if mc.KEY_REDIRECT in p_gateway:
            match p_gateway[mc.KEY_REDIRECT]:
                case 2:
                    # Note: after testing it looks that when connecting to the designated Meross broker (address
                    # from account api info), it issues this message trying to switch to another broker but, if
                    # we follow the switch-over, the newly designated broker seems unresponsive to session
                    # establishment. Ignoring this message instead looks like working and keeping the connection
                    # to the originally designated broker seems to work with the app able to reach and interact
                    # with our emulator like if it was the real device.
                    pass
                case _:
                    # Watchout since this might be the mqtt thread context.
                    # We're then using call_soon_threadsafe to post-pone execution
                    # in the main/loop thread
                    def _restart_callback():
                        if self.mqtt_client:
                            self._mqtt_shutdown()
                        with self.lock:  # likely unneed since the mqtt thread is over
                            firmware = self.descriptor.firmware
                            if mc.KEY_HOST in p_gateway:
                                firmware[mc.KEY_SERVER] = p_gateway[mc.KEY_HOST]
                                if mc.KEY_PORT in p_gateway:
                                    firmware[mc.KEY_PORT] = p_gateway[mc.KEY_PORT]
                            if mc.KEY_SECONDHOST in p_gateway:
                                firmware[mc.KEY_SECONDSERVER] = p_gateway[
                                    mc.KEY_SECONDHOST
                                ]
                                if mc.KEY_SECONDPORT in p_gateway:
                                    firmware[mc.KEY_SECONDPORT] = p_gateway[
                                        mc.KEY_SECONDPORT
                                    ]
                            firmware[mc.KEY_USERID] = p_key[mc.KEY_USERID]
                            self.key = p_key[mc.KEY_KEY]
                        self._mqtt_setup()

                    self.loop.call_soon_threadsafe(_restart_callback)

        return mc.METHOD_SETACK, {}

    def _SETACK_Appliance_Control_Bind(self, header, payload, /):
        self.mqtt_publish_push(
            mn.Appliance_System_Report.name,
            {
                mn.Appliance_System_Report.key: [
                    {mc.KEY_TYPE: 1, mc.KEY_VALUE: 0, mc.KEY_TIMESTAMP: self.epoch}
                ]
            },
        )
        self.mqtt_publish_push(
            mn.Appliance_System_Time.name,
            {mn.Appliance_System_Time.key: self.descriptor.time},
        )
        return None, None

    def _GET_Appliance_Control_Toggle(self, header, payload, /):
        # only actual example of this usage comes from legacy firmwares
        # carrying state in all->control
        return mc.METHOD_GETACK, {mc.KEY_TOGGLE: self._get_control_key(mc.KEY_TOGGLE)}

    def _SET_Appliance_Control_Toggle(self, header, payload, /):
        # only acual example of this usage comes from legacy firmwares
        # carrying state in all->control
        self._get_control_key(mc.KEY_TOGGLE)[mc.KEY_ONOFF] = payload[mc.KEY_TOGGLE][
            mc.KEY_ONOFF
        ]
        return mc.METHOD_SETACK, {}

    def _GET_Appliance_System_Debug(self, header, payload, /):
        firmware = self.descriptor.firmware
        return mc.METHOD_GETACK, {
            mc.KEY_DEBUG: {
                mc.KEY_SYSTEM: {
                    mc.KEY_VERSION: firmware.get(mc.KEY_VERSION),
                    "sysUpTime": "169h52m27s",
                    "localTimeOffset": 0,
                    "localTime": "Sun Mar 10 13:19:09 2024",
                    "suncalc": "6:6;18:13",
                },
                mc.KEY_NETWORK: {
                    "linkStatus": "connected",
                    mc.KEY_SIGNAL: 70,
                    "ssid": "######0",
                    mc.KEY_GATEWAYMAC: firmware.get(mc.KEY_WIFIMAC),
                    mc.KEY_INNERIP: firmware.get(mc.KEY_INNERIP),
                    "wifiDisconnectCount": 0,
                },
                mc.KEY_CLOUD: {
                    mc.KEY_ACTIVESERVER: firmware.get(mc.KEY_SERVER),
                    mc.KEY_MAINSERVER: firmware.get(mc.KEY_SERVER),
                    mc.KEY_MAINPORT: firmware.get(mc.KEY_PORT),
                    mc.KEY_SECONDSERVER: firmware.get(
                        mc.KEY_SECONDSERVER, firmware.get(mc.KEY_SERVER)
                    ),
                    mc.KEY_SECONDPORT: firmware.get(
                        mc.KEY_SECONDPORT, firmware.get(mc.KEY_PORT)
                    ),
                    mc.KEY_USERID: firmware.get(mc.KEY_USERID),
                    "sysConnectTime": "Wed Feb 28 05:39:07 2024",
                    "sysOnlineTime": "271h40m2s",
                    "sysDisconnectCount": 2,
                },
            }
        }

    def _GET_Appliance_System_Firmware(self, header, payload, /):
        return mc.METHOD_GETACK, {mc.KEY_FIRMWARE: self.descriptor.firmware}

    def _GET_Appliance_System_Hardware(self, header, payload, /):
        return mc.METHOD_GETACK, {mc.KEY_HARDWARE: self.descriptor.hardware}

    def _GET_Appliance_System_Online(self, header, payload, /):
        return mc.METHOD_GETACK, {mc.KEY_ONLINE: self.descriptor.all[mc.KEY_ONLINE]}

    def _SET_Appliance_System_Time(self, header, payload, /):
        self.descriptor.update_time(payload[mc.KEY_TIME])
        self.update_epoch()
        return mc.METHOD_SETACK, {}

    def _get_key_state(self, namespace: str, /) -> tuple[str, dict | list]:
        """
        general device state is usually carried in NS_ALL into the "digest" key
        and is also almost regularly keyed by using the camelCase of the last verb
        in namespace.
        For some devices not all state is carried there tho, so we'll inspect the
        GETACK payload for the relevant namespace looking for state there too
        """
        key = self.NAMESPACES[namespace].key

        match namespace.split("."):
            case (_, "Control", _):
                p_digest = self.descriptor.digest
            case (_, "Control", ns_2, _):
                p_digest = self.descriptor.digest
                subkey = "".join([ns_2[0].lower(), ns_2[1:]])
                if subkey in p_digest:
                    p_digest = p_digest[subkey]
            case _:
                return key, self.namespaces[namespace][key]

        if key in p_digest:
            return key, p_digest[key]

        return key, self.namespaces[namespace][key]

    def _get_control_key(self, key, /):
        """Extracts the legacy 'control' key from NS_ALL (previous to 'digest' introduction)."""
        p_control = self.descriptor.all.get(mc.KEY_CONTROL)
        if p_control is None:
            raise Exception("'control' key not present")
        if key not in p_control:
            raise Exception(f"'{key}' not present in 'control' key")
        return p_control[key]

    def _log_message(self, tag: str, message: str, /):
        print(f"Emulator({self.uuid}) {tag}: {message}")

    def _scheduler(self):
        """Called by asyncio at (almost) regular intervals to trigger
        internal state changes useful for PUSHes. To be called by
        inherited implementations at start so to update the epoch."""
        self._scheduler_unsub = asyncio.get_event_loop().call_later(
            30,
            self._scheduler,
        )
        self.update_epoch()

    def get_namespace_state(self, ns: "Namespace", channel, /) -> dict:
        return get_element_by_key(
            self.namespaces[ns.name][ns.key], ns.key_channel, channel
        )

    def update_namespace_state(
        self,
        ns: "Namespace",
        nsdefaultmode: NSDefaultMode,
        payload: dict | list,
        /,
    ):
        """updates the current state (stored in namespace key) eventually creating a default.
        Useful when sanitizing mixin state during init should the trace miss some well-known namespaces info
        """
        try:
            p_namespace = self.namespaces[ns.name]
        except KeyError:
            self.namespaces[ns.name] = p_namespace = {}

        if isinstance(ns.request_payload_type.value, dict):
            assert type(payload) is dict
            try:
                if nsdefaultmode is MerossEmulator.NSDefaultMode.MixIn:
                    p_namespace[ns.key] |= payload
                else:
                    p_namespace[ns.key] = payload | p_namespace[ns.key]
            except KeyError:
                p_namespace[ns.key] = payload
        else:  # isinstance(ns.request_payload_type.value, list):
            try:
                p_state: list = p_namespace[ns.key]
            except KeyError:
                p_namespace[ns.key] = p_state = []

            key_channel = ns.key_channel
            for p_payload_channel in extract_dict_payloads(payload):
                channel = p_payload_channel[key_channel]
                try:
                    p_channel_state = get_element_by_key(p_state, key_channel, channel)
                    if nsdefaultmode is MerossEmulator.NSDefaultMode.MixIn:
                        p_channel_state |= p_payload_channel
                    else:
                        p_channel_state |= p_payload_channel | p_channel_state
                except KeyError:
                    p_state.append(p_payload_channel)

    def mqtt_publish_push(self, namespace: str, payload: dict):
        """
        Used to async (PUSH) state changes to MQTT: the execution is actually delayed so
        that any current message parsing/reply completes before publishing this.
        """
        # capture context before delaying
        mqtt_client = self.mqtt_client
        message = MerossRequest(
            namespace,
            mc.METHOD_PUSH,
            payload,
            self.key,
            mqtt_client.topic_publish,
            mc.HEADER_TRIGGERSRC_DEVICE,
        ).json()

        def _mqtt_publish():
            self._log_message("TX(MQTT)", message)
            mqtt_client.publish(mqtt_client.topic_publish, message)

        self.loop.call_soon_threadsafe(_mqtt_publish)

    def _mqtt_setup(self):
        self.mqtt_client = mqtt_client = MerossMQTTDeviceClient(
            self.uuid, key=self.key, userid=self.descriptor.userId or ""
        )
        mqtt_client.on_subscribe = self._mqttc_subscribe
        mqtt_client.on_disconnect = self._mqttc_disconnect
        mqtt_client.on_message = self._mqttc_message
        mqtt_client.suppress_exceptions = True
        mqtt_client.safe_start(self.descriptor.main_broker)

    def _mqtt_shutdown(self):
        self.mqtt_client.safe_stop()
        with self.lock:
            self.mqtt_client = None  # type: ignore
            self.mqtt_connected = None

    def _mqttc_subscribe(self, *args):
        mqtt_client = self.mqtt_client
        mqtt_client._mqttc_subscribe(*args)
        with self.lock:
            self.mqtt_connected = mqtt_client
            self.update_epoch()
            self.descriptor.online[mc.KEY_STATUS] = mc.STATUS_ONLINE
            # This is to start a kind of session establishment with
            # Meross brokers. Check the SETACK reply to follow the state machine
            message = MerossRequest(
                mn.Appliance_Control_Bind.name,
                mc.METHOD_SET,
                {
                    mn.Appliance_Control_Bind.key: {
                        mc.KEY_BINDTIME: self.epoch,
                        mc.KEY_TIME: self.descriptor.time,
                        mc.KEY_HARDWARE: self.descriptor.hardware,
                        mc.KEY_FIRMWARE: self.descriptor.firmware,
                    }
                },
                self.key,
                mqtt_client.topic_subscribe,
                mc.HEADER_TRIGGERSRC_DEVBOOT,
            ).json()
            self._log_message("TX(MQTT)", message)
            mqtt_client.publish(mqtt_client.topic_publish, message)

    def _mqttc_disconnect(self, *args):
        self.mqtt_client._mqttc_disconnect(*args)
        with self.lock:
            self.mqtt_connected = None
            self.descriptor.online[mc.KEY_STATUS] = mc.STATUS_NOTONLINE

    def _mqttc_message(self, client: "mqtt.Client", userdata, msg: "mqtt.MQTTMessage"):
        request = MerossMessage.decode(msg.payload.decode("utf-8"))
        if response := self.handle(request):
            client.publish(request[mc.KEY_HEADER][mc.KEY_FROM], response)
