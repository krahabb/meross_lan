from typing import TYPE_CHECKING, override

from . import DeviceCfgParser, SubDevice, mc, mlc, mn, mn_h
from ...number import NumberParser
from ...switch import SwitchParser

if TYPE_CHECKING:
    from typing import TypedDict, Unpack

    from . import Hub
    from ...merossclient import SubDeviceDescriptor
    from ...merossclient.protocol import types as mt


class mst(SubDevice):
    """Smart Water Sprinkler."""

    """
    calibrationConfig.setWaCon((int)
        (
            MSMSTUtils.m59095i0(this.f31278l) ?
                MSMSTRepository.m31088w().f18821c * 1000.0f :
                (MSMSTRepository.m31088w().f18821c * 1000.0f) / 0.26417f)
    );

    return (
        !(originDevice instanceof SprinklerDevice) ||
        (deviceConfigM59073V = m59073V((SprinklerDevice) originDevice)) == null ||
        (unitCfg = deviceConfigM59073V.getUnitCfg()) == null ||
        unitCfg.getUnitType() == null
        )
        ?
        m59070S()
        :
        unitCfg.getUnitType().intValue() == DeviceCommonConfig.UnitCfg.UNIT_PUB;

    """
    KEY_MSTCFG = "mstCfg"
    KEY_WACON = "waCon"
    KEY_DURA = "dura"
    KEY_WFM = "wfm"
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
    } | DeviceCfgParser.init_parser_defs  # type: ignore[assignment]

    class Switch(SwitchParser):
        """Switch to turn on/off the MST valve."""

        init_ns = mn_h.Appliance_Control_Water
        init_entity_key = f"{init_ns.slug}__{SwitchParser.init_key_value}"
        init_value_on = 1
        init_value_off = 2

        _attr_name = "Watering"


class mst100(mst):

    def __init__(self, descriptor: "SubDeviceDescriptor", hub: "Hub", /, **kwargs):
        SubDevice.__init__(self, descriptor, hub, **kwargs)
        subid = descriptor.id
        index = mn.IndexType.subId.get(subid, 0, None)
        switch = mst.Switch(self, index=index)
        switch.unique_id = f"{hub.id}_{subid}_onoff"  # LEGACY
        hub.get_handler(mn_h.Appliance_Control_Water).register_parser(switch)
        hub.ns_handlers[mn.Appliance_Config_DeviceCfg].register_parser(
            DeviceCfgParser(subid, hub, index=index, parser_defs=mst.DEVICE_CFG_DEFS)
        )

    @override
    def _parse_digest_(self, payload: "mt.hub._mst100", /):
        # unknown payload semantic
        pass


class mst200(mst):

    def __init__(self, descriptor: "SubDeviceDescriptor", hub: "Hub", /, **kwargs):
        SubDevice.__init__(self, descriptor, hub, **kwargs)
        handler_water = hub.get_handler(mn_h.Appliance_Control_Water)
        handler_devicecfg = hub.ns_handlers[mn.Appliance_Config_DeviceCfg]
        subid = descriptor.id
        for channel in range(1, 3):
            # indexed by subId, channels
            handler_water.register_parser(
                mst.Switch(self, index=mn.IndexType.subId.get(subid, None, channel))
            )
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
    def _parse_digest_(self, payload: "mt.hub._mst100", /):
        # unknown payload semantic
        pass
