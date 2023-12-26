"""
    Emulator module: implementation for an emulator class able to
    simulate the real protocol stack working on a device. This can be used to
    setup an http server representing a connection to a physical device for
    testing purposes (or for fun).
    The emulator is implemented as a 'generic' protocol parser which uses
    the grammar from a trace/diagnostic to setup the proper response
    Somewhere, here and there, some hardcoded behavior is implemented to
    reach an higher state of functionality since at the core, the emulator
    is just a 'reply' service of what's inside a trace
"""
from __future__ import annotations

import json
import os
import re
from time import time
from zoneinfo import ZoneInfo

from aiohttp import web

# This import is tricky since importlib will initialize
# meross_lan too when importing. This has the following
# implications:
# meross_lan is not really needed to be run in order to
# run the emulator so that's an unneded overhead just to
# access the symbols defined in merossclient. The right
# solution would be to 'move' merossclient to an independent
# package since merossclient itself is not dependant
# on meross_lan (it is a basic meross api interface)
# but that would imply packaging/publishing the code
# in order to have it as a dependency accessible by
# meross_lan. The solutions so far could be:
# 1) use an import trick to bypass the importlib
# design. This would have a lot of implications
# when we use the emulator in our tests which are using
# meross_lan (and all of its imports)
# 2) actually, importing the whole meross_lan, beside the
# overhead, has always worked when instantiating the
# emulator alone (standalone app from the cli)
# but now (aiohttp 3.8.1) the import system fails
# when importing the meross_lan module due to circular
# imports in homeassistant modules (namely the homeassistant.helpers)
# This is maybe due to changes in relative import order in
# HomeAssistant but they're not appearing when running HA
# since they're likely living in a 'sweet spot' of the
# init sequence.
# As for now, we need to be sure the homeassistant.core module
# is initialized before the homeassistant.helpers.storage
# so I've changed a bit the import sequence in meross_lan
# to have the homeassistant.core imported (initialized) before
# homeassistant.helpers.storage
from custom_components.meross_lan.merossclient import (
    MerossDeviceDescriptor,
    MerossMessageType,
    build_message,
    const as mc,
    get_namespacekey,
    get_replykey,
)


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
        hardware[mc.KEY_MACADDRESS] = ":".join(re.findall("..", uuid[-12:]))

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
            _json = json.loads(f.read())
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
            if protocol == "auto":
                self.namespaces[namespace] = {
                    get_namespacekey(namespace): data
                    if isinstance(data, dict)
                    else json.loads(data)
                }
            else:
                self.namespaces[namespace] = (
                    data if isinstance(data, dict) else json.loads(data)
                )


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

    _tzinfo: ZoneInfo | None = None

    def __init__(self, descriptor: MerossEmulatorDescriptor, key):
        self.key = key
        self.descriptor = descriptor
        self.p_all_system_time = descriptor.system.get(mc.KEY_TIME)
        if mc.NS_APPLIANCE_SYSTEM_DNDMODE in descriptor.ability:
            self.p_dndmode = {mc.KEY_DNDMODE: {mc.KEY_MODE: 0}}
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
        if self._tzinfo and (self._tzinfo.key == tz_name):
            return self._tzinfo
        try:
            self._tzinfo = ZoneInfo(tz_name)
        except Exception:
            self._tzinfo = None
        return self._tzinfo

    # async def post_config(self, request: web_Request):
    def handle(self, request: str) -> MerossMessageType:
        jsonrequest: MerossMessageType = json.loads(request)
        header = jsonrequest[mc.KEY_HEADER]
        payload = jsonrequest[mc.KEY_PAYLOAD]
        namespace = header[mc.KEY_NAMESPACE]
        method = header[mc.KEY_METHOD]

        print(
            f"Emulator({self.descriptor.uuid}) "
            f"RX: namespace={namespace} method={method} payload={json.dumps(payload)}"
        )
        try:
            self.update_epoch()

            if namespace not in self.descriptor.ability:
                raise Exception(f"{namespace} not supported in ability")

            elif get_replykey(header, self.key) is not self.key:
                method = mc.METHOD_ERROR
                payload = {mc.KEY_ERROR: {mc.KEY_CODE: mc.ERROR_INVALIDKEY}}

            elif handler := getattr(
                self, f"_{method}_{namespace.replace('.', '_')}", None
            ):
                method, payload = handler(header, payload)

            else:
                method, payload = self._handler_default(method, namespace, payload)

        except Exception as e:
            method = mc.METHOD_ERROR
            payload = {mc.KEY_ERROR: {mc.KEY_CODE: -1, "message": str(e)}}

        data = build_message(
            namespace,
            method,
            payload,
            self.key,
            mc.MANUFACTURER,
            header[mc.KEY_MESSAGEID],
        )
        print(
            f"Emulator({self.descriptor.uuid}) TX: namespace={namespace} method={method} payload={json.dumps(payload)}"
        )
        return data

    def update_epoch(self):
        """
        Called (by default) on every command processing.
        Could be used to (rather asynchronously) trigger internal state changes
        """
        self.epoch = int(time())
        if self.p_all_system_time:
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

    def _SET_Appliance_Control_Mp3(self, header, payload):
        if mc.NS_APPLIANCE_CONTROL_MP3 not in self.descriptor.namespaces:
            raise Exception(
                f"{mc.NS_APPLIANCE_CONTROL_MP3} not supported in namespaces"
            )
        mp3 = self.descriptor.namespaces[mc.NS_APPLIANCE_CONTROL_MP3]
        mp3[mc.KEY_MP3].update(payload[mc.KEY_MP3])
        return mc.METHOD_SETACK, {}


