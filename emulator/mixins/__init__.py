from __future__ import annotations
import asyncio

import threading
from time import time
import typing
from zoneinfo import ZoneInfo

from custom_components.meross_lan.merossclient import (
    NAMESPACE_TO_KEY,
    MerossDeviceDescriptor,
    MerossHeaderType,
    MerossMessage,
    MerossMessageType,
    MerossPayloadType,
    build_message,
    const as mc,
    extract_dict_payloads,
    get_element_by_key,
    get_macaddress_from_uuid,
    get_replykey,
    json_dumps,
    json_loads,
    update_dict_strict,
    update_dict_strict_by_key,
)

if typing.TYPE_CHECKING:
    import paho.mqtt.client as mqtt


class MerossEmulatorDescriptor(MerossDeviceDescriptor):
    namespaces: dict

    def __init__(self, tracefile: str, uuid):
        self.namespaces = {}
        with open(tracefile, "r", encoding="utf8") as f:
            if tracefile.endswith(".json.txt"):
                # HA diagnostics trace
                self._import_json(f)
            else:
                self._import_tsv(f)

        super().__init__(self.namespaces[mc.NS_APPLIANCE_SYSTEM_ABILITY])
        self.update(self.namespaces[mc.NS_APPLIANCE_SYSTEM_ALL])
        # patch system payload with fake ids
        hardware = self.hardware
        hardware[mc.KEY_UUID] = uuid
        hardware[mc.KEY_MACADDRESS] = get_macaddress_from_uuid(uuid)

    def _import_tsv(self, f):
        """
        parse a legacy tab separated values meross_lan trace
        """
        for line in f:
            row = line.split("\t")
            self._import_tracerow(row)

    def _import_json(self, f):
        """
        parse a 'diagnostics' HA trace
        """
        try:
            _json = json_loads(f.read())
            data = _json["data"]
            columns = None
            for row in data["trace"]:
                if columns is None:
                    columns = row
                    # we could parse and setup a 'column search'
                    # algorithm here should the trace layout change
                    # right now it's the same as for csv files...
                else:
                    self._import_tracerow(row)

        except Exception:
            pass

        return

    def _import_tracerow(self, values: list):
        # rxtx = values[1]
        protocol = values[-4]
        method = values[-3]
        namespace = values[-2]
        data = values[-1]
        if method == mc.METHOD_GETACK:
            if not isinstance(data, dict):
                data = json_loads(data)
            if protocol == "auto":
                data = {NAMESPACE_TO_KEY[namespace]: data}
            self.namespaces[namespace] = data
        elif (
            method == mc.METHOD_SETACK and namespace == mc.NS_APPLIANCE_CONTROL_MULTIPLE
        ):
            if not isinstance(data, dict):
                data = json_loads(data)
            for message in data[mc.KEY_MULTIPLE]:
                header = message[mc.KEY_HEADER]
                if header[mc.KEY_METHOD] == mc.METHOD_GETACK:
                    self.namespaces[header[mc.KEY_NAMESPACE]] = message[mc.KEY_PAYLOAD]


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

    __slots__ = (
        "epoch",
        "lock",
        "key",
        "descriptor",
        "p_dndmode",
        "topic_response",
        "mqtt",
        "_scheduler_unsub",
        "_tzinfo",
        "__dict__",
    )

    def __init__(self, descriptor: MerossEmulatorDescriptor, key: str):
        self.lock = threading.Lock()
        self.key = key
        self.descriptor = descriptor
        if mc.NS_APPLIANCE_SYSTEM_DNDMODE in descriptor.ability:
            self.p_dndmode = {mc.KEY_DNDMODE: {mc.KEY_MODE: 0}}
        self.topic_response = mc.TOPIC_RESPONSE.format(descriptor.uuid)
        self.mqtt = None
        self._scheduler_unsub = None
        self._tzinfo: ZoneInfo | None = None
        self.update_epoch()

    def shutdown(self):
        """cleanup when the emulator is stopped/destroyed"""
        if self._scheduler_unsub:
            self._scheduler_unsub.cancel()
            self._scheduler_unsub = None

    def set_timezone(self, timezone: str):
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

    def handle(self, request: MerossMessage | str) -> MerossMessageType | None:
        """
        main message handler entry point: this is called either from web.Request
        for request routed from the web.Application or from the mqtt.Client.
        It could also be used alone if we want to 'query' the emulator in any other
        scenario like for testing (where the web/mqtt environments are likely mocked)
        This method is thread-safe
        """
        if isinstance(request, str):
            request = MerossMessage.decode(request)
        request_header = request[mc.KEY_HEADER]
        request_payload = request[mc.KEY_PAYLOAD]
        print(
            f"Emulator({self.uuid}) "
            f"RX: namespace={request_header[mc.KEY_NAMESPACE]} method={request_header[mc.KEY_METHOD]} payload={json_dumps(request_payload)}"
        )
        with self.lock:
            # guarantee thread safety by locking the whole message handling
            self.update_epoch()
            response = self._handle_message(request_header, request_payload)

        if response:
            response_header = response[mc.KEY_HEADER]
            print(
                f"Emulator({self.uuid}) "
                f"TX: namespace={response_header[mc.KEY_NAMESPACE]} method={response_header[mc.KEY_METHOD]} payload={json_dumps(response[mc.KEY_PAYLOAD])}"
            )
        return response

    def handle_connect(self, client: mqtt.Client):
        with self.lock:
            self.mqtt = client
            self.update_epoch()
            # kind of Bind message..we're just interested in validating
            # the server code in meross_lan (it doesn't really check this
            # payload)
            message_bind_set = build_message(
                mc.NS_APPLIANCE_CONTROL_BIND,
                mc.METHOD_SET,
                {
                    "bind": {
                        "bindTime": self.epoch,
                        mc.KEY_HARDWARE: self.descriptor.hardware,
                        mc.KEY_FIRMWARE: self.descriptor.firmware,
                    }
                },
                self.key,
                self.topic_response,
            )
            client.publish(self.topic_response, json_dumps(message_bind_set))

    def handle_disconnect(self, client: mqtt.Client):
        with self.lock:
            self.mqtt = None

    def _handle_message(self, header: MerossHeaderType, payload: MerossPayloadType):
        namespace = header[mc.KEY_NAMESPACE]
        method = header[mc.KEY_METHOD]
        try:
            if namespace not in self.descriptor.ability:
                raise Exception(f"{namespace} not supported in ability")

            elif get_replykey(header, self.key) is not self.key:
                response_method = mc.METHOD_ERROR
                response_payload = {mc.KEY_ERROR: {mc.KEY_CODE: mc.ERROR_INVALIDKEY}}

            elif handler := getattr(
                self, f"_{method}_{namespace.replace('.', '_')}", None
            ):
                response_method, response_payload = handler(header, payload)

            else:
                response_method, response_payload = self._handler_default(
                    method, namespace, payload
                )

        except Exception as e:
            response_method = mc.METHOD_ERROR
            response_payload = {mc.KEY_ERROR: {mc.KEY_CODE: -1, "message": str(e)}}

        if response_method:
            return build_message(
                namespace,
                response_method,
                response_payload,
                self.key,
                self.topic_response,
                header[mc.KEY_MESSAGEID],
            )

    def _handler_default(self, method: str, namespace: str, payload: dict):
        """
        This is an euristhic to try parse a namespace carrying state stored in all->digest
        If the state is not stored in all->digest we'll search our namespace(s) list for
        state carried through our GETACK messages in the trace
        """
        try:
            key, p_state = self._get_key_state(namespace)
        except Exception as exception:
            # when the 'looking for state' euristic fails
            # we might fallback to a static reply should it fit...
            if (method == mc.METHOD_GET) and (namespace in self.descriptor.namespaces):
                return mc.METHOD_GETACK, self.descriptor.namespaces[namespace]
            raise Exception(
                f"{namespace} not supported in emulator ({exception})"
            ) from exception

        if method == mc.METHOD_GET:
            return mc.METHOD_GETACK, {key: p_state}

        if method == mc.METHOD_SET:
            p_payload = payload[key]
            if isinstance(p_state, list):
                for p_payload_channel in extract_dict_payloads(p_payload):
                    update_dict_strict_by_key(p_state, p_payload_channel)
            elif mc.KEY_CHANNEL in p_state:
                if p_state[mc.KEY_CHANNEL] == p_payload[mc.KEY_CHANNEL]:
                    update_dict_strict(p_state, p_payload)
                else:
                    raise Exception(
                        f"{p_payload[mc.KEY_CHANNEL]} not present in digest.{key}"
                    )
            else:
                update_dict_strict(p_state, p_payload)

            return mc.METHOD_SETACK, {}

        if method == mc.METHOD_PUSH:
            if namespace in mc.PUSH_ONLY_NAMESPACES:
                return mc.METHOD_PUSH, self.descriptor.namespaces[namespace]

        raise Exception(f"{method} not supported in emulator for {namespace}")

    def _SETACK_Appliance_Control_Bind(self, header, payload):
        return None, None

    def _SET_Appliance_Control_Multiple(self, header, payload):
        multiple = []
        for message in payload[mc.KEY_MULTIPLE]:
            if response := self._handle_message(
                message[mc.KEY_HEADER], message[mc.KEY_PAYLOAD]
            ):
                multiple.append(response)
        return mc.METHOD_SETACK, {mc.KEY_MULTIPLE: multiple}

    def _GET_Appliance_Control_Toggle(self, header, payload):
        # only acual example of this usage comes from legacy firmwares
        # carrying state in all->control
        return mc.METHOD_GETACK, {mc.KEY_TOGGLE: self._get_control_key(mc.KEY_TOGGLE)}

    def _SET_Appliance_Control_Toggle(self, header, payload):
        # only acual example of this usage comes from legacy firmwares
        # carrying state in all->control
        self._get_control_key(mc.KEY_TOGGLE)[mc.KEY_ONOFF] = payload[mc.KEY_TOGGLE][
            mc.KEY_ONOFF
        ]
        return mc.METHOD_SETACK, {}

    def _GET_Appliance_System_DNDMode(self, header, payload):
        return mc.METHOD_GETACK, self.p_dndmode

    def _SET_Appliance_System_DNDMode(self, header, payload):
        update_dict_strict(self.p_dndmode, payload)
        return mc.METHOD_SETACK, {}

    def _SET_Appliance_System_Time(self, header, payload):
        self.descriptor.update_time(payload[mc.KEY_TIME])
        self.update_epoch()
        return mc.METHOD_SETACK, {}

    def _get_key_state(self, namespace: str) -> tuple[str, dict | list]:
        """
        general device state is usually carried in NS_ALL into the "digest" key
        and is also almost regularly keyed by using the camelCase of the last verb
        in namespace.
        For some devices not all state is carried there tho, so we'll inspect the
        GETACK payload for the relevant namespace looking for state there too
        """
        key = NAMESPACE_TO_KEY[namespace]

        match namespace.split("."):
            case (_, "RollerShutter", _):
                return key, self.descriptor.namespaces[namespace][key]
            case (_, "Config", _):
                return key, self.descriptor.namespaces[namespace][key]
            case (_, "Control", _):
                p_digest = self.descriptor.digest
            case (_, "Control", ns_2, _):
                p_digest = self.descriptor.digest
                subkey = "".join([ns_2[0].lower(), ns_2[1:]])
                if subkey in p_digest:
                    p_digest = p_digest[subkey]
            case _:
                raise Exception(f"{namespace} not supported in emulator")

        if key in p_digest:
            return key, p_digest[key]

        return key, self.descriptor.namespaces[namespace][key]

    def _get_control_key(self, key):
        """Extracts the legacy 'control' key from NS_ALL (previous to 'digest' introduction)."""
        p_control = self.descriptor.all.get(mc.KEY_CONTROL)
        if p_control is None:
            raise Exception("'control' key not present")
        if key not in p_control:
            raise Exception(f"'{key}' not present in 'control' key")
        return p_control[key]

    def _scheduler(self):
        """Called by asyncio at (almost) regular intervals to trigger
        internal state changes useful for PUSHes. To be called by
        inherited implementations at start so to update the epoch."""
        self._scheduler_unsub = asyncio.get_event_loop().call_later(
            30,
            self._scheduler,
        )
        self.update_epoch()

    def get_namespace_state(
        self, namespace: str, channel, key_channel: str = mc.KEY_CHANNEL
    ) -> dict:
        p_namespace_state = self.descriptor.namespaces[namespace][
            NAMESPACE_TO_KEY[namespace]
        ]
        return get_element_by_key(p_namespace_state, key_channel, channel)

    def update_namespace_state(
        self, namespace: str, channel, payload: dict, key_channel: str = mc.KEY_CHANNEL
    ):
        """updates the current state (stored in namespace key) eventually creating a default.
        Useful when sanitizing mixin state during init should the trace miss some well-known namespaces info
        """
        try:
            p_namespace_state: list = self.descriptor.namespaces[namespace][
                NAMESPACE_TO_KEY[namespace]
            ]
            try:
                p_channel_state = get_element_by_key(
                    p_namespace_state, key_channel, channel
                )
            except KeyError:
                p_channel_state = {key_channel: channel}
                p_namespace_state.append(p_channel_state)
        except KeyError:
            p_channel_state = {key_channel: channel}
            p_namespace_state = [p_channel_state]
            self.descriptor.namespaces[namespace] = {
                NAMESPACE_TO_KEY[namespace]: p_namespace_state
            }

        p_channel_state.update(payload)
