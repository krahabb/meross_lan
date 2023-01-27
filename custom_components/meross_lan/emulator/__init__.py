import os

from aiohttp import web

from ..merossclient import const as mc

from .descriptor import MerossEmulatorDescriptor
from .emulator import MerossEmulator


def build_emulator(tracefile, uuid, key) -> MerossEmulator:

    descriptor = MerossEmulatorDescriptor(tracefile, uuid)

    mixin_classes = []

    if mc.KEY_THERMOSTAT in descriptor.digest:
        from .mixins.thermostat import ThermostatMixin # pylint: disable=import-outside-toplevel
        mixin_classes.append(ThermostatMixin)

    mixin_classes.append(MerossEmulator)
    # build a label to cache the set
    class_name = ''
    for m in mixin_classes:
        class_name = class_name + m.__name__
    class_type = type(class_name, tuple(mixin_classes), {})

    return class_type(descriptor, key)


def run(argv):
    """
    self running python app entry point
    command line invocation:
    'python -m aiohttp.web -H localhost -P 80 meross_lan.emulator:run tracefilepath'
    """
    key = ''
    uuid = '01234567890123456789001122334455'
    for arg in argv:
        arg: str
        if arg.startswith('-K'):
            key = arg[2:].strip()
        elif arg.startswith('-U'):
            uuid = arg[2:].strip()
        else:
            tracefilepath = arg

    app = web.Application()

    if os.path.isdir(tracefilepath):
        uuidsub = 0
        for f in os.listdir(tracefilepath):
            fullpath = os.path.join(tracefilepath, f)
            #expect only valid csv files
            f = f.split('.')
            if f[-1] not in ('csv','txt','json'):
                continue

            # filename could be formatted to carry device definitions parameters:
            # format the filename like 'xxxwhatever-Kdevice_key-Udevice_id'
            # this way, parameters will be 'binded' to that trace in an easy way
            _key = key
            uuidsub = uuidsub + 1
            _uuidsub = str(uuidsub)
            _uuid = uuid[:-len(_uuidsub)] + _uuidsub
            for f in f[0].split('-'):
                if f.startswith('K'):
                    _key = f[1:].strip()
                elif f.startswith('U'):
                    _uuid = f[1:].strip()
            emulator = build_emulator(fullpath, _uuid, _key)
            app.router.add_post(f"/{_uuid}/config", emulator.post_config)
    else:
        #device = MerossDevice("custom_components/meross_lan/traces/msh300-1638110082.csv")
        emulator = build_emulator(tracefilepath, _uuid, _key)
        app.router.add_post("/config", emulator.post_config)

    return app