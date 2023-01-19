from __future__ import annotations

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

CORE_HAS_NATIVE_UNIT = hasattr(NumberEntity, 'native_unit_of_measurement')

from .merossclient import const as mc, get_namespacekey  # mEROSS cONST
from .meross_entity import (
    _MerossEntity,
    platform_setup_entry, platform_unload_entry,
    ENTITY_CATEGORY_CONFIG,
)
from .sensor import CLASS_TO_UNIT_MAP
from .helpers import LOGGER


async def async_setup_entry(hass: object, config_entry: object, async_add_devices):
    platform_setup_entry(hass, config_entry, async_add_devices, PLATFORM_NUMBER)

async def async_unload_entry(hass: object, config_entry: object) -> bool:
    return platform_unload_entry(hass, config_entry, PLATFORM_NUMBER)


if CORE_HAS_NATIVE_UNIT:
    # implement 'new' (2022.6) style NumberEntity
    class MLConfigNumber(_MerossEntity, NumberEntity):

        PLATFORM = PLATFORM_NUMBER

        multiplier = 1

        _attr_mode = NUMBERMODE_BOX
        _attr_native_max_value: float
        _attr_native_min_value: float
        _attr_native_step: float
        _attr_native_unit_of_measurement: str | None


        @property
        def entity_category(self):
            return ENTITY_CATEGORY_CONFIG

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
        def native_value(self) -> float | None:
            return self._attr_state

        def update_native_value(self, value):
            self.update_state(value / self.multiplier)

else:
    # pre 2022.6 style NumberEntity
    # since derived classes will try to adapt to new _native_* style
    # here we adapt for older HA cores
    class MLConfigNumber(_MerossEntity, NumberEntity):

        PLATFORM = PLATFORM_NUMBER

        multiplier = 1

        _attr_mode = NUMBERMODE_BOX
        _attr_native_max_value: float
        _attr_native_min_value: float
        _attr_native_step: float
        _attr_native_unit_of_measurement: str | None


        @property
        def entity_category(self):
            return ENTITY_CATEGORY_CONFIG

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
        def max_value(self) -> float:
            return self.native_max_value

        @property
        def min_value(self) -> float:
            return self.native_min_value

        @property
        def step(self) -> float:
            return self.native_step

        @property
        def unit_of_measurement(self) -> str | None:
            return self.native_unit_of_measurement

        @property
        def value(self) -> float | None:
            return self._attr_state

        def update_native_value(self, value):
            self.update_state(value / self.multiplier)

        async def async_set_value(self, value: float):
            await self.async_set_native_value(value)


class MLHubAdjustNumber(MLConfigNumber):

    multiplier = 100

    def __init__(
        self,
        subdevice: "MerossSubDevice",
        key: str,
        namespace: str,
        device_class: str,
        min_value: float,
        max_value: float,
        step: float
        ):
        self._key = key
        self._namespace = namespace
        self._namespace_key = get_namespacekey(namespace)
        self._attr_native_min_value = min_value
        self._attr_native_max_value = max_value
        self._attr_native_step = step
        self._attr_native_unit_of_measurement = CLASS_TO_UNIT_MAP.get(device_class)
        self._attr_name = f"Adjust {device_class}"
        super().__init__(
            subdevice.hub,
            subdevice.id,
            f"config_{self._namespace_key}_{key}",
            device_class,
            subdevice)


    async def async_set_native_value(self, value: float):
        self.device.request(
            self._namespace,
            mc.METHOD_SET,
            {
                self._namespace_key: [
                    {
                        mc.KEY_ID: self.subdevice.id,
                        self._key: int(value * self.multiplier)
                    }
                ]
            },
        )



class MLScreenBrightnessNumber(MLConfigNumber):

    _attr_native_max_value = 100
    _attr_native_min_value = 0
    _attr_native_step = 1
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_icon = 'mdi:brightness-percent'


    def __init__(self, device: "MerossDevice", channel: object, key: str):
        self._key = key
        self._attr_name = f"Screen brightness ({key})"
        super().__init__(device, channel, f"screenbrightness_{key}")


    async def async_set_native_value(self, value: float):
        payload = {
            mc.KEY_CHANNEL: self.channel,
            mc.KEY_OPERATION: self.device._number_brightness_operation.native_value,
            mc.KEY_STANDBY: self.device._number_brightness_standby.native_value
            }
        payload[self._key] = value

        def _ack_callback():
            self.update_native_value(value)

        self.device.request(
            mc.NS_APPLIANCE_CONTROL_SCREEN_BRIGHTNESS,
            mc.METHOD_SET,
            { mc.KEY_BRIGHTNESS: [ payload ] },
            _ack_callback
        )



class ScreenBrightnessMixin:


    def __init__(self, api, descriptor, entry):
        super().__init__(api, descriptor, entry)

        try:
            # the 'ScreenBrightnessMixin' actually doesnt have a clue of how many  entities
            # are controllable since the digest payload doesnt carry anything (like MerossShutter)
            # So we're not implementing _init_xxx and _parse_xxx methods here and
            # we'll just add a couple of number entities to control 'active' and 'standby' brightness
            # on channel 0 which will likely be the only one available
            self._number_brightness_operation = MLScreenBrightnessNumber(self, 0, mc.KEY_OPERATION)
            self._number_brightness_standby = MLScreenBrightnessNumber(self, 0, mc.KEY_STANDBY)
            self.polling_dictionary.add(mc.NS_APPLIANCE_CONTROL_SCREEN_BRIGHTNESS)

        except Exception as e:
            LOGGER.warning("ScreenBrightnessMixin(%s) init exception:(%s)", self.device_id, str(e))


    def _handle_Appliance_Control_Screen_Brightness(self, header: dict, payload: dict):
        p_channels = payload.get(mc.KEY_BRIGHTNESS)
        for p_channel in p_channels:
            if p_channel.get(mc.KEY_CHANNEL) == 0:
                self._number_brightness_operation.update_native_value(p_channel[mc.KEY_OPERATION])
                self._number_brightness_standby.update_native_value(p_channel[mc.KEY_STANDBY])
                break