def build_emulator(tracefile, uuid, key) -> MerossEmulator:
    """
    Given a supported 'tracefile' (either a legacy trace .csv or a diagnostic .json)
    parse it and build the appropriate emulator instance with the give 'uuid' and 'key'
    this will also set the correct inferred mac address in the descriptor based on the uuid
    as this appears to be consistent with real devices config
    """
    descriptor = MerossEmulatorDescriptor(tracefile, uuid)

    mixin_classes = []

    if mc.KEY_HUB in descriptor.digest:
        from .mixins.hub import HubMixin

        mixin_classes.append(HubMixin)
    if mc.KEY_THERMOSTAT in descriptor.digest:
        from .mixins.thermostat import ThermostatMixin

        mixin_classes.append(ThermostatMixin)
    if mc.KEY_GARAGEDOOR in descriptor.digest:
        from .mixins.garagedoor import GarageDoorMixin

        mixin_classes.append(GarageDoorMixin)
    if mc.NS_APPLIANCE_CONTROL_ELECTRICITY in descriptor.ability:
        from .mixins.electricity import ElectricityMixin

        mixin_classes.append(ElectricityMixin)
    if mc.NS_APPLIANCE_CONTROL_CONSUMPTIONX in descriptor.ability:
        from .mixins.electricity import ConsumptionXMixin

        mixin_classes.append(ConsumptionXMixin)

    if mc.NS_APPLIANCE_CONTROL_LIGHT in descriptor.ability:
        from .mixins.light import LightMixin

        mixin_classes.append(LightMixin)

    mixin_classes.append(MerossEmulator)
    # build a label to cache the set
    class_name = ""
    for m in mixin_classes:
        class_name = class_name + m.__name__
    class_type = type(class_name, tuple(mixin_classes), {})

    return class_type(descriptor, key)


def generate_emulators(tracespath: str, defaultuuid: str, defaultkey: str):
    """
    This function is a generator.
    Scans the directory for supported files and build all the emulators
    the filename, if correctly formatted, should contain the device uuid
    and key to use for the emulator. If not, we'll use the 'defaultuuid' and/or
    'defaultkey' when instantiating the emulator. This allows for supporting
    basic plain filenames which don't contain any info but also, will make
    it difficult to understand which device is which
    """
    uuidsub = 0
    for f in os.listdir(tracespath):
        fullpath = os.path.join(tracespath, f)
        # expect only valid csv or json files
        f = f.split(".")
        if f[-1] not in ("csv", "txt", "json"):
            continue

        # filename could be formatted to carry device definitions parameters:
        # format the filename like 'xxxwhatever-Kdevice_key-Udevice_id'
        # this way, parameters will be 'binded' to that trace in an easy way
        key = defaultkey
        uuid = None
        for _f in f[0].split("-"):
            if _f.startswith("K"):
                key = _f[1:].strip()
            elif _f.startswith("U"):
                uuid = _f[1:].strip()
        if uuid is None:
            uuidsub = uuidsub + 1
            _uuidsub = str(uuidsub)
            uuid = defaultuuid[: -len(_uuidsub)] + _uuidsub
        yield build_emulator(fullpath, uuid, key)


def run(argv):
    """
    self running python app entry point
    command line invocation:
    'python -m aiohttp.web -H localhost -P 80 meross_lan.emulator:run tracefilepath'
    """
    key = ""
    uuid = "01234567890123456789001122334455"
    tracefilepath = "."
    for arg in argv:
        arg: str
        if arg.startswith("-K"):
            key = arg[2:].strip()
        elif arg.startswith("-U"):
            uuid = arg[2:].strip()
        else:
            tracefilepath = arg

    app = web.Application()

    def make_post_handler(emulator: MerossEmulator):
        async def _callback(request: web.Request) -> web.Response:
            return web.json_response(emulator.handle(await request.text()))

        return _callback

    if os.path.isdir(tracefilepath):
        for emulator in generate_emulators(tracefilepath, uuid, key):
            app.router.add_post(
                f"/{emulator.descriptor.uuid}/config", make_post_handler(emulator)
            )
    else:
        emulator = build_emulator(tracefilepath, uuid, key)
        app.router.add_post("/config", make_post_handler(emulator))

    return app
