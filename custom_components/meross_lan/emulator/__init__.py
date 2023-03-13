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

import os

from aiohttp import web

from custom_components.meross_lan.merossclient import const as mc

from .descriptor import MerossEmulatorDescriptor
from .emulator import MerossEmulator


def build_emulator(tracefile, uuid, key) -> MerossEmulator:
    """
    Given a supported 'tracefile' (either a legacy trace .csv or a diagnostic .json)
    parse it and build the appropriate emulator instance with the give 'uuid' and 'key'
    """
    descriptor = MerossEmulatorDescriptor(tracefile, uuid)

    mixin_classes = []

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
        from .mixins.electricity import ConsumptionMixin

        mixin_classes.append(ConsumptionMixin)

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
