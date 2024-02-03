from __future__ import annotations

import typing

from ..helpers.namespaces import SmartPollingStrategy
from ..merossclient import const as mc
from ..number import PERCENTAGE, MLConfigNumber

if typing.TYPE_CHECKING:
    from ..meross_device import MerossDevice


class MLScreenBrightnessNumber(MLConfigNumber):
    manager: ScreenBrightnessMixin

    # HA core entity attributes:
    icon: str = "mdi:brightness-percent"
    native_max_value = 100
    native_min_value = 0
    native_step = 12.5
    native_unit_of_measurement = PERCENTAGE

    def __init__(self, manager: ScreenBrightnessMixin, channel: object, key: str):
        self.key_value = key
        self.name = f"Screen brightness ({key})"
        super().__init__(manager, channel, f"screenbrightness_{key}")

    """REMOVE(attr)
    @property
    def native_max_value(self):
        return 100

    @property
    def native_min_value(self):
        return 0

    @property
    def native_step(self):
        return 12.5

    @property
    def native_unit_of_measurement(self):
        return PERCENTAGE
    """

    async def async_set_native_value(self, value: float):
        brightness = {
            mc.KEY_CHANNEL: self.channel,
            mc.KEY_OPERATION: self.manager._number_brightness_operation.native_value,
            mc.KEY_STANDBY: self.manager._number_brightness_standby.native_value,
        }
        brightness[self.key_value] = value
        if await self.manager.async_request_ack(
            mc.NS_APPLIANCE_CONTROL_SCREEN_BRIGHTNESS,
            mc.METHOD_SET,
            {mc.KEY_BRIGHTNESS: [brightness]},
        ):
            self.update_native_value(value)


class ScreenBrightnessMixin(
    MerossDevice if typing.TYPE_CHECKING else object
):  # pylint: disable=used-before-assignment
    _number_brightness_operation: MLScreenBrightnessNumber
    _number_brightness_standby: MLScreenBrightnessNumber

    def __init__(self, descriptor, entry):
        super().__init__(descriptor, entry)

        with self.exception_warning("ScreenBrightnessMixin init"):
            # the 'ScreenBrightnessMixin' actually doesnt have a clue of how many  entities
            # are controllable since the digest payload doesnt carry anything (like MerossShutter)
            # So we're not implementing _init_xxx and _parse_xxx methods here and
            # we'll just add a couple of number entities to control 'active' and 'standby' brightness
            # on channel 0 which will likely be the only one available
            self._number_brightness_operation = MLScreenBrightnessNumber(
                self, 0, mc.KEY_OPERATION
            )
            self._number_brightness_standby = MLScreenBrightnessNumber(
                self, 0, mc.KEY_STANDBY
            )
            SmartPollingStrategy(
                self,
                mc.NS_APPLIANCE_CONTROL_SCREEN_BRIGHTNESS,
                item_count=1,
            )

    # interface: MerossDevice
    async def async_shutdown(self):
        await super().async_shutdown()
        self._number_brightness_operation = None  # type: ignore
        self._number_brightness_standby = None  # type: ignore

    # interface: self
    def _handle_Appliance_Control_Screen_Brightness(self, header: dict, payload: dict):
        for p_channel in payload[mc.KEY_BRIGHTNESS]:
            if p_channel.get(mc.KEY_CHANNEL) == 0:
                self._number_brightness_operation.update_native_value(
                    p_channel[mc.KEY_OPERATION]
                )
                self._number_brightness_standby.update_native_value(
                    p_channel[mc.KEY_STANDBY]
                )
                break
