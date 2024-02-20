from __future__ import annotations

import typing

from ..helpers.namespaces import NamespaceHandler, SmartPollingStrategy
from ..merossclient import const as mc
from ..number import MLConfigNumber

if typing.TYPE_CHECKING:
    from ..meross_device import MerossDevice


class MLScreenBrightnessNumber(MLConfigNumber):
    manager: MerossDevice

    namespace = mc.NS_APPLIANCE_CONTROL_SCREEN_BRIGHTNESS
    key_namespace = mc.KEY_BRIGHTNESS

    # HA core entity attributes:
    icon: str = "mdi:brightness-percent"
    native_max_value = 100
    native_min_value = 0
    native_step = 12.5

    def __init__(self, manager: MerossDevice, key: str):
        self.key_value = key
        self.name = f"Screen brightness ({key})"
        super().__init__(
            manager,
            0,
            f"screenbrightness_{key}",
            native_unit_of_measurement=MLConfigNumber.UNIT_PERCENTAGE,
        )

    async def async_set_native_value(self, value: float):
        """Override base async_set_native_value since it would round
        the value to an int (common device native type)."""
        if await self.async_request(value):
            self.update_device_value(value)


class ScreenBrightnessNamespaceHandler(NamespaceHandler):

    __slots__ = (
        "number_brightness_operation",
        "number_brightness_standby",
    )

    def __init__(self, device: MerossDevice):
        super().__init__(
            device,
            mc.NS_APPLIANCE_CONTROL_SCREEN_BRIGHTNESS,
            handler=self._handle_Appliance_Control_Screen_Brightness,
        )
        self.number_brightness_operation = MLScreenBrightnessNumber(
            device, mc.KEY_OPERATION
        )
        self.number_brightness_standby = MLScreenBrightnessNumber(
            device, mc.KEY_STANDBY
        )
        SmartPollingStrategy(
            device,
            mc.NS_APPLIANCE_CONTROL_SCREEN_BRIGHTNESS,
            payload=[{mc.KEY_CHANNEL: 0}],
            item_count=1,
        )

    def _handle_Appliance_Control_Screen_Brightness(self, header: dict, payload: dict):
        for p_channel in payload[mc.KEY_BRIGHTNESS]:
            if p_channel[mc.KEY_CHANNEL] == 0:
                self.number_brightness_operation.update_device_value(
                    p_channel[mc.KEY_OPERATION]
                )
                self.number_brightness_standby.update_device_value(
                    p_channel[mc.KEY_STANDBY]
                )
                break
