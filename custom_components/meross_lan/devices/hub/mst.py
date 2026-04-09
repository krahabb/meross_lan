from functools import cached_property
from typing import TYPE_CHECKING, override

from . import SubDevice, mc, mlc, mn, mn_h
from ...number import NumberParser
from ...switch import SwitchParser

if TYPE_CHECKING:
    from typing import TypedDict, Unpack

    from . import Hub
    from ...merossclient.protocol import types as mt


class mst(SubDevice):
    """Smart Water Sprinkler."""

    class Switch(SwitchParser):
        """Switch to turn on/off the MST valve."""

        init_ns = mn_h.Appliance_Control_Water
        # init_entity_key = f"{init_ns.slug}__{SwitchParser.init_key_value}"
        init_entity_key = mc.KEY_ONOFF  # to mantain unique_id compatibility
        init_value_on = 1
        init_value_off = 2

        _attr_name = "Watering"

    class WateringDurationNumber(NumberParser):
        """Number to set watering duration."""

        init_ns = mn.Appliance_Config_DeviceCfg
        init_key_value = NumberParser.NestedKeyValue("mstCfg", "dura")
        # init_entity_key = f"{init_ns.slug}__{init_key_value}"
        init_entity_key = mc.KEY_DURATION  # to mantain unique_id compatibility
        # HA core entity attributes:
        _attr_name = "Watering duration"
        _attr_device_class = NumberParser.DEVICE_CLASS_DURATION
        _attr_native_unit_of_measurement = mlc.hac.UnitOfTime.SECONDS
        _attr_native_max_value = (
            86400  # 1 day max duration (no real info just guessing)
        )
        _attr_native_min_value = 1


class mst100(mst):

    def __init__(self, subid: str, hub: "Hub", key_digest: str, model: str, /):
        SubDevice.__init__(self, subid, hub, key_digest, model)
        index = mn.IndexType.subId(subid, 0)
        hub.get_handler(mn_h.Appliance_Control_Water).register_parser(
            mst.Switch(subid, hub, index=index)
        )
        hub.get_handler(mn.Appliance_Config_DeviceCfg).register_parser(
            mst.WateringDurationNumber(subid, hub, index=index)
        )

    @override
    def _parse_digest_(self, payload: "mt.hub._mst100", /):
        # unknown payload semantic
        pass


class mst200(mst):

    def __init__(self, subid: str, hub: "Hub", key_digest: str, model: str, /):
        SubDevice.__init__(self, subid, hub, key_digest, model)
        for channel in range(1, 3):
            hub.get_handler(mn_h.Appliance_Control_Water).register_parser(
                mst.Switch(subid, hub, index=mn.IndexType.subId(subid, channel))
            )
            hub.get_handler(mn.Appliance_Config_DeviceCfg).register_parser(
                mst.WateringDurationNumber(
                    subid, hub, index=mn.IndexType.subId(subid, channel)
                )
            )

    @override
    def _parse_digest_(self, payload: "mt.hub._mst100", /):
        # unknown payload semantic
        pass
