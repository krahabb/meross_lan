from typing import TYPE_CHECKING, override

from . import DeviceCfgParser, SubDevice, mc, mlc, mn, mn_h
from ...binary_sensor import BinarySensorParser
from ...number import NumberParser
from ...switch import SwitchParser

if TYPE_CHECKING:
    from typing import Final, TypedDict, Unpack

    from . import Hub
    from ...merossclient import SubDeviceDescriptor
    from ...merossclient.protocol import types as mt


KEY_MSTCFG = "mstCfg"
KEY_WACON = "waCon"
KEY_DURA = "dura"
KEY_WFM = "wfm"
KEY_WADET = "waDet"
KEY_WFLOW = "wflow"


class mst(SubDevice):
    """Smart Water Sprinkler."""

    DEVICE_CFG_DEFS = {
        KEY_MSTCFG: {
            mc.KEY_CALIBRATION: {
                mc.KEY_ONOFF: SwitchParser.DEF(
                    entity_key=f"{mn.Appliance_Config_DeviceCfg.slug}__{KEY_MSTCFG}_{mc.KEY_CALIBRATION}_{mc.KEY_ONOFF}",
                    value_on=1,
                    value_off=2,
                    name="Calibration on/off",
                ),
                KEY_WACON: NumberParser.DEF(
                    entity_key=f"{mn.Appliance_Config_DeviceCfg.slug}__{KEY_MSTCFG}_{mc.KEY_CALIBRATION}_{KEY_WACON}",
                    name="Calibration water consumption",
                    native_unit_of_measurement=mlc.hac.UnitOfVolume.MILLILITERS,
                ),
            },
            KEY_DURA: NumberParser.DEF(
                entity_key=f"{mn.Appliance_Config_DeviceCfg.slug}__{KEY_MSTCFG}_{KEY_DURA}",
                name="Watering duration",
                device_class=NumberParser.DEVICE_CLASS_DURATION,
                native_unit_of_measurement=mlc.hac.UnitOfTime.SECONDS,
                native_max_value=(
                    86400  # 1 day max duration (no real info just guessing)
                ),
                native_min_value=1,
                native_step=1,
            ),
            KEY_WFM: SwitchParser.DEF(
                entity_key=f"{mn.Appliance_Config_DeviceCfg.slug}__{KEY_MSTCFG}_{KEY_WFM}",
                value_on=1,
                value_off=2,
                name="Water flow measurement",
            ),
        },
    } | DeviceCfgParser._INIT_parser_defs  # type: ignore[assignment]

    class Switch(SwitchParser):
        """Switch to turn on/off the MST valve."""

        _INIT_ns = mn_h.Appliance_Control_Water
        _INIT_entity_key = f"{_INIT_ns.slug}__{SwitchParser._INIT_key_value}"
        _INIT_value_on = 1
        _INIT_value_off = 2

        _attr_name = "Watering"

    class WaterFlowSensor(BinarySensorParser):
        _INIT_ns = mn_h.Appliance_Control_WaterEvent
        _INIT_key_value = BinarySensorParser.KeyValue(KEY_WFLOW)
        _INIT_entity_key = f"{_INIT_ns.slug}__{_INIT_key_value}"
        _INIT_value_on = 2
        _INIT_value_off = 1
        _attr_device_class = BinarySensorParser.DeviceClass.PROBLEM

    __slots__ = ("sensor_wflow",)


class mst100(mst):

    if TYPE_CHECKING:
        sensor_wflow: Final[mst.WaterFlowSensor]

    def __init__(self, descriptor: "SubDeviceDescriptor", hub: "Hub", /, **kwargs):
        SubDevice.__init__(self, descriptor, hub, **kwargs)
        subid = descriptor.id
        index = mn.IndexType.subId.get(subid, 0, None)
        switch = mst.Switch(self, index=index, unique_id=f"{hub.id}_{subid}_onoff")
        hub.get_handler(mn_h.Appliance_Control_Water).register_parser(switch)
        self.sensor_wflow = mst.WaterFlowSensor(switch)
        hub.ns_handlers[mn.Appliance_Config_DeviceCfg].register_parser(
            DeviceCfgParser(subid, hub, index=index, parser_defs=mst.DEVICE_CFG_DEFS)
        )

    @override
    def _parse_digest_(self, payload: "mt.hub._mst100", /):
        # unknown payload semantic
        self.sensor_wflow.update_device_value(payload[KEY_WFLOW])


class mst200(mst):

    if TYPE_CHECKING:
        sensor_wflow: Final[list[mst.WaterFlowSensor]]

    def __init__(self, descriptor: "SubDeviceDescriptor", hub: "Hub", /, **kwargs):
        SubDevice.__init__(self, descriptor, hub, **kwargs)
        handler_water = hub.get_handler(mn_h.Appliance_Control_Water)
        handler_devicecfg = hub.ns_handlers[mn.Appliance_Config_DeviceCfg]
        subid = descriptor.id
        self.sensor_wflow = []
        for channel in range(1, 3):
            # indexed by subId, channels
            switch = mst.Switch(
                self, index=mn.IndexType.subId.get(subid, None, channel)
            )
            handler_water.register_parser(switch)
            self.sensor_wflow.append(mst.WaterFlowSensor(switch))
            # indexed by subId, channel
            handler_devicecfg.register_parser(
                DeviceCfgParser(
                    subid,
                    hub,
                    index=mn.IndexType.subId.get(subid, channel, None),
                    parser_defs=mst.DEVICE_CFG_DEFS,
                )
            )

    @override
    def _parse_digest_(self, payload: "mt.hub._mst200", /):
        for waDet in payload["waDet"]:
            try:
                self.sensor_wflow[waDet[mc.KEY_CHANNEL] - 1].update_device_value(
                    waDet[KEY_WFLOW]
                )
            except Exception as e:
                self.log_exception(self.WARNING, e, "_parse_digest")
