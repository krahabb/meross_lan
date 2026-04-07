from functools import cached_property
from typing import TYPE_CHECKING, override

from . import SubDevice, mc, mlc, mn, mn_h
from ...number import NumberParser
from ...switch import SwitchParser

if TYPE_CHECKING:
    from typing import TypedDict, Unpack

    from . import Hub
    from ...merossclient.protocol import types as mt


class mst100(SubDevice, SwitchParser):
    """Switch to turn on/off the MST valve."""

    if TYPE_CHECKING:
        # Appliance.Config.DeviceCfg payload structure
        class DeviceCfg_mstCfg_calibration(TypedDict):
            waCon: int  # water consumption
            onoff: int
            lmTime: int

        class DeviceCfg_mstCfg(TypedDict):
            dura: int  # duration of watering in seconds
            wfm: int  # water flow measurement
            calibration: "mst100.DeviceCfg_mstCfg_calibration"

        class DeviceCfg(mt.hub.SubIdPayload):
            mstCfg: "mst100.DeviceCfg_mstCfg"

    class WateringDurationNumber(NumberParser):
        """Number to set watering duration."""

        init_ns = mn.Appliance_Config_DeviceCfg
        init_entity_key = mc.KEY_DURATION
        init_key_value = NumberParser.NestedKeyValue("mstCfg", "dura")
        # HA core entity attributes:
        _attr_name = "Watering duration"
        _attr_device_class = NumberParser.DEVICE_CLASS_DURATION
        _attr_native_unit_of_measurement = mlc.hac.UnitOfTime.SECONDS
        _attr_native_max_value = (
            86400  # 1 day max duration (no real info just guessing)
        )
        _attr_native_min_value = 1

    NS_HUB = (mn.Appliance_Config_DeviceCfg, *SubDevice.NS_HUB)
    init_ns = mn_h.Appliance_Control_Water
    init_value_on = 1
    init_value_off = 2

    _attr_name = "Watering"

    __slots__ = ("number_duration",)

    def __init__(self, subid: str, hub: "Hub", key_digest: str, model: str, /):
        SubDevice.__init__(self, subid, hub, key_digest, model)
        self.index = mn.IndexType.subId(subid, 0)
        self.number_duration = mst100.WateringDurationNumber(
            subid, hub, index=self.index
        )

    @cached_property
    def unique_id(self) -> str | None:
        # patch unique_id to mantain compatibility
        # with previous versions until we refactor the whole unique_id system
        return f"{self.parent.id}_{self.id}_onoff"

    def shutdown(self):
        SubDevice.shutdown(self)
        del self.number_duration

    @override
    def _parse_digest_(self, payload: "mt.hub._mst100", /):
        # unknown payload semantic
        pass

    def _parse_deviceCfg(self, payload: "DeviceCfg", /):
        self.number_duration._parse(payload)


class mst200(SubDevice):

    if TYPE_CHECKING:
        # Appliance.Config.DeviceCfg payload structure
        class DeviceCfg(mt.hub.SubIdPayload):
            mstCfg: mt.JsonMapping

    NS_HUB = (mn.Appliance_Config_DeviceCfg, *SubDevice.NS_HUB)
    init_ns = mn_h.Appliance_Control_Water
    init_value_on = 1
    init_value_off = 2

    def __init__(self, subid: str, hub: "Hub", key_digest: str, model: str, /):
        SubDevice.__init__(self, subid, hub, key_digest, model)

    @override
    def _parse(self, payload: "mt.hub.Water", /):
        pass

    @override
    def _parse_digest_(self, payload: "mt.hub._mst200", /):
        pass

    def _parse_deviceCfg(self, payload: "DeviceCfg", /):
        pass
