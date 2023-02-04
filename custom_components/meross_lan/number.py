from __future__ import annotations
import typing

from homeassistant.components.number import (
    DOMAIN as PLATFORM_NUMBER,
    NumberEntity,
)

try:
    from homeassistant.components.number import NumberMode

    NUMBERMODE_AUTO = NumberMode.AUTO
    NUMBERMODE_BOX = NumberMode.BOX
    NUMBERMODE_SLIDER = NumberMode.SLIDER
except:
    NUMBERMODE_AUTO = "auto"
    NUMBERMODE_BOX = "box"
    NUMBERMODE_SLIDER = "slider"

from homeassistant.const import (
    PERCENTAGE,
)

from .merossclient import const as mc, get_namespacekey  # mEROSS cONST
from . import meross_entity as me
from .sensor import CLASS_TO_UNIT_MAP
from .helpers import LOGGER

if typing.TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from homeassistant.config_entries import ConfigEntry
    from .meross_device import MerossDevice
    from .meross_device_hub import MerossSubDevice


CORE_HAS_NATIVE_UNIT = hasattr(NumberEntity, "native_unit_of_measurement")


async def async_setup_entry(
    hass: HomeAssistant, config_entry: ConfigEntry, async_add_devices
):
    me.platform_setup_entry(hass, config_entry, async_add_devices, PLATFORM_NUMBER)


async def async_unload_entry(hass: HomeAssistant, config_entry: ConfigEntry):
    return me.platform_unload_entry(hass, config_entry, PLATFORM_NUMBER)


if CORE_HAS_NATIVE_UNIT:
    # implement 'new' (2022.6) style NumberEntity
    PatchedNumberEntity = NumberEntity  # type: ignore
else:
    # pre 2022.6 style NumberEntity
    # since derived classes will try to adapt to new _native_* style
    # here we adapt for older HA cores
    class PatchedNumberEntity(NumberEntity):
        @property
        def max_value(self):
            return self.native_max_value

        @property
        def min_value(self):
            return self.native_min_value

        @property
        def step(self):
            return self.native_step

        @property
        def unit_of_measurement(self):
            return self.native_unit_of_measurement

        @property
        def value(self):
            return self._attr_state

        async def async_set_value(self, value: float):  # type: ignore
            await self.async_set_native_value(value)


class MLConfigNumber(me.MerossEntity, PatchedNumberEntity):

    PLATFORM = PLATFORM_NUMBER

    _attr_entity_category = me.EntityCategory.CONFIG
    _attr_mode = NUMBERMODE_BOX  # type: ignore
    _attr_native_max_value: float
    _attr_native_min_value: float
    _attr_native_step: float
    _attr_native_unit_of_measurement: str | None

    multiplier = 1
    # customize the request payload for different
    # devices api. see 'async_set_native_value' to see how
    namespace: str
    key_namespace: str
    key_channel: str = mc.KEY_CHANNEL
    key_value: str

    @property
    def native_max_value(self):
        return self._attr_native_max_value

    @property
    def native_min_value(self):
        return self._attr_native_min_value

    @property
    def native_step(self):
        return self._attr_native_step

    @property
    def native_unit_of_measurement(self):
        return self._attr_native_unit_of_measurement

    @property
    def native_value(self):
        return self._attr_state

    def update_native_value(self, value):
        self.update_state(value / self.multiplier)

    async def async_set_native_value(self, value: float):

        device_value = int(value * self.multiplier)

        def _ack_callback(acknowledge: bool, header: dict, payload: dict):
            if acknowledge:
                self.update_native_value(device_value)

        await self.device.async_request(
            self.namespace,
            mc.METHOD_SET,
            {
                self.key_namespace: [
                    {self.key_channel: self.channel, self.key_value: device_value}
                ]
            },
            _ack_callback,
        )


class MLHubAdjustNumber(MLConfigNumber):

    multiplier = 100
    key_channel = mc.KEY_ID

    def __init__(
        self,
        subdevice: "MerossSubDevice",
        key: str,
        namespace: str,
        device_class: str,
        min_value: float,
        max_value: float,
        step: float,
    ):
        self.namespace = namespace
        self.key_namespace = get_namespacekey(namespace)
        self.key_value = key
        self._attr_native_min_value = min_value
        self._attr_native_max_value = max_value
        self._attr_native_step = step
        self._attr_native_unit_of_measurement = CLASS_TO_UNIT_MAP.get(device_class)
        self._attr_name = f"Adjust {device_class}"
        super().__init__(
            subdevice.hub,
            subdevice.id,
            f"config_{self.key_namespace}_{key}",
            device_class,
            subdevice,
        )


class MLScreenBrightnessNumber(MLConfigNumber):

    device: ScreenBrightnessMixin

    _attr_native_max_value = 100
    _attr_native_min_value = 0
    _attr_native_step = 12.5
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_icon = "mdi:brightness-percent"

    def __init__(self, device: "MerossDevice", channel: object, key: str):
        self.key_value = key
        self._attr_name = f"Screen brightness ({key})"
        super().__init__(device, channel, f"screenbrightness_{key}")

    async def async_set_native_value(self, value: float):
        brightness = {
            mc.KEY_CHANNEL: self.channel,
            mc.KEY_OPERATION: self.device._number_brightness_operation.native_value,
            mc.KEY_STANDBY: self.device._number_brightness_standby.native_value,
        }
        brightness[self.key_value] = value

        def _ack_callback(acknowledge: bool, header: dict, payload: dict):
            if acknowledge:
                self.update_native_value(value)

        await self.device.async_request(
            mc.NS_APPLIANCE_CONTROL_SCREEN_BRIGHTNESS,
            mc.METHOD_SET,
            {mc.KEY_BRIGHTNESS: [brightness]},
            _ack_callback,
        )


class ScreenBrightnessMixin(
    MerossDevice if typing.TYPE_CHECKING else object
):  # pylint: disable=used-before-assignment
    def __init__(self, api, descriptor, entry):
        super().__init__(api, descriptor, entry)

        try:
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
            self.polling_dictionary[
                mc.NS_APPLIANCE_CONTROL_SCREEN_BRIGHTNESS
            ] = mc.PAYLOAD_GET[mc.NS_APPLIANCE_CONTROL_SCREEN_BRIGHTNESS]

        except Exception as e:
            LOGGER.warning(
                "ScreenBrightnessMixin(%s) init exception:(%s)", self.device_id, str(e)
            )

    def _handle_Appliance_Control_Screen_Brightness(self, header: dict, payload: dict):
        if isinstance(p_channels := payload.get(mc.KEY_BRIGHTNESS), list):
            for p_channel in p_channels:
                if p_channel.get(mc.KEY_CHANNEL) == 0:
                    self._number_brightness_operation.update_native_value(
                        p_channel[mc.KEY_OPERATION]
                    )
                    self._number_brightness_standby.update_native_value(
                        p_channel[mc.KEY_STANDBY]
                    )
                    break
