from http.client import HTTPException
import os
from time import time
from urllib.error import HTTPError
from aiohttp import web

from json import (
    dumps as json_dumps,
    loads as json_loads,
)

from ..merossclient import (
    MerossDeviceDescriptor,
    build_payload,
    const as mc,
    get_namespacekey,
    get_replykey,  # mEROSS cONST
)

class MerossDevice:


    def __init__(self, tracefile, uuid, key):
        self.uuid = uuid
        self.key = key
        self.namespaces = {}
        with open(tracefile, 'r') as f:
            for line in f:
                columns = line.split('\t')
                rxtx = columns[1]
                protocol = columns[2]
                method = columns[3]
                namespace = columns[4]
                data = columns[5]

                if method == mc.METHOD_GETACK:
                    if protocol == 'auto':
                        self.namespaces[namespace] = {
                            get_namespacekey(namespace): json_loads(data)
                        }
                    else:
                        self.namespaces[namespace] = json_loads(data)

        # patch system payload with fake ids
        self.descriptor = MerossDeviceDescriptor(self.namespaces[mc.NS_APPLIANCE_SYSTEM_ABILITY])
        self.p_all = self.namespaces[mc.NS_APPLIANCE_SYSTEM_ALL]
        self.p_all_digest = self.p_all[mc.KEY_ALL].get(mc.KEY_DIGEST, {})
        system = self.p_all[mc.KEY_ALL][mc.KEY_SYSTEM]
        self.p_all_system_time = system.get(mc.KEY_TIME)
        hardware = system[mc.KEY_HARDWARE]
        hardware[mc.KEY_UUID] = self.uuid
        hardware[mc.KEY_MACADDRESS] = self.uuid[-12:]
        self.descriptor.update(self.p_all)

        print(f"Initialized {self.descriptor.productname} (model:{self.descriptor.productmodel})")


    async def post_config(self, request: web.Request):
        jsonrequest = await request.json()
        header:dict = jsonrequest[mc.KEY_HEADER]
        payload:dict = jsonrequest[mc.KEY_PAYLOAD]
        namespace:str = header[mc.KEY_NAMESPACE]
        method:str = header[mc.KEY_METHOD]

        try:
            if namespace not in self.descriptor.ability:
                raise Exception(f"{namespace} not supported")

            elif get_replykey(header, self.key) is not self.key:
                method = mc.METHOD_ERROR
                payload = { mc.KEY_ERROR: { mc.KEY_CODE: mc.ERROR_INVALIDKEY} }

            elif (handler := getattr(self, f"_{method}_{namespace.replace('.', '_')}", None)) is not None:
                method, payload = handler(header, payload)

            else:# when no special handler in place we apply a raw euristhic

                try:
                    n = namespace.split('.')
                    if n[1] != 'Control':
                        raise Exception(f"{namespace} not supported")

                    key = get_namespacekey(namespace)
                    p_payload_key = payload[key]
                    p_digest = self.p_all_digest
                    if len(n) == 4:
                        p_digest = p_digest[n[2].lower()]

                    if key not in p_digest:
                        raise Exception(f"{key} not present in digest")
                    p_digest_key = p_digest[key]

                    if method != mc.METHOD_SET:
                        # TODO.....
                        raise Exception(f"{method} not supported")

                    def _update(payload: dict):
                        channel = payload[mc.KEY_CHANNEL]
                        for p in p_digest_key:
                            if p[mc.KEY_CHANNEL] == channel:
                                p.update(payload)
                                break
                        else:
                            raise Exception(f"{channel} not present in digest.{key}")

                    if isinstance(p_digest_key, list):
                        if isinstance(p_payload_key, list):
                            for p_p in p_payload_key:
                                _update(p_p)
                        else:
                            _update(p_payload_key)
                    else:
                        if p_digest_key[mc.KEY_CHANNEL] == p_payload_key[mc.KEY_CHANNEL]:
                            p_digest_key.update(p_payload_key)
                        else:
                            raise Exception(f"{p_payload_key[mc.KEY_CHANNEL]} not present in digest.{key}")

                    method = mc.METHOD_SETACK
                    payload = {}

                except Exception as e:
                    if (method == mc.METHOD_GET) and (namespace in self.namespaces):
                        method = mc.METHOD_GETACK
                        payload = self.namespaces[namespace]
                    else:
                        raise e


        except Exception as e:
            method = mc.METHOD_ERROR
            payload = { mc.KEY_ERROR: { mc.KEY_CODE: -1, "message": str(e)} }

        data = build_payload(namespace, method, payload, self.key, mc.MANUFACTURER, header[mc.KEY_MESSAGEID])
        return web.json_response(data)


    def _GET_Appliance_System_All(self, header, payload):
        if self.p_all_system_time:
            self.p_all_system_time[mc.KEY_TIMESTAMP] = int(time())
        return mc.METHOD_GETACK, self.namespaces[mc.NS_APPLIANCE_SYSTEM_ALL]


    def _SET_Appliance_Control_Thermostat_Mode(self, header, payload):
        p_digest_mode_list = self.p_all_digest[mc.KEY_THERMOSTAT][mc.KEY_MODE]
        p_digest_windowopened_list = dict()
        p_mode_list = payload[mc.KEY_MODE]
        for p_mode in p_mode_list:
            channel = p_mode[mc.KEY_CHANNEL]
            for p_digest_mode in p_digest_mode_list:
                if p_digest_mode[mc.KEY_CHANNEL] == channel:
                    p_digest_mode.update(p_mode)
                    mode = p_digest_mode[mc.KEY_MODE]
                    MODE_KEY_MAP = {
                        mc.MTS200_MODE_HEAT: mc.KEY_HEATTEMP,
                        mc.MTS200_MODE_COOL: mc.KEY_COOLTEMP,
                        mc.MTS200_MODE_ECO: mc.KEY_ECOTEMP,
                        mc.MTS200_MODE_CUSTOM: mc.KEY_MANUALTEMP
                    }
                    if mode in MODE_KEY_MAP:
                        p_digest_mode[mc.KEY_TARGETTEMP] = p_digest_mode[MODE_KEY_MAP[mode]]
                    else:# we use this to trigger a windowOpened later in code
                        p_digest_windowopened_list = self.p_all_digest[mc.KEY_THERMOSTAT][mc.KEY_WINDOWOPENED]
                    if p_digest_mode[mc.KEY_ONOFF]:
                        p_digest_mode[mc.KEY_STATE] = 1 if p_digest_mode[mc.KEY_TARGETTEMP] > p_digest_mode[mc.KEY_CURRENTTEMP] else 0
                    else:
                        p_digest_mode[mc.KEY_STATE] = 0
                    break
            else:
                raise Exception(f"{channel} not present in digest.thermostat")

            # randomly switch the window
            for p_digest_windowopened in p_digest_windowopened_list:
                if p_digest_windowopened[mc.KEY_CHANNEL] == channel:
                    p_digest_windowopened[mc.KEY_STATUS] = 0 if p_digest_windowopened[mc.KEY_STATUS] else 1
                    break

        return mc.METHOD_SETACK, {}

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
            if f[-1] != 'csv':
                continue

            #try to extract relevant parameters from file name
            _key = key
            uuidsub = uuidsub + 1
            _uuidsub = str(uuidsub)
            _uuid = uuid[:-len(_uuidsub)] + _uuidsub
            for f in f[0].split('-'):
                if f.startswith('K'):
                    _key = f[1:].strip()
                elif f.startswith('U'):
                    _uuid = f[1:].strip()
            device = MerossDevice(fullpath, _uuid, _key)
            app.router.add_post(f"/{_uuid}/config", device.post_config)
    else:
        #device = MerossDevice("custom_components/meross_lan/traces/msh300-1638110082.csv")
        device = MerossDevice(tracefilepath, uuid, key)
        app.router.add_post("/config", device.post_config)

    return app