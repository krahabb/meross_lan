"""
    Emulator:

    Based off the knowledge inside the MerossEmulatorDescriptor
    this class tries to reply to an incoming request by looking
    at the vocabulary of known namespaces listed in the descriptor.
    It is also able to manage a sort of state for commands accessing
    data in the Apllication.System.All namespace at the 'digest' key
    which are the majority.
    If state is not available there it could be looked up in the specific
    command carrying the message and so automatically managed too

"""
from __future__ import annotations

from json import dumps as json_dumps, loads as json_loads
from time import time
from zoneinfo import ZoneInfo

from custom_components.meross_lan.merossclient import (
    build_payload,
    const as mc,
    get_namespacekey,
    get_replykey,
)

from .descriptor import MerossEmulatorDescriptor


class MerossEmulator:

    _tzinfo: ZoneInfo | None = None

    def __init__(self, descriptor: MerossEmulatorDescriptor, key):
        self.key = key
        self.descriptor = descriptor
        self.p_all_system_time = descriptor.system.get(mc.KEY_TIME)
        if mc.NS_APPLIANCE_SYSTEM_DNDMODE in descriptor.ability:
            self.p_dndmode = {mc.KEY_DNDMODE: {mc.KEY_MODE: 0}}
        self.update_epoch()
        print(f"Initialized {descriptor.productname} (model:{descriptor.productmodel})")

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
        if (self._tzinfo is not None) and (self._tzinfo.key == tz_name):
            return self._tzinfo
        try:
            self._tzinfo = ZoneInfo(tz_name)
        except Exception:
            self._tzinfo = None
        return self._tzinfo

    # async def post_config(self, request: web_Request):
    def handle(self, request: str) -> dict:
        jsonrequest = json_loads(request)
        header: dict = jsonrequest[mc.KEY_HEADER]
        payload: dict = jsonrequest[mc.KEY_PAYLOAD]
        namespace: str = header[mc.KEY_NAMESPACE]
        method: str = header[mc.KEY_METHOD]

        print(
            f"Emulator({self.descriptor.uuid}) "
            f"RX: namespace={namespace} method={method} payload={json_dumps(payload)}"
        )
        try:
            self.update_epoch()

            if namespace not in self.descriptor.ability:
                raise Exception(f"{namespace} not supported in ability")

            elif get_replykey(header, self.key) is not self.key:
                method = mc.METHOD_ERROR
                payload = {mc.KEY_ERROR: {mc.KEY_CODE: mc.ERROR_INVALIDKEY}}

            elif (
                handler := getattr(
                    self, f"_{method}_{namespace.replace('.', '_')}", None
                )
            ) is not None:
                method, payload = handler(header, payload)

            else:
                method, payload = self._handler_default(method, namespace, payload)

        except Exception as e:
            method = mc.METHOD_ERROR
            payload = {mc.KEY_ERROR: {mc.KEY_CODE: -1, "message": str(e)}}

        data = build_payload(
            namespace,
            method,
            payload,
            self.key,
            mc.MANUFACTURER,
            header[mc.KEY_MESSAGEID],
        )
        print(
            f"Emulator({self.descriptor.uuid}) TX: namespace={namespace} method={method} payload={json_dumps(payload)}"
        )
        return data

    def update_epoch(self):
        self.epoch = int(time())
        if self.p_all_system_time is not None:
            self.p_all_system_time[mc.KEY_TIMESTAMP] = self.epoch

    def _get_key_state(self, namespace: str) -> tuple[str, dict]:
        """
        general device state is usually carried in NS_ALL into the "digest" key
        and is also almost regularly keyed by using the camelCase of the last verb
        in namespace.
        For some devices not all state is carried there tho, so we'll inspect the
        GETACK payload for the relevant namespace looking for state there too
        """
        n = namespace.split(".")
        if n[1] != "Control":
            raise Exception(f"{namespace} not supported in emulator")

        key = get_namespacekey(namespace)
        p_digest = self.descriptor.digest
        if len(n) == 4:
            # 4 parts namespaces usually access a subkey in digest
            subkey = n[2].lower()
            if subkey in p_digest:
                p_digest = p_digest[subkey]

        if key not in p_digest:
            if namespace in self.descriptor.namespaces:
                p_digest = self.descriptor.namespaces[namespace]
                if key not in p_digest:
                    raise Exception(f"{key} not present in digest and {namespace}")
            else:
                raise Exception(f"{key} not present in digest")

        return key, p_digest[key]

    def _handler_default(self, method: str, namespace: str, payload: dict):
        """
        This is an euristhic to try parse a namespace carrying state stored in all->digest
        If the state is not stored in all->digest we'll search our namespace(s) list for
        state carried through our GETACK messages in the trace
        """
        try:
            key, p_state = self._get_key_state(namespace)
        except Exception as error:
            # when the 'looking for state' euristic fails
            # we might fallback to a static reply should it fit...
            if (method == mc.METHOD_GET) and (namespace in self.descriptor.namespaces):
                return mc.METHOD_GETACK, self.descriptor.namespaces[namespace]
            raise error

        if method == mc.METHOD_GET:
            return mc.METHOD_GETACK, {key: p_state}

        if method != mc.METHOD_SET:
            # TODO.....
            raise Exception(f"{method} not supported in emulator")

        def _update(payload: dict):
            channel = payload[mc.KEY_CHANNEL]
            for p in p_state:
                if p[mc.KEY_CHANNEL] == channel:
                    p.update(payload)
                    break
            else:
                raise Exception(f"{channel} not present in digest.{key}")

        p_payload = payload[key]
        if isinstance(p_state, list):
            if isinstance(p_payload, list):
                for p_p in p_payload:
                    _update(p_p)
            else:
                _update(p_payload)
        else:
            if p_state[mc.KEY_CHANNEL] == p_payload[mc.KEY_CHANNEL]:
                p_state.update(p_payload)
            else:
                raise Exception(
                    f"{p_payload[mc.KEY_CHANNEL]} not present in digest.{key}"
                )

        return mc.METHOD_SETACK, {}

    def _GET_Appliance_System_DNDMode(self, header, payload):
        return mc.METHOD_GETACK, self.p_dndmode

    def _SET_Appliance_System_DNDMode(self, header, payload):
        self.p_dndmode = payload
        return mc.METHOD_SETACK, {}

    def _get_control_key(self, key):
        p_control = self.descriptor.all.get(mc.KEY_CONTROL)
        if p_control is None:
            raise Exception(f"{mc.KEY_CONTROL} not present")
        if key not in p_control:
            raise Exception(f"{key} not present in control")
        return p_control[key]

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

    def _SET_Appliance_Control_Light(self, header, payload):
        # need to override basic handler since lights turning on/off is tricky between
        # various firmwares: some supports onoff in light payload some use the togglex
        p_light = payload[mc.KEY_LIGHT]
        p_digest = self.descriptor.digest
        support_onoff_in_light = mc.KEY_ONOFF in p_digest[mc.KEY_LIGHT]
        # generally speaking set_light always turns on, unless the payload carries onoff = 0 and
        # the device is not using togglex
        if support_onoff_in_light:
            onoff = p_light.get(mc.KEY_ONOFF, 1)
            p_light[mc.KEY_ONOFF] = onoff
        else:
            onoff = 1
            p_light.pop(mc.KEY_ONOFF, None)
        if mc.KEY_TOGGLEX in p_digest:
            # fixed channel 0..that is..
            p_digest[mc.KEY_TOGGLEX][0][mc.KEY_ONOFF] = onoff
        p_digest[mc.KEY_LIGHT].update(p_light)
        return mc.METHOD_SETACK, {}

    def _SET_Appliance_Control_Mp3(self, header, payload):
        if mc.NS_APPLIANCE_CONTROL_MP3 not in self.descriptor.namespaces:
            raise Exception(
                f"{mc.NS_APPLIANCE_CONTROL_MP3} not supported in namespaces"
            )
        mp3 = self.descriptor.namespaces[mc.NS_APPLIANCE_CONTROL_MP3]
        mp3[mc.KEY_MP3].update(payload[mc.KEY_MP3])
        return mc.METHOD_SETACK, {}
