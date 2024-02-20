"""
    Emulator module: implementation for an emulator able to
    simulate the real protocol stack working on a device. This can be used to
    setup an http server representing a connection to a physical device for
    testing purposes (or for fun).
    The emulator is implemented as a 'generic' protocol parser which uses
    the grammar from a trace/diagnostic to setup the proper response
    Somewhere, here and there, some hardcoded behavior is implemented to
    reach an higher state of functionality since at the core, the emulator
    is just a reply service of what's inside a trace.
    Typically, an emulator is built by using 'build_emulator' since it is
    a mixin based class.
    'generate_emulators' is an helper (python generator) to build a whole
    set of emulators from all the traces stored in a path.
"""

from __future__ import annotations

import os

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
    MerossMessage,
    const as mc,
    json_dumps,
)

from .mixins import MerossEmulator, MerossEmulatorDescriptor


def build_emulator(tracefile, uuid, key) -> MerossEmulator:
    """
    Given a supported 'tracefile' (either a legacy trace .csv or a diagnostic .json)
    parse it and build the appropriate emulator instance with the give 'uuid' and 'key'
    this will also set the correct inferred mac address in the descriptor based on the uuid
    as this appears to be consistent with real devices config
    """
    print(f"Initializing uuid({uuid}):", end="")
    descriptor = MerossEmulatorDescriptor(tracefile, uuid)
    ability = descriptor.ability
    digest = descriptor.digest
    mixin_classes = []

    if mc.KEY_HUB in digest:
        from .mixins.hub import HubMixin

        mixin_classes.append(HubMixin)
    if mc.KEY_THERMOSTAT in digest:
        from .mixins.thermostat import ThermostatMixin

        mixin_classes.append(ThermostatMixin)
    if mc.KEY_GARAGEDOOR in digest:
        from .mixins.garagedoor import GarageDoorMixin

        mixin_classes.append(GarageDoorMixin)
    if mc.NS_APPLIANCE_CONTROL_ELECTRICITY in ability:
        from .mixins.electricity import ElectricityMixin

        mixin_classes.append(ElectricityMixin)
    if mc.NS_APPLIANCE_CONTROL_CONSUMPTIONX in ability:
        from .mixins.electricity import ConsumptionXMixin

        mixin_classes.append(ConsumptionXMixin)

    if mc.NS_APPLIANCE_CONTROL_LIGHT in ability:
        from .mixins.light import LightMixin

        mixin_classes.append(LightMixin)

    if mc.NS_APPLIANCE_CONTROL_FAN in ability:
        from .mixins.fan import FanMixin

        mixin_classes.append(FanMixin)

    if mc.NS_APPLIANCE_ROLLERSHUTTER_STATE in ability:
        from .mixins.rollershutter import RollerShutterMixin

        mixin_classes.append(RollerShutterMixin)

    if mc.NS_APPLIANCE_CONTROL_PHYSICALLOCK in ability:
        from .mixins.physicallock import PhysicalLockMixin

        mixin_classes.append(PhysicalLockMixin)

    mixin_classes.append(MerossEmulator)
    # build a label to cache the set
    class_name = ""
    for m in mixin_classes:
        class_name = class_name + m.__name__
    class_type = type(class_name, tuple(mixin_classes), {})

    emulator = class_type(descriptor, key)
    print(f" {descriptor.type} (model:{descriptor.productmodel})")
    return emulator


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
    broker = None
    tracefilepath = "."
    for arg in argv:
        arg: str
        if arg.startswith("-key"):
            key = arg[4:].strip()
        elif arg.startswith("-uuid"):
            uuid = arg[5:].strip()
        elif arg.startswith("-broker"):
            broker = arg[7:].strip()
        else:
            tracefilepath = arg

    app = web.Application()

    def web_post_handler(emulator: MerossEmulator):
        async def _callback(request: web.Request) -> web.Response:
            if not emulator._scheduler_unsub:
                # starts internal scheduler once when we're in asyncio environment
                emulator._scheduler()
            return web.json_response(emulator.handle(await request.text()))

        return _callback

    if os.path.isdir(tracefilepath):
        emulators = {
            emulator.descriptor.uuid: emulator
            for emulator in generate_emulators(tracefilepath, uuid, key)
        }
        for _uuid, emulator in emulators.items():
            app.router.add_post(f"/{_uuid}/config", web_post_handler(emulator))
    else:
        emulator = build_emulator(tracefilepath, uuid, key)
        emulators = {emulator.descriptor.uuid: emulator}
        app.router.add_post("/config", web_post_handler(emulator))

    if broker:
        import ssl

        import paho.mqtt.client as mqtt

        def _mqttc_connect(client: mqtt.Client, userdata, flags, rc):
            result, mid = client.subscribe([(mc.TOPIC_REQUEST.format("+"), 1)])
            if result != mqtt.MQTT_ERR_SUCCESS:
                print("Failed to subscribe to mqtt topic")
            for emulator in emulators.values():
                emulator.handle_connect(client)

        def _mqttc_disconnect(client: mqtt.Client, userdata, rc):
            for emulator in emulators.values():
                emulator.handle_disconnect(client)

        def _mqttc_message(client: mqtt.Client, userdata, msg: mqtt.MQTTMessage):
            if msg_uuid := mc.RE_PATTERN_TOPIC_UUID.match(msg.topic):
                if emulator := emulators.get(msg_uuid.group(1)):
                    request = MerossMessage.decode(msg.payload.decode("utf-8"))
                    if response := emulator.handle(request):
                        client.publish(
                            request[mc.KEY_HEADER][mc.KEY_FROM], json_dumps(response)
                        )

        mqtt_client = mqtt.Client("MerossEmulator", protocol=mqtt.MQTTv311)
        mqtt_client.username_pw_set("emulator")
        mqtt_client.tls_set(cert_reqs=ssl.CERT_NONE, tls_version=ssl.PROTOCOL_TLSv1_2)
        mqtt_client.tls_insecure_set(True)
        mqtt_client.on_connect = _mqttc_connect
        mqtt_client.on_disconnect = _mqttc_disconnect
        mqtt_client.on_message = _mqttc_message
        mqtt_client.suppress_exceptions = True
        if ":" in broker:
            broker = broker.split(":")
            port = int(broker[1])
            broker = broker[0]
        else:
            port = 8883
        mqtt_client.connect_async(broker, port)
        mqtt_client.loop_start()

    return app
