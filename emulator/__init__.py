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

import os
from typing import TYPE_CHECKING

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
from .mixins import Emulator, EmulatorDescriptor, mc, mn

if TYPE_CHECKING:
    from typing import Iterable, Unpack

    class build_emulator_kwargs(Emulator.Args, EmulatorDescriptor.Args):
        pass


def build_emulator(tracefile, **kwargs: Unpack[build_emulator_kwargs]) -> Emulator:
    """
    Given a supported 'tracefile' (either a legacy trace .csv or a diagnostic .json)
    parse it and build the appropriate emulator instance with the give 'uuid' and 'key'
    this will also set the correct inferred mac address in the descriptor based on the uuid
    as this appears to be consistent with real devices config
    """
    print(f"Initializing descriptor({kwargs.values()}):", end="")
    descriptor = EmulatorDescriptor(tracefile, **kwargs)
    ability = descriptor.ability
    digest = descriptor.digest
    mixin_classes = []

    if mc.KEY_HUB in digest:
        from .mixins.hub import HubMixin

        mixin_classes.append(HubMixin)
    if (
        mc.KEY_THERMOSTAT in digest
        or mn.thermostat.Appliance_Control_Thermostat_ModeC in ability
    ):
        from .mixins.thermostat import ThermostatMixin

        mixin_classes.append(ThermostatMixin)
    if mc.KEY_GARAGEDOOR in digest:
        from .mixins.garagedoor import GarageDoorMixin

        mixin_classes.append(GarageDoorMixin)
    if mn.Appliance_Control_Electricity in ability:
        from .mixins.electricity import ElectricityMixin

        mixin_classes.append(ElectricityMixin)
    if mn.Appliance_Control_ElectricityX in ability:
        from .mixins.electricity import ElectricityXMixin

        mixin_classes.append(ElectricityXMixin)
    if mn.Appliance_Control_ConsumptionH in ability:
        from .mixins.electricity import ConsumptionHMixin

        mixin_classes.append(ConsumptionHMixin)

    if mn.Appliance_Control_ConsumptionX in ability:
        from .mixins.electricity import ConsumptionXMixin

        mixin_classes.append(ConsumptionXMixin)

    if mn.Appliance_Control_Light in ability:
        from .mixins.light import LightMixin

        mixin_classes.append(LightMixin)

    if mn.Appliance_Control_Fan in ability:
        from .mixins.fan import FanMixin

        mixin_classes.append(FanMixin)

    if mn.Appliance_RollerShutter_State in ability:
        from .mixins.rollershutter import RollerShutterMixin

        mixin_classes.append(RollerShutterMixin)

    if mn.Appliance_Control_PhysicalLock in ability:
        from .mixins.physicallock import PhysicalLockMixin

        mixin_classes.append(PhysicalLockMixin)

    mixin_classes.append(Emulator)
    # build a label to cache the set
    class_name = ""
    for m in mixin_classes:
        class_name = class_name + m.__name__
    class_type = type(class_name, tuple(mixin_classes), {})

    emulator = class_type(descriptor, **kwargs)
    print(
        f" {descriptor.type} (model:{descriptor.productmodel} uuid:{descriptor.uuid})"
    )
    return emulator


def build_emulators(
    tracespath: str,
    *,
    included_uuid: Iterable[str] | None = None,
    **kwargs: Unpack[build_emulator_kwargs],
):
    """
    This function is a generator.
    Scans the directory for supported files and build all the emulators
    the filename, if correctly formatted, should contain the device uuid
    and key to use for the emulator. If not, we'll use the 'defaultuuid' and/or
    'defaultkey' when instantiating the emulator. This allows for supporting
    basic plain filenames which don't contain any info but also, will make
    it difficult to understand which device is which
    """
    uuid = kwargs.get("uuid") or Emulator.DEFAULT_UUID

    uuidsub = 0
    for f in os.listdir(tracespath):
        fullpath = os.path.join(tracespath, f)
        # expect only valid csv or json files
        f = f.split(".")
        if f[-1] not in ("csv", "txt", "json"):
            continue

        _kwargs = kwargs.copy()
        # filename could be formatted to carry device definitions parameters:
        # format the filename like 'xxxwhatever-Kdevice_key-Udevice_id'
        # this way, parameters will be 'binded' to that trace in an easy way
        _uuid = None
        for _f in f[0].split("-"):
            if _f.startswith("K"):
                _kwargs["key"] = _f[1:].strip()
            elif _f.startswith("U"):
                _uuid = _f[1:].strip()
            elif _f.startswith("B"):
                _kwargs["broker"] = _f[1:].strip()
            elif _f.startswith("A"):
                _kwargs["userId"] = int(_f[1:].strip())

        if _uuid:
            if included_uuid and (_uuid not in included_uuid):
                continue
        else:
            uuidsub = uuidsub + 1
            _uuidsub = str(uuidsub)
            _uuid = uuid[: -len(_uuidsub)] + _uuidsub
        _kwargs["uuid"] = _uuid
        yield build_emulator(fullpath, **_kwargs)


def run(argv):
    """
    self running python app entry point
    command line invocation:
    'python -m aiohttp.web -H localhost -P 80 meross_lan.emulator:run tracefilepath'
    """
    emulator_kwargs: build_emulator_kwargs = {}
    tracefilepath = "."
    for arg in argv:
        arg: str
        if arg.startswith("-key"):
            emulator_kwargs["key"] = arg[4:].strip()
        elif arg.startswith("-uuid"):
            emulator_kwargs["uuid"] = arg[5:].strip()
        elif arg.startswith("-broker"):
            emulator_kwargs["broker"] = arg[7:].strip()
        else:
            tracefilepath = arg

    app = web.Application()

    def web_post_handler(emulator: Emulator):
        async def _callback(request: web.Request) -> web.Response:
            try:
                return web.Response(
                    status=200,
                    text=emulator.handle(await request.text()),
                )
            except Exception as exception:
                return web.Response(
                    status=500,
                    reason=str(exception) or exception.__class__.__name__,
                )

        return _callback

    if os.path.isdir(tracefilepath):
        emulators = {
            emulator.uuid: emulator
            for emulator in build_emulators(tracefilepath, **emulator_kwargs)
        }
        for _uuid, emulator in emulators.items():
            app.router.add_post(f"/{_uuid}/config", web_post_handler(emulator))
    else:
        emulator = build_emulator(tracefilepath, **emulator_kwargs)
        emulators = {emulator.uuid: emulator}
        app.router.add_post("/config", web_post_handler(emulator))

    async def _on_startup(app: web.Application):
        for emulator in emulators.values():
            await emulator.async_startup(
                enable_scheduler=True, enable_mqtt="broker" in emulator_kwargs
            )

    async def _on_shutdown(app: web.Application):
        for emulator in emulators.values():
            await emulator.async_shutdown()

    app.on_startup.append(_on_startup)
    app.on_shutdown.append(_on_shutdown)

    return app
