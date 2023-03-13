"""
    Emulator descriptor:
    build a description of the emulator by parsing the trace file.
    This will be used then to instantiate a proper Emulator class
    in case we need some special behavor
"""
from __future__ import annotations

from json import loads as json_loads

from custom_components.meross_lan.merossclient import (
    MerossDeviceDescriptor,
    const as mc,
    get_namespacekey,
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
        hardware[mc.KEY_MACADDRESS] = uuid[-12:]

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

        except:
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
                    else json_loads(data)
                }
            else:
                self.namespaces[namespace] = (
                    data if isinstance(data, dict) else json_loads(data)
                )
